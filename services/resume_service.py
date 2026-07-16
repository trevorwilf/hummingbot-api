"""Copy-forward resume service.

Implements the copy-forward hook described in ``COPY_FORWARD_HOOK_DESIGN.md``
(design v2). The hook seeds a new bot instance's ``data/`` with the prior run's
range-ladder state files *before* the container starts, fail-closed, with zero
behavior change when resume is off.

Phase 2 scope (this module, source resolution — design §5):
    * ``ResumeAbortReason`` — enum of fail-closed abort reasons (§11).
    * ``ResumeError`` — exception carrying a ``ResumeAbortReason``.
    * ``ResolvedSource`` — the resolved prior instance to copy from.
    * ``resolve_source`` — "which one to grab" resolution for the ``explicit``
      and ``latest`` strategies.

Later phases extend this module with the copy set (§6), guards (§7) and hook
orchestration (§3/§4). Nothing here touches Docker, Postgres directly, or the
filesystem beyond read-only ``pathlib`` inspection of the bots directory.

Paths use ``pathlib`` throughout — prod is Linux, dev is Windows.
"""

import fnmatch
import hashlib
import json
import logging
import re
import secrets
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abort reasons (§11) and the resume exception
# ---------------------------------------------------------------------------

class ResumeAbortReason(str, Enum):
    """Fail-closed abort reasons, mirroring the §11 failure-modes table.

    Members are declared once here and imported by every later phase. The full
    set is declared up-front (even though Phase 2 only raises a subset) so the
    enum is stable across the batch.
    """

    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    LATEST_AMBIGUOUS = "LATEST_AMBIGUOUS"
    ARCHIVE_NESTED = "ARCHIVE_NESTED"
    SOURCE_RUNNING = "SOURCE_RUNNING"
    LEDGER_INVALID = "LEDGER_INVALID"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    DEST_EXISTS = "DEST_EXISTS"
    DEST_NOT_EMPTY = "DEST_NOT_EMPTY"
    UNGRACEFUL_SOURCE = "UNGRACEFUL_SOURCE"
    EXTRA_PATH_ESCAPE = "EXTRA_PATH_ESCAPE"
    EXTRA_PATH_MISSING = "EXTRA_PATH_MISSING"
    COPY_IO_ERROR = "COPY_IO_ERROR"


class ResumeError(Exception):
    """Raised whenever a resume must abort fail-closed.

    Carries a :class:`ResumeAbortReason` so callers (the hook, the router's
    error mapping) can branch on the machine-readable reason while still
    surfacing a human-readable message.
    """

    def __init__(self, reason: ResumeAbortReason, message: str):
        self.reason = reason
        self.message = message
        super().__init__(f"{reason.value}: {message}")


# ---------------------------------------------------------------------------
# Resolved source
# ---------------------------------------------------------------------------

@dataclass
class ResolvedSource:
    """A resolved prior instance whose ``data/`` will be copied forward.

    Attributes:
        instance_name: The prior instance's name (its directory basename).
        data_dir: Absolute path to the prior instance's ``data/`` directory.
        instance_dir: Absolute path to the prior instance's root directory.
        origin: Where it was resolved from — ``"instances"`` (live tree) or
            ``"archived"`` (local-move archive tree).
    """

    instance_name: str
    data_dir: Path
    instance_dir: Path
    origin: str  # "instances" | "archived"


# ---------------------------------------------------------------------------
# Base-name / timestamp parsing (§5 latest rules)
# ---------------------------------------------------------------------------

# The API's own instance-name suffix, in two generations:
#
#   legacy   "<base>-YYYYMMDD-HHMMSS"                     (pre-CDX-001)
#   current  "<base>-YYYYMMDD-HHMMSS-<micros>-<rand>"     (CDX-001)
#
# The sub-second + random components exist because the second-granular stamp let
# two deploys of the same base name inside one wall-clock second generate the
# SAME instance name and collide on the target directory (CDX-001). The trailing
# pair is OPTIONAL here so legacy-named instances — already on disk and already
# in ``bot_runs`` — keep parsing, and therefore keep resolving as ``latest``
# lineage. Still anchored at the END and applied exactly ONCE, so operator names
# that embed their own timestamp-like tokens (e.g. "KRAKEN_LADDER_V1-20260712-2302")
# are never double-stripped.
#
# INVARIANT: this regex and :func:`generate_instance_name` are a matched pair —
# every name the generator emits must parse back to (base, timestamp), or
# ``latest`` resolution silently stops finding lineage (the generator's output
# would no longer match its own base). The round-trip is asserted in
# tests/test_copyforward_p1_exclusive_target.py. Never change one alone.
_API_SUFFIX_RE = re.compile(r"-(\d{8}-\d{6})(?:-(\d{6})-([0-9a-f]{6}))?$")
_API_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
# Bytes of entropy in the name suffix; 3 bytes -> the 6 hex chars matched above.
_API_NAME_RANDOM_BYTES = 3


def generate_instance_name(base_name: str, now=None) -> str:
    """Build the unique instance name a deploy of ``base_name`` runs under.

    ``<base>-YYYYMMDD-HHMMSS-<micros>-<rand>``: the wall-clock stamp keeps names
    sortable and human-readable, while the microseconds and 6 hex chars of
    entropy make the name collision-free (CDX-001 — two deploys of one base name
    within the same second used to produce the same name, and the loser silently
    reused the winner's directory).

    Uniqueness must not rest on the clock alone: two API workers can read the
    same microsecond, and a clock can step backwards over NTP. The random
    component is what actually guarantees distinctness; the timestamp is for
    humans and for ``latest`` ordering.

    Args:
        base_name: The operator-supplied name (already model-validated).
        now: Injectable clock (tests). Defaults to ``datetime.now()``.

    Returns:
        The unique name, which :func:`_strip_api_suffix` maps back to
        ``base_name`` and :func:`_parse_api_timestamp` maps back to ``now``.
    """
    from datetime import datetime

    stamp = now if now is not None else datetime.now()
    return (
        f"{base_name}-{stamp.strftime(_API_TIMESTAMP_FORMAT)}"
        f"-{stamp.microsecond:06d}-{secrets.token_hex(_API_NAME_RANDOM_BYTES)}"
    )


def _strip_api_suffix(instance_name: str) -> str:
    """Return the logical base name = instance name with ONLY the final
    ``-YYYYMMDD-HHMMSS`` suffix removed (single application, anchored at end)."""
    return _API_SUFFIX_RE.sub("", instance_name, count=1)


def _parse_api_timestamp(instance_name: str):
    """Parse the datetime from the final API suffix, or ``None`` if absent.

    Uses ``datetime.strptime`` (not mtime — archiving/backup perturbs mtime).
    Import is local so the module has no import-time ``datetime`` dependency
    beyond what it uses, and to keep the top of the file about types only.

    The current name format carries microseconds (CDX-001); when present they
    are folded into the returned datetime so two instances deployed inside the
    same second still order strictly. Legacy names lack them and parse to a
    whole second, exactly as before.
    """
    from datetime import datetime

    match = _API_SUFFIX_RE.search(instance_name)
    if match is None:
        return None
    try:
        parsed = datetime.strptime(match.group(1), _API_TIMESTAMP_FORMAT)
    except ValueError:
        return None
    micros = match.group(2)
    if micros is not None:
        parsed = parsed.replace(microsecond=int(micros))
    return parsed


# ---------------------------------------------------------------------------
# Archive nesting pathology (§5, R3)
# ---------------------------------------------------------------------------

def _looks_like_instance(directory: Path) -> bool:
    """A directory "looks like a complete instance" if it holds a ``data/`` or
    ``conf/`` subdir (§5 nested-archive resolution)."""
    return (directory / "data").is_dir() or (directory / "conf").is_dir()


def _resolve_archive_instance_dir(archive_base: Path, name: str) -> Optional[Path]:
    """Resolve the instance directory inside a local-move archive.

    ``BotArchiver.archive_locally`` has no same-name collision handling
    (utils/bot_archiver.py:52-53 ``shutil.move`` into an existing dir), so
    archiving a name twice nests ``bots/archived/<name>/<name>``. We consider
    the base level and one level of same-name nesting:

        * exactly one plausible level  -> return it
        * more than one plausible level -> raise ``ARCHIVE_NESTED`` (ambiguous)
        * zero plausible levels         -> return ``None`` (not found)

    Returns the resolved instance directory, or ``None`` when nothing plausible
    exists (caller raises ``SOURCE_NOT_FOUND`` with the S3 note).
    """
    if not archive_base.exists():
        return None

    candidates: List[Path] = []
    if _looks_like_instance(archive_base):
        candidates.append(archive_base)
    nested = archive_base / name
    if nested.is_dir() and _looks_like_instance(nested):
        candidates.append(nested)

    if len(candidates) > 1:
        raise ResumeError(
            ResumeAbortReason.ARCHIVE_NESTED,
            f"Ambiguous nested archive at '{archive_base}': both the base "
            f"directory and its nested '{name}/{name}' copy look like complete "
            f"instances. Archiving the same name twice nests them; resolve by "
            f"hand or use resume_mode='explicit' against an un-nested instance.",
        )
    if len(candidates) == 1:
        return candidates[0]
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def resolve_source(
    deployment,
    new_instance_name: str,
    bots_path: Path,
    bot_run_repo,
) -> ResolvedSource:
    """Resolve the single prior instance whose ``data/`` should be copied forward.

    Args:
        deployment: The deploy model (``V2ControllerDeployment`` /
            ``V2ScriptDeployment``). Only the resume fields are read:
            ``resume_mode``, ``resume_from``, ``resume_from_archive``.
        new_instance_name: The timestamped name of the instance being created
            (excluded from ``latest`` candidates).
        bots_path: The ``bots/`` directory that contains the ``instances/`` and
            ``archived/`` subtrees. ``pathlib`` — never assume a separator.
        bot_run_repo: The existing ``BotRunRepository`` (or a compatible mock),
            used for ``latest`` lineage. Only ``get_bot_runs`` is called.

    Returns:
        A :class:`ResolvedSource` for the resolved prior instance.

    Raises:
        ResumeError: fail-closed on any resolution failure (§11).
        ValueError: if called with ``resume_mode == "off"`` (programmer error —
            the hook must gate on mode before calling).
    """
    bots_path = Path(bots_path)
    mode = deployment.resume_mode

    if mode == "off":
        # The hook gates on mode; reaching here with "off" is a wiring bug.
        raise ValueError("resolve_source called with resume_mode='off'")
    if mode == "explicit":
        resolved = _resolve_explicit(deployment, bots_path)
    elif mode == "latest":
        resolved = await _resolve_latest(new_instance_name, bots_path, bot_run_repo)
    else:
        raise ValueError(f"Unknown resume_mode: {mode!r}")

    logger.info(
        "Resume source resolved: mode=%s instance=%s origin=%s data_dir=%s",
        mode,
        resolved.instance_name,
        resolved.origin,
        resolved.data_dir,
    )
    return resolved


# ---------------------------------------------------------------------------
# explicit
# ---------------------------------------------------------------------------

def _resolve_explicit(deployment, bots_path: Path) -> ResolvedSource:
    """Resolve ``resume_mode == 'explicit'`` — operator names the exact instance."""
    name = deployment.resume_from
    if not name:
        # Belt-and-suspenders: model validation already enforces this.
        raise ResumeError(
            ResumeAbortReason.SOURCE_NOT_FOUND,
            "resume_mode='explicit' requires resume_from, but it was not set.",
        )

    if deployment.resume_from_archive:
        archive_base = bots_path / "archived" / name
        instance_dir = _resolve_archive_instance_dir(archive_base, name)
        if instance_dir is None:
            # Archive dir absent -> must call out that S3 archives are unsupported.
            raise ResumeError(
                ResumeAbortReason.SOURCE_NOT_FOUND,
                f"Resume source not found. Searched archived path: '{archive_base}'. "
                f"Note: only local-move archives can be resumed — S3-archived "
                f"sources are not supported (the repo has no download/extract path).",
            )
        data_dir = instance_dir / "data"
        if not data_dir.is_dir():
            raise ResumeError(
                ResumeAbortReason.SOURCE_NOT_FOUND,
                f"Resume source not found. Archive instance '{instance_dir}' has "
                f"no 'data/' directory to copy from.",
            )
        return ResolvedSource(
            instance_name=name,
            data_dir=data_dir,
            instance_dir=instance_dir,
            origin="archived",
        )

    # Non-archive: live instances tree.
    instance_dir = bots_path / "instances" / name
    data_dir = instance_dir / "data"
    if not data_dir.is_dir():
        archive_hint = bots_path / "archived" / name
        raise ResumeError(
            ResumeAbortReason.SOURCE_NOT_FOUND,
            f"Resume source not found. Searched: '{data_dir}'. "
            f"(resume_from_archive is false, so '{archive_hint}' was not searched.)",
        )
    return ResolvedSource(
        instance_name=name,
        data_dir=data_dir,
        instance_dir=instance_dir,
        origin="instances",
    )


# ---------------------------------------------------------------------------
# latest
# ---------------------------------------------------------------------------

async def _resolve_latest(
    new_instance_name: str,
    bots_path: Path,
    bot_run_repo,
) -> ResolvedSource:
    """Resolve ``resume_mode == 'latest'`` — newest prior run of the same bot.

    Lineage comes from Postgres (``bot_runs.instance_name``). Directory listing
    over ``bots/instances/`` is the fallback ONLY when the DB is unavailable.
    """
    target_base = _strip_api_suffix(new_instance_name)

    candidate_names, used_fallback = await _collect_latest_candidates(
        new_instance_name, target_base, bots_path, bot_run_repo
    )

    if not candidate_names:
        raise ResumeError(
            ResumeAbortReason.SOURCE_NOT_FOUND,
            f"No prior run found for base name '{target_base}' "
            f"(resume_mode='latest'). "
            f"{'Directory-listing fallback was used. ' if used_fallback else ''}"
            f"Deploy once, or use resume_mode='explicit'.",
        )

    winner = _pick_newest(candidate_names, target_base)

    instance_dir = bots_path / "instances" / winner
    data_dir = instance_dir / "data"
    if not data_dir.is_dir():
        raise ResumeError(
            ResumeAbortReason.SOURCE_NOT_FOUND,
            f"Resolved latest source '{winner}' but its data directory "
            f"'{data_dir}' is missing on disk.",
        )
    return ResolvedSource(
        instance_name=winner,
        data_dir=data_dir,
        instance_dir=instance_dir,
        origin="instances",
    )


async def _collect_latest_candidates(
    new_instance_name: str,
    target_base: str,
    bots_path: Path,
    bot_run_repo,
):
    """Return ``(candidate_names, used_fallback)``.

    Prefers DB lineage; falls back to directory listing ONLY when the DB call
    raises (unavailable). A DB call that succeeds with zero matches does NOT
    trigger the fallback — that is a legitimate "no prior run" result.
    """
    try:
        runs = await bot_run_repo.get_bot_runs(limit=1000)
        names = [
            r.instance_name
            for r in runs
            if getattr(r, "instance_name", None)
        ]
        candidates = _match_candidates(names, new_instance_name, target_base)
        return candidates, False
    except Exception as exc:  # DB unavailable -> directory fallback (same rules).
        logger.warning(
            "bot_runs lineage query failed (%s); falling back to directory "
            "listing over instances/ for latest resolution.",
            exc,
        )
        instances_dir = bots_path / "instances"
        names = []
        if instances_dir.is_dir():
            names = [d.name for d in instances_dir.iterdir() if d.is_dir()]
        candidates = _match_candidates(names, new_instance_name, target_base)
        return candidates, True


def _match_candidates(
    names,
    new_instance_name: str,
    target_base: str,
) -> List[str]:
    """Filter ``names`` to those whose own base matches ``target_base``
    byte-for-byte, excluding the instance being created.

    Duplicates are intentionally NOT collapsed: because the API name format is
    ``<base>-<ts>``, two distinct candidate strings sharing ``target_base`` must
    differ in their timestamp, so the only way two entries share the newest
    timestamp is a genuinely duplicated lineage row. That is ambiguous history
    and must fail closed (§5 "tie or conflicting lineage → LATEST_AMBIGUOUS"),
    so the duplicates are preserved for the tie check in :func:`_pick_newest`.
    """
    matched: List[str] = []
    for name in names:
        if name == new_instance_name:  # exclude self
            continue
        if _strip_api_suffix(name) == target_base:
            matched.append(name)
    return matched


def _pick_newest(candidate_names: List[str], target_base: str) -> str:
    """Pick the unique newest candidate by parsed API timestamp.

    A tie on the max timestamp (two runs stamped identically) is ambiguous
    lineage → ``LATEST_AMBIGUOUS`` (§5). Candidates without a parseable API
    suffix are unrankable and dropped defensively.
    """
    ranked = []
    for name in candidate_names:
        ts = _parse_api_timestamp(name)
        if ts is None:
            logger.warning(
                "latest candidate '%s' has no parseable API timestamp suffix; "
                "skipping it as unrankable.",
                name,
            )
            continue
        ranked.append((ts, name))

    if not ranked:
        # Every candidate matched the base but none carried a timestamp — treat
        # as no usable candidate (fail-closed).
        raise ResumeError(
            ResumeAbortReason.SOURCE_NOT_FOUND,
            f"Found names matching base '{target_base}' but none carried a "
            f"parseable timestamp suffix; cannot resolve 'latest'.",
        )

    max_ts = max(ts for ts, _ in ranked)
    top = [name for ts, name in ranked if ts == max_ts]
    if len(top) > 1:
        raise ResumeError(
            ResumeAbortReason.LATEST_AMBIGUOUS,
            f"Ambiguous 'latest' resolution for base '{target_base}': "
            f"{len(top)} candidates share the newest timestamp {top!r}. "
            f"Use resume_mode='explicit' to name the exact source.",
        )
    return top[0]


# ===========================================================================
# Phase 3 — Config-derived copy set + per-controller semantics (design §6)
# ===========================================================================
#
# The copy set is computed PER CONTROLLER in the new deploy, from the staged
# controller YAMLs — never from filename globs (§6, B3). This module only
# *computes* the plan (read-only inspection); the actual file copies run in the
# hook (Phase 5). Any fail-closed condition raises ``ResumeError`` here so the
# deploy aborts before a container exists.

# The engine key that identifies a range-ladder controller
# (controllers/market_making/conf_range_inventory_ladder.yml:2 -> controller_name).
RANGE_LADDER_CONTROLLER_NAME = "range_inventory_ladder"

# CopyItem.kind values that name a real file to physically copy (Phase 5 acts
# only on these). ``absolute_skipped`` is an audit marker, never copied.
_COPYABLE_KINDS = frozenset({"ledger", "owner", "sqlite", "sqlite_journal", "extra"})

# Never copied, regardless of source (§6): session-stamped diagnostics, atomic-
# write remnants, logs. Matched by ``fnmatch`` against the file *name*. The
# derived copy set only enumerates explicitly-named ledgers/owner sidecars plus
# an ``*.sqlite`` glob, so these mainly harden the sqlite glob and reverse scan.
_EXCLUDE_PATTERNS = (
    "*.diagnostic_*.jsonl",  # range_inventory_ladder.py:1487-1489 (session-stamped)
    "*.tmp",                 # interrupted mkstemp atomic-write remnants
    "*.log",                 # logs
)


def _is_excluded(name: str) -> bool:
    """True if a file name matches any never-copy pattern (§6)."""
    return any(fnmatch.fnmatch(name, pat) for pat in _EXCLUDE_PATTERNS)


def _is_absolute_state_file(name: str) -> bool:
    """True if a ``state_file_name`` escapes ``data/`` — i.e. it is absolute.

    Detects BOTH POSIX (``/...``) and Windows drive-letter / UNC roots
    regardless of the host OS, because prod is Linux and dev is Windows and
    ``pathlib.Path.is_absolute()`` only recognises the *host's* flavour. Mirrors
    the extra-paths guard in ``models/bot_orchestration.py``.
    """
    if not name:
        return False
    if name.startswith("/") or name.startswith("\\"):
        return True
    # Windows drive-letter absolute, e.g. ``C:\x`` or ``C:/x`` (or bare ``C:``).
    if len(name) >= 2 and name[1] == ":" and (len(name) == 2 or name[2] in ("/", "\\")):
        return True
    return Path(name).is_absolute()


@dataclass
class CopyItem:
    """One entry in a :class:`CopyPlan`.

    Attributes:
        src: Absolute source path (in the resolved source ``data/``). For an
            ``absolute_skipped`` marker this is the operator's absolute
            ``state_file_name`` (informational only).
        dst: Absolute destination path in the new instance's ``data/``, or
            ``None`` for a non-copy marker (``absolute_skipped``).
        controller_id: The owning controller id, or ``None`` for instance-level
            files (sqlite, extra paths).
        kind: One of ``ledger`` / ``owner`` / ``sqlite`` / ``sqlite_journal`` /
            ``extra`` (copyable), or ``absolute_skipped`` (audit marker).
    """

    src: Path
    dst: Optional[Path]
    controller_id: Optional[str]
    kind: str

    @property
    def copyable(self) -> bool:
        """True if Phase 5 should physically copy this item."""
        return self.dst is not None and self.kind in _COPYABLE_KINDS


@dataclass
class CopyPlan:
    """The config-derived copy set plus per-controller decisions and warnings.

    Attributes:
        items: Every planned item (copyable files + audit markers).
        decisions: ``controller_id -> "copied" | "fresh_seed" | "skipped"``.
        warnings: Loud, operator-facing warning strings (also logged at WARNING).
    """

    items: List[CopyItem] = field(default_factory=list)
    decisions: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def files_to_copy(self) -> List[CopyItem]:
        """The subset of ``items`` Phase 5 will actually copy."""
        return [it for it in self.items if it.copyable]

    def _warn(self, message: str) -> None:
        logger.warning(message)
        self.warnings.append(message)


# ---------------------------------------------------------------------------
# YAML helpers (staged controller configs + conf_client.yml)
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    """Load a YAML mapping, returning ``{}`` for an empty file.

    Raises the underlying error to the caller only where a parse failure is a
    meaningful abort; staged configs written by the API are well-formed.
    """
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


def _iter_staged_controllers(new_instance_dir: Path):
    """Yield ``(yaml_path, config_dict)`` for each staged controller YAML.

    Reads ``<new_instance>/conf/controllers/*.yml`` — exactly what the new bot
    will run (§4 note prefers the instance-staged copies). Non-mapping / broken
    files are skipped with a warning rather than aborting (they are not
    necessarily range-ladder controllers).
    """
    controllers_dir = new_instance_dir / "conf" / "controllers"
    if not controllers_dir.is_dir():
        return
    for yaml_path in sorted(controllers_dir.glob("*.yml")):
        try:
            config = _load_yaml(yaml_path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not parse staged controller '%s': %s", yaml_path, exc)
            continue
        if isinstance(config, dict):
            yield yaml_path, config


def _is_sqlite_deployment(new_instance_dir: Path) -> bool:
    """Parse the staged ``conf_client.yml`` and decide if the engine DB is sqlite.

    The engine default is ``DBSqliteMode`` (client_config_map.py:772), so a
    missing file / missing ``db_mode`` block is treated as sqlite. A Postgres
    (``DBOtherMode``) deployment carries ``db_mode.db_engine != "sqlite"`` and
    has no per-instance sqlite to carry (§15).
    """
    conf_client = new_instance_dir / "conf" / "conf_client.yml"
    if not conf_client.is_file():
        return True  # engine default is sqlite
    try:
        data = _load_yaml(conf_client)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not parse '%s' (%s); assuming engine sqlite default.", conf_client, exc)
        return True
    db_mode = data.get("db_mode")
    if not isinstance(db_mode, dict):
        return True  # default sqlite
    engine = db_mode.get("db_engine")
    return engine is None or engine == "sqlite"


# ---------------------------------------------------------------------------
# Per-controller ledger resolution
# ---------------------------------------------------------------------------

def _expected_ledger_name(config: dict) -> Optional[str]:
    """Return the expected state-file name for a controller config, or ``None``
    if ``state_file_name`` is absolute (escapes ``data/``, §6.2).

    Mirrors ``range_inventory_ladder.py:1471`` — ``state_file_name`` if set,
    else ``range_inventory_ladder_<id>.json``.
    """
    state_file_name = config.get("state_file_name")
    if state_file_name:
        return str(state_file_name)
    controller_id = config.get("id")
    return f"range_inventory_ladder_{controller_id}.json"


def _validate_ledger(src_ledger: Path) -> None:
    """Fail-closed if an expected ledger is zero-length or not parseable JSON.

    Never seed garbage — the engine would quarantine and re-seed from the wallet
    (the insufficient-funds bug this whole hook exists to prevent).
    """
    try:
        raw = src_ledger.read_bytes()
    except OSError as exc:
        raise ResumeError(
            ResumeAbortReason.LEDGER_INVALID,
            f"Expected ledger '{src_ledger}' could not be read: {exc}.",
        )
    if len(raw) == 0:
        raise ResumeError(
            ResumeAbortReason.LEDGER_INVALID,
            f"Expected ledger '{src_ledger}' is zero-length — refusing to seed garbage.",
        )
    try:
        json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResumeError(
            ResumeAbortReason.LEDGER_INVALID,
            f"Expected ledger '{src_ledger}' is not valid JSON ({exc}) — refusing to seed garbage.",
        )


def _read_owner_controller_id(owner_path: Path) -> str:
    """Return the ``controller_id`` from a ``.owner`` sidecar.

    Fail-closed (``OWNER_MISMATCH``) if the sidecar is unparseable or carries no
    ``controller_id`` — identity is unverifiable, and identity is what guards
    against copying the wrong controller's ledger (§6, range_inventory_ladder.py:2009).
    """
    try:
        marker = json.loads(owner_path.read_text(encoding="utf-8"))
        owner_id = marker.get("controller_id") if isinstance(marker, dict) else None
    except (OSError, ValueError) as exc:
        raise ResumeError(
            ResumeAbortReason.OWNER_MISMATCH,
            f"Owner sidecar '{owner_path}' is unparseable ({exc}); "
            f"controller identity cannot be verified — failing closed.",
        )
    if not owner_id:
        raise ResumeError(
            ResumeAbortReason.OWNER_MISMATCH,
            f"Owner sidecar '{owner_path}' carries no 'controller_id'; "
            f"controller identity cannot be verified — failing closed.",
        )
    return owner_id


def _plan_controller(config: dict, source: "ResolvedSource", new_data_dir: Path, plan: CopyPlan) -> Optional[str]:
    """Plan the copy for one range-ladder controller; mutate ``plan`` in place.

    Returns the expected ledger *filename* handled for this controller (so the
    reverse scan can skip it), or ``None`` when nothing on disk was named
    (absolute-skip or fresh-seed).
    """
    controller_id = config.get("id")
    state_file_name = config.get("state_file_name")

    # §6.2 — absolute state_file_name escapes data/: skip + loud warning.
    if state_file_name and _is_absolute_state_file(str(state_file_name)):
        plan.items.append(
            CopyItem(src=Path(str(state_file_name)), dst=None, controller_id=controller_id, kind="absolute_skipped")
        )
        plan.decisions[controller_id] = "skipped"
        plan._warn(
            f"Controller '{controller_id}': state_file_name '{state_file_name}' is an absolute path — "
            f"it escapes data/ and is not the resume hook's to manage (shared-mount scheme assumed). Skipping."
        )
        return None

    ledger_name = _expected_ledger_name(config)
    src_ledger = source.data_dir / ledger_name
    src_owner = source.data_dir / f"{ledger_name}.owner"

    # No ledger in source -> warn + fresh seed for THIS controller only (§6 table).
    if not src_ledger.exists():
        plan.decisions[controller_id] = "fresh_seed"
        plan._warn(
            f"Controller '{controller_id}': no ledger '{ledger_name}' in source '{source.data_dir}' — "
            f"fresh-seeding this controller (legitimate for a newly added controller)."
        )
        return ledger_name

    # Ledger present -> must be valid, else abort (never seed garbage).
    _validate_ledger(src_ledger)

    # Identity via .owner controller_id, never the filename (§6).
    if src_owner.exists():
        owner_id = _read_owner_controller_id(src_owner)
        if owner_id != controller_id:
            raise ResumeError(
                ResumeAbortReason.OWNER_MISMATCH,
                f"Owner mismatch for ledger '{src_ledger}': sidecar claims controller "
                f"'{owner_id}' but the deploy expects '{controller_id}'. Wrong ledger — failing closed.",
            )
        plan.items.append(
            CopyItem(src=src_owner, dst=new_data_dir / src_owner.name, controller_id=controller_id, kind="owner")
        )
    else:
        # No sidecar. A custom state_file_name carries no id in its name, so
        # identity is unverifiable -> fail closed. A default-named ledger embeds
        # the id in its filename -> warn + copy.
        if state_file_name:
            raise ResumeError(
                ResumeAbortReason.OWNER_MISMATCH,
                f"Ledger '{src_ledger}' has a custom state_file_name but no '.owner' sidecar; "
                f"controller identity cannot be verified — failing closed.",
            )
        plan._warn(
            f"Controller '{controller_id}': ledger '{ledger_name}' has no '.owner' sidecar; identity is "
            f"taken from the default filename (which embeds the id). Copying."
        )

    plan.items.append(
        CopyItem(src=src_ledger, dst=new_data_dir / ledger_name, controller_id=controller_id, kind="ledger")
    )
    plan.decisions[controller_id] = "copied"
    return ledger_name


# ---------------------------------------------------------------------------
# Reverse scan, sqlite, extra paths
# ---------------------------------------------------------------------------

def _source_ledger_controller_id(json_path: Path) -> Optional[str]:
    """Best-effort controller id for a source ``*.json`` — from its ``.owner``
    sidecar if present and parseable, else from a default ``range_inventory_
    ladder_<id>.json`` filename. Returns ``None`` if it is not identifiable as a
    ladder ledger. Never raises (this is logging-only classification)."""
    owner = Path(f"{json_path}.owner")
    if owner.exists():
        try:
            marker = json.loads(owner.read_text(encoding="utf-8"))
            if isinstance(marker, dict) and marker.get("controller_id"):
                return marker["controller_id"]
        except (OSError, ValueError):
            pass
    match = re.fullmatch(r"range_inventory_ladder_(.+)\.json", json_path.name)
    if match:
        return match.group(1)
    return None


def _scan_orphan_source_ledgers(source: "ResolvedSource", deployed_ids: set, handled_names: set, plan: CopyPlan) -> None:
    """Log source ledgers whose controller is not in the new deploy (§6 table: skip).

    Read-only classification: marks ``skipped`` decisions, never aborts.
    """
    if not source.data_dir.is_dir():
        return
    for json_path in sorted(source.data_dir.glob("*.json")):
        if json_path.name in handled_names or _is_excluded(json_path.name):
            continue
        if json_path.name == "resume.manifest.json":
            continue
        orphan_id = _source_ledger_controller_id(json_path)
        if orphan_id is None or orphan_id in deployed_ids:
            continue
        plan.decisions.setdefault(orphan_id, "skipped")
        logger.info(
            "Source ledger '%s' belongs to controller '%s' which is not in this deploy — skipping.",
            json_path.name,
            orphan_id,
        )


def _plan_sqlite(source: "ResolvedSource", new_data_dir: Path, plan: CopyPlan) -> None:
    """Add ``*.sqlite`` (+ ``-journal`` sidecars) from source ``data/`` for
    sqlite deployments only (§6.3). Postgres deployments add nothing here."""
    if not source.data_dir.is_dir():
        return
    for path in sorted(source.data_dir.iterdir()):
        if not path.is_file() or _is_excluded(path.name):
            continue
        if path.name.endswith(".sqlite"):
            plan.items.append(CopyItem(src=path, dst=new_data_dir / path.name, controller_id=None, kind="sqlite"))
        elif path.name.endswith(".sqlite-journal"):
            plan.items.append(
                CopyItem(src=path, dst=new_data_dir / path.name, controller_id=None, kind="sqlite_journal")
            )


def _plan_extra_paths(deployment, source: "ResolvedSource", new_data_dir: Path, plan: CopyPlan) -> None:
    """Add ``resume_extra_paths`` after a symlink-safe containment check (§6.4).

    Each must resolve (``Path.resolve()``) to a location WITHIN the source
    ``data/`` — else ``EXTRA_PATH_ESCAPE`` (covers a symlink pointing outside);
    and it must exist — else ``EXTRA_PATH_MISSING``.
    """
    extra_paths = getattr(deployment, "resume_extra_paths", None) or []
    if not extra_paths:
        return
    data_root = source.data_dir.resolve()
    for rel in extra_paths:
        candidate = (source.data_dir / rel)
        resolved = candidate.resolve()
        if not resolved.is_relative_to(data_root):
            raise ResumeError(
                ResumeAbortReason.EXTRA_PATH_ESCAPE,
                f"resume_extra_paths entry '{rel}' resolves to '{resolved}', outside source data/ "
                f"'{data_root}' — containment violation, failing closed.",
            )
        if not resolved.exists():
            raise ResumeError(
                ResumeAbortReason.EXTRA_PATH_MISSING,
                f"resume_extra_paths entry '{rel}' does not exist under source data/ '{data_root}'.",
            )
        plan.items.append(
            CopyItem(src=resolved, dst=new_data_dir / rel, controller_id=None, kind="extra")
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_copy_plan(new_instance_dir, source: "ResolvedSource", deployment) -> CopyPlan:
    """Compute the config-derived copy set for a resume (design §6).

    Read-only: inspects the staged controller YAMLs under
    ``<new_instance>/conf/controllers/`` and the resolved ``source`` ``data/`` to
    decide, per controller, what to copy — never a filename glob (B3). No files
    are written here; Phase 5 executes ``plan.files_to_copy``.

    Args:
        new_instance_dir: The new instance's root dir (its ``conf/`` is already
            staged; its ``data/`` is where files will land).
        source: The :class:`ResolvedSource` from :func:`resolve_source` (P2).
        deployment: The deploy model (reads ``resume_extra_paths``).

    Returns:
        A :class:`CopyPlan` (items, per-controller decisions, warnings).

    Raises:
        ResumeError: fail-closed on ``LEDGER_INVALID`` / ``OWNER_MISMATCH`` /
            ``EXTRA_PATH_ESCAPE`` / ``EXTRA_PATH_MISSING`` (§11).
    """
    new_instance_dir = Path(new_instance_dir)
    new_data_dir = new_instance_dir / "data"
    plan = CopyPlan()

    deployed_ids: set = set()
    handled_names: set = set()

    # Per-controller: range-ladder controllers in the new deploy (staged YAMLs).
    for _yaml_path, config in _iter_staged_controllers(new_instance_dir):
        if config.get("controller_name") != RANGE_LADDER_CONTROLLER_NAME:
            continue
        controller_id = config.get("id")
        if not controller_id:
            logger.warning("Staged range-ladder controller with no 'id' — skipping.")
            continue
        deployed_ids.add(controller_id)
        handled = _plan_controller(config, source, new_data_dir, plan)
        if handled:
            handled_names.add(handled)
            handled_names.add(f"{handled}.owner")

    # Source ledgers for controllers NOT in this deploy -> skipped (logged).
    _scan_orphan_source_ledgers(source, deployed_ids, handled_names, plan)

    # SQLite half — only for sqlite deployments.
    if _is_sqlite_deployment(new_instance_dir):
        _plan_sqlite(source, new_data_dir, plan)

    # resume_extra_paths (containment-validated within source data/).
    _plan_extra_paths(deployment, source, new_data_dir, plan)

    logger.info(
        "Copy plan for '%s' from '%s': %d file(s) to copy, decisions=%s, %d warning(s).",
        new_instance_dir.name,
        source.instance_name,
        len(plan.files_to_copy),
        plan.decisions,
        len(plan.warnings),
    )
    return plan


# ===========================================================================
# Phase 4 — Preconditions & guards, fail-closed (design §7)
# ===========================================================================
#
# ``run_guards`` runs the pre-copy preconditions from §7 against the resolved
# source and the (still-empty) destination ``data/``. It builds a
# :class:`GuardReport` recording each guard's pass/fail + message, and raises
# ``ResumeError`` on the FIRST hard failure so the deploy aborts before any
# container starts (§2.1). Ledger validity (§7.2) is enforced in the P3 copy
# plan, not here — the report merely records that.
#
# Guards use ONLY Docker container state (never PIDs — container-namespaced and
# meaningless to the API host, §2.2) and the API ``bot_runs`` history. No
# filesystem writes; the destination is inspected read-only.

# Docker container states that mean the source is *not* safely quiesced — an
# active writer could still be mutating the ledger, breaking the single-owner
# invariant (§2.2). Everything else (exited / created / dead / removing) is an
# acceptable stopped-or-absent source.
_ACTIVE_CONTAINER_STATES = frozenset({"running", "restarting", "paused"})

# API ``bot_runs.run_status`` value that denotes a clean, graceful stop
# (database/models.py:190 — CREATED / RUNNING / STOPPED / ERROR). Anything else
# (or a missing end marker / absent row) is treated as ungraceful (§7.5).
_GRACEFUL_RUN_STATUS = "STOPPED"

# Destination state-file globs whose presence means the new ``data/`` is not the
# pristine empty dir the hook expects (§7.4). ``*.owner`` catches the ledger
# sidecars (``<ledger>.json.owner``).
_DEST_STATE_GLOBS = ("*.json", "*.sqlite", "*.owner")


@dataclass
class GuardCheck:
    """One precondition result within a :class:`GuardReport`.

    Attributes:
        name: Stable machine name of the guard (e.g. ``"source_container"``).
        passed: Whether the guard passed.
        message: Human-readable detail (why it passed/failed, or a warning).
    """

    name: str
    passed: bool
    message: str


@dataclass
class GuardReport:
    """The result of running all §7 preconditions.

    On a hard failure ``run_guards`` raises ``ResumeError`` (with this partial
    report attached as ``err.guard_report``); on full success it returns a
    report whose ``passed`` is ``True``. Serialized into the resume manifest by
    Phase 5.

    Attributes:
        checks: Per-guard results, in evaluation order.
        warnings: Loud, operator-facing warnings (e.g. an accepted ungraceful
            source). Also emitted at WARNING level.
    """

    checks: List[GuardCheck] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True iff every recorded guard passed."""
        return all(c.passed for c in self.checks)

    def _record(self, name: str, passed: bool, message: str) -> None:
        self.checks.append(GuardCheck(name=name, passed=passed, message=message))
        (logger.info if passed else logger.warning)(
            "Resume guard '%s': %s — %s", name, "PASS" if passed else "FAIL", message
        )

    def _warn(self, message: str) -> None:
        logger.warning(message)
        self.warnings.append(message)

    def to_dict(self) -> dict:
        """A JSON-serializable summary for the resume manifest (§13)."""
        return {
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "message": c.message}
                for c in self.checks
            ],
            "warnings": list(self.warnings),
        }


def _abort_guard(reason: ResumeAbortReason, message: str, report: GuardReport):
    """Record a failed guard, attach the partial report to the error, raise.

    Centralises the fail-closed exit so every hard guard failure both lands in
    the report (for observability) and aborts before any container starts.
    """
    report._record(reason.name.lower(), False, message)
    err = ResumeError(reason, message)
    err.guard_report = report
    raise err


def _guard_source_container(source: "ResolvedSource", docker_client, report: GuardReport) -> None:
    """§7.1 — the source container must have exited.

    Looks up the container named EXACTLY ``source.instance_name`` via the Docker
    SDK. ``running`` / ``restarting`` / ``paused`` → ``SOURCE_RUNNING`` (covers
    the idle-after-stop-bot case, where the headless process still holds the
    ledger). ``NotFound`` → PASS: the container object is gone, so the
    directory-on-disk is an acceptable source. No PID / ``.owner``-liveness
    checks — Docker state is the only valid stopped-check (§2.2).
    """
    from docker.errors import NotFound

    name = source.instance_name
    try:
        container = docker_client.containers.get(name)
    except NotFound:
        report._record(
            "source_container",
            True,
            f"No container named '{name}' — source is a stopped/removed instance on disk.",
        )
        return

    status = getattr(container, "status", None)
    if status in _ACTIVE_CONTAINER_STATES:
        _abort_guard(
            ResumeAbortReason.SOURCE_RUNNING,
            f"Source container '{name}' is '{status}' — stop the container first "
            f"(a running or idle-after-stop-bot process still owns the ledger).",
            report,
        )
    report._record(
        "source_container",
        True,
        f"Source container '{name}' is '{status}' (not active) — safely quiesced.",
    )


def resolve_deploy_target(bots_path, instance_name: str) -> Path:
    """The instance directory a deploy of ``instance_name`` will create.

    The single source of truth for "where does this instance live", shared by
    the deploy path (``docker_service.create_hummingbot_instance``) and
    :func:`preview_resume`. The preview validating a path the deploy does not
    actually use is precisely the CLA-008 P1 defect, so both sides derive the
    target here rather than each re-joining the components themselves.
    """
    return Path(bots_path) / "instances" / instance_name


def guard_target_available(target_dir, report: Optional[GuardReport] = None) -> GuardReport:
    """§7.0 / CDX-001 — the target instance directory must not already exist.

    The exclusive-creation guard. A pre-existing target is NEVER reused and
    NEVER deleted: it may be a live instance, an archive, or an operator's tree,
    and its ``data/`` may hold the only copy of a ladder ledger. The deploy path
    runs this BEFORE it stages anything (so a colliding deploy does no work and
    touches nothing); :func:`preview_resume` runs the SAME function against real
    filesystem state so its answer is the deploy's answer.

    Args:
        target_dir: The instance directory from :func:`resolve_deploy_target`.
        report: The report to record into; a fresh one is created when the
            caller has none (the deploy path, which guards before any resume
            machinery exists — and runs even with ``resume_mode="off"``).

    Returns:
        The report, with ``target_available`` recorded as passed.

    Raises:
        ResumeError: ``DEST_EXISTS`` when the target already exists.
    """
    report = report if report is not None else GuardReport()
    target_dir = Path(target_dir)
    if target_dir.exists():
        _abort_guard(
            ResumeAbortReason.DEST_EXISTS,
            f"Target instance directory '{target_dir}' already exists — refusing "
            f"to reuse, overwrite or delete it. Deploy under a different instance "
            f"name; the existing directory is left exactly as it is.",
            report,
        )
    report._record(
        "target_available",
        True,
        f"Target '{target_dir}' does not exist — it will be created exclusively "
        f"by this deploy.",
    )
    return report


def _guard_destination_empty(new_data_dir: Path, report: GuardReport) -> None:
    """§7.4 — the destination ``data/`` must contain no state files.

    Any ``*.json`` / ``*.sqlite`` / ``*.owner`` present → ``DEST_NOT_EMPTY``. At
    the hook's attach point ``data/`` is freshly created and empty, but assert it
    so an unexpected pre-seed is never silently overwritten.
    """
    new_data_dir = Path(new_data_dir)
    if not new_data_dir.is_dir():
        report._record(
            "destination_empty",
            True,
            f"Destination '{new_data_dir}' does not exist yet — treated as empty.",
        )
        return

    stray: List[str] = []
    for pattern in _DEST_STATE_GLOBS:
        stray.extend(p.name for p in new_data_dir.glob(pattern) if p.is_file())
    if stray:
        _abort_guard(
            ResumeAbortReason.DEST_NOT_EMPTY,
            f"Destination '{new_data_dir}' already holds state file(s) "
            f"{sorted(set(stray))} — refusing to overwrite an unexpected seed.",
            report,
        )
    report._record(
        "destination_empty",
        True,
        f"Destination '{new_data_dir}' holds no state files — clean.",
    )


async def _latest_source_run(source: "ResolvedSource", bot_run_repo):
    """Return the source's most recent ``bot_runs`` row, or ``None``.

    Filters ``get_bot_runs`` (already ordered newest-first by ``deployed_at``,
    bot_run_repository.py:121) by ``instance_name`` — the repo has no
    instance-name filter, mirroring ``resolve_source``'s in-Python match. A repo
    error is swallowed to ``None`` (unknown history → treated as ungraceful).
    """
    try:
        runs = await bot_run_repo.get_bot_runs(limit=1000)
    except Exception as exc:  # DB unavailable → unknown history, fail-closed below.
        logger.warning(
            "bot_runs lookup for ungraceful-source guard failed (%s); "
            "treating source history as unknown (ungraceful).",
            exc,
        )
        return None
    for run in runs:
        if getattr(run, "instance_name", None) == source.instance_name:
            return run
    return None


async def _guard_ungraceful_source(
    source: "ResolvedSource", deployment, bot_run_repo, report: GuardReport
) -> None:
    """§7.5 — advisory: the source should have stopped gracefully.

    The source's most recent ``bot_runs`` row is graceful iff its ``run_status``
    is ``STOPPED`` AND it carries an end marker (``stopped_at``). A non-stopped /
    errored status, a missing end marker, or an absent row (unknown history) is
    ungraceful → ``UNGRACEFUL_SOURCE`` unless ``resume_accept_ungraceful=True``,
    in which case the guard PASSES with a loud warning recorded. Exchange
    open-order verification is the operator runbook's job, not the hook's.
    """
    accept = bool(getattr(deployment, "resume_accept_ungraceful", False))
    run = await _latest_source_run(source, bot_run_repo)

    if run is None:
        detail = (
            f"No bot_runs history for source '{source.instance_name}' — unknown "
            f"history is not graceful."
        )
        graceful = False
    else:
        status = getattr(run, "run_status", None)
        stopped_at = getattr(run, "stopped_at", None)
        graceful = status == _GRACEFUL_RUN_STATUS and stopped_at is not None
        detail = (
            f"Source '{source.instance_name}' last run_status={status!r}, "
            f"stopped_at={stopped_at!r}."
        )

    if graceful:
        report._record("graceful_source", True, f"Graceful stop confirmed. {detail}")
        return

    if accept:
        message = (
            f"UNGRACEFUL SOURCE ACCEPTED via resume_accept_ungraceful=True. {detail} "
            f"The hook carries the ledger, NOT order cleanup — ensure the exchange "
            f"was flat before deploying."
        )
        report._warn(message)
        report._record("graceful_source", True, message)
        return

    _abort_guard(
        ResumeAbortReason.UNGRACEFUL_SOURCE,
        f"{detail} Source did not stop gracefully — refusing to resume. Cancel any "
        f"open orders and set resume_accept_ungraceful=True to override.",
        report,
    )


async def run_guards(
    source: "ResolvedSource",
    new_data_dir,
    deployment,
    docker_client,
    bot_run_repo,
    target_dir=None,
) -> GuardReport:
    """Run the §7 preconditions, fail-closed, before any copy or container start.

    Evaluates the guards in §7 order and raises ``ResumeError`` on the FIRST
    hard failure (with the partial :class:`GuardReport` attached as
    ``err.guard_report``); returns a passing report when every guard clears.

    Guards:
        0. Target instance dir does not exist (§7.0 / CDX-001) — only when
           ``target_dir`` is given (see the arg).
        1. Source container exited (§7.1) — Docker state only.
        2. Ledger validity (§7.2) — enforced in the P3 copy plan; recorded here.
        3. Destination ``data/`` empty (§7.4).
        4. Source stopped gracefully (§7.5) — advisory, overridable.

    Args:
        source: The :class:`ResolvedSource` from :func:`resolve_source` (P2).
        new_data_dir: The new instance's ``data/`` directory (the copy target).
        deployment: The deploy model (reads ``resume_accept_ungraceful``).
        docker_client: The Docker SDK client (``client.containers.get``).
        bot_run_repo: The existing ``BotRunRepository`` (or a compatible mock).
        target_dir: The instance directory the deploy would create. When given,
            the §7.0 exclusive-creation guard runs first and lands in this
            report. Only :func:`preview_resume` passes it: on the real deploy
            path the guard has already run in ``create_hummingbot_instance``,
            before any staging, so re-running it here would be redundant (and
            would pass trivially — the deploy builds in a staging sibling, not
            at the target).

    Returns:
        A passing :class:`GuardReport`.

    Raises:
        ResumeError: on the first hard guard failure (§11).
    """
    report = GuardReport()

    # 0. Target instance dir is free (§7.0 / CDX-001) — preview only; see arg.
    if target_dir is not None:
        guard_target_available(target_dir, report)

    # 1. Source container has exited (§7.1).
    _guard_source_container(source, docker_client, report)

    # 2. Ledger validity (§7.2) is enforced by compute_copy_plan (P3); record it.
    report._record(
        "ledger_validity",
        True,
        "Ledger existence/JSON validity is enforced fail-closed during copy-plan "
        "computation (compute_copy_plan, LEDGER_INVALID).",
    )

    # 3. Destination data/ contains no state files (§7.4).
    _guard_destination_empty(new_data_dir, report)

    # 4. Source stopped gracefully (§7.5), overridable via resume_accept_ungraceful.
    await _guard_ungraceful_source(source, deployment, bot_run_repo, report)

    logger.info(
        "Resume guards passed for source '%s' → dest '%s' (%d checks, %d warning(s)).",
        source.instance_name,
        new_data_dir,
        len(report.checks),
        len(report.warnings),
    )
    return report


# ===========================================================================
# Phase 5 — Hook orchestration (design §3/§4, §10.6, §12, §13)
# ===========================================================================
#
# ``seed_resume_state`` is the single entry point wired into
# ``docker_service.create_hummingbot_instance``: it runs after config staging
# and strictly before ``containers.run``, and chains the P2/P4/P3 pieces:
# resolve → guards → copy plan → file-level copies → config-drift diff →
# ``data/resume.manifest.json`` → structured events. On any ``ResumeError`` it
# logs ``bot_resume_failed``, best-effort removes the just-created instance
# dir, and re-raises so the deploy fails loudly BEFORE a container exists —
# no half-seeded instance may ever start (§2.1, §11).

# Sentinel for a field present on only one side of the config-drift diff (§12).
_DRIFT_ABSENT = "<absent>"


def _manifest_file_entry(path: Path, new_data_root: Path) -> dict:
    """Manifest entry for one copied file: name (relative to ``data/``), size,
    sha256. Size and hash come from a single read so they describe the same
    bytes."""
    raw = path.read_bytes()
    try:
        rel = path.resolve().relative_to(new_data_root)
    except ValueError:  # pragma: no cover - defensive; dst containment is guarded
        rel = Path(path.name)
    return {
        "name": rel.as_posix(),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _execute_copy_plan(plan: CopyPlan, new_data_dir: Path) -> List[dict]:
    """Execute the copyable items of a :class:`CopyPlan`, file-level.

    Uses ``shutil.copy2`` (``copytree`` for a directory-valued extra path) with
    a containment guard mirroring ``docker_service._ensure_contained``: every
    destination must resolve within the new instance's ``data/``. Any I/O
    failure aborts fail-closed (``COPY_IO_ERROR``) so the container never
    starts on a partial seed.

    Returns the manifest ``files`` entries ({name, size, sha256}) for every
    file that landed in ``data/``.
    """
    new_data_dir = Path(new_data_dir)
    new_data_root = new_data_dir.resolve()
    entries: List[dict] = []

    for item in plan.files_to_copy:
        src = Path(item.src)
        dst = Path(item.dst)
        if not dst.resolve().is_relative_to(new_data_root):
            raise ResumeError(
                ResumeAbortReason.EXTRA_PATH_ESCAPE,
                f"Copy destination '{dst}' resolves outside the new data/ dir "
                f"'{new_data_root}' — containment violation, failing closed.",
            )
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                for copied in sorted(p for p in dst.rglob("*") if p.is_file()):
                    entries.append(_manifest_file_entry(copied, new_data_root))
            else:
                shutil.copy2(src, dst)
                entries.append(_manifest_file_entry(dst, new_data_root))
        except (OSError, shutil.Error) as exc:
            raise ResumeError(
                ResumeAbortReason.COPY_IO_ERROR,
                f"I/O error copying '{src}' -> '{dst}': {exc}. Aborting before "
                f"container start (no half-seeded run).",
            )
    return entries


def _diff_controller_configs(source: "ResolvedSource", new_instance_dir: Path) -> List[dict]:
    """Config-drift diff (§12): source instance's controller YAMLs vs the
    newly staged ones, field-level.

    Live edits made while the old bot ran exist only in the SOURCE instance's
    ``conf/controllers/``; a redeploy stages from the shared template. Any
    difference is warned loudly and recorded in the manifest — the template
    WINS (no carry-forward). A controller file absent from the source (newly
    added controller) has nothing to drift against and is skipped.
    """
    drift: List[dict] = []
    new_controllers_dir = new_instance_dir / "conf" / "controllers"
    src_controllers_dir = source.instance_dir / "conf" / "controllers"
    if not new_controllers_dir.is_dir():
        return drift

    for staged in sorted(new_controllers_dir.glob("*.yml")):
        src_file = src_controllers_dir / staged.name
        if not src_file.is_file():
            continue
        try:
            src_cfg = _load_yaml(src_file)
            new_cfg = _load_yaml(staged)
        except Exception as exc:
            logger.warning(
                "Config-drift diff skipped for '%s': unparseable YAML (%s).",
                staged.name, exc,
            )
            continue
        if not isinstance(src_cfg, dict) or not isinstance(new_cfg, dict):
            continue

        fields = []
        for key in sorted(set(src_cfg) | set(new_cfg)):
            source_value = src_cfg.get(key, _DRIFT_ABSENT)
            template_value = new_cfg.get(key, _DRIFT_ABSENT)
            if source_value != template_value:
                fields.append(
                    {"field": key, "source": source_value, "template": template_value}
                )
        if fields:
            entry = {
                "file": staged.name,
                "controller_id": new_cfg.get("id"),
                "fields": fields,
            }
            drift.append(entry)
            logger.warning(
                "Config drift for controller '%s' (%s): source instance's YAML differs "
                "from the staged template on field(s) %s — the template wins (no "
                "carry-forward). The resumed bot runs the TEMPLATE parameters, which "
                "may differ from what the stopped bot was running.",
                entry["controller_id"],
                staged.name,
                [f["field"] for f in fields],
            )
    return drift


def _resume_one_liner(new_name: str, source_name: str, files: List[dict], plan: CopyPlan, drift: List[dict]) -> str:
    """The §13 one-line summary log."""
    copied = ", ".join(f"{f['name']} ({f['size']} B)" for f in files) or "nothing"
    fresh = sum(1 for d in plan.decisions.values() if d == "fresh_seed")
    fresh_part = f"; {fresh} controller(s) fresh-seeded" if fresh else ""
    if drift:
        drift_part = (
            f"{sum(len(d['fields']) for d in drift)} field(s) across "
            f"{len(drift)} file(s)"
        )
    else:
        drift_part = "none"
    return (
        f"Resumed {new_name} from {source_name}: copied {copied}"
        f"{fresh_part}; config drift: {drift_part}."
    )


def _cleanup_failed_instance(new_instance_dir: Path, created_by_this_attempt: bool) -> None:
    """Best-effort removal of the dir THIS ATTEMPT created, after a failed
    resume, so no half-seeded instance is left behind for a later deploy (or
    operator) to trip over. A cleanup failure is logged, never raised — the
    original ``ResumeError`` is what must surface.

    ``created_by_this_attempt`` is the CDX-001 interlock. This function used to
    ``rmtree`` whatever directory it was handed; combined with the old
    reuse-an-existing-instance-dir path in ``create_hummingbot_instance``, a
    failed resume onto an existing name deleted the operator's existing
    instance — ledger, sqlite and all. It may now only remove a directory the
    caller exclusively created for this attempt (the staging sibling). Default-
    deny: without an explicit ownership assertion nothing is deleted, because a
    directory this process did not create may be the only copy of a ledger.
    """
    if not created_by_this_attempt:
        logger.warning(
            "Not removing instance dir '%s' after resume failure: this attempt "
            "did not create it, and a directory we did not create is never "
            "deleted (CDX-001). Remove it by hand if it is a leftover.",
            new_instance_dir,
        )
        return
    try:
        if new_instance_dir.exists():
            shutil.rmtree(new_instance_dir)
            logger.info(
                "Removed half-created instance dir '%s' after resume failure.",
                new_instance_dir,
            )
    except OSError as exc:
        logger.error(
            "Could not clean up instance dir '%s' after resume failure: %s",
            new_instance_dir, exc,
        )


def _log_resume_failed(instance_name: str, reason: str, detail: str) -> None:
    logger.error(
        "bot_resume_failed: instance=%s reason=%s — %s",
        instance_name,
        reason,
        detail,
        extra={
            "event": "bot_resume_failed",
            "resume_abort_reason": reason,
            "resume_instance": instance_name,
        },
    )


async def _seed(
    deployment,
    new_instance_dir: Path,
    bots_path: Path,
    docker_client,
    bot_run_repo,
    new_instance_name: str,
) -> dict:
    """The hook body: resolve → guards → plan → copy → drift → manifest → events.

    ``new_instance_name`` is the instance's LOGICAL name and is deliberately not
    derived from ``new_instance_dir.name``: the deploy path builds in a staging
    sibling (``<name>.staging-<rand>``, CDX-001), so the directory name is not
    the instance name. Using the directory name for identity would make
    ``latest`` strip the wrong base and find no lineage at all.
    """
    from datetime import datetime, timezone

    new_data_dir = new_instance_dir / "data"

    # 1. Which prior instance to copy from (P2, §5).
    source = await resolve_source(deployment, new_instance_name, bots_path, bot_run_repo)

    # 2. Fail-closed preconditions (P4, §7).
    guard_report = await run_guards(source, new_data_dir, deployment, docker_client, bot_run_repo)

    # 3. Config-derived copy set (P3, §6).
    plan = compute_copy_plan(new_instance_dir, source, deployment)

    # 4. Execute the copies, containment-guarded, fail-closed on I/O error.
    files = _execute_copy_plan(plan, new_data_dir)

    # 5. Config-drift diff (§12) — warn + record; template wins.
    drift = _diff_controller_configs(source, new_instance_dir)

    # 6. Audit manifest (§13) — also the double-resume detector: it lands in
    #    data/ and trips the DEST_NOT_EMPTY guard of any later re-seed attempt.
    manifest = {
        "source_instance": source.instance_name,
        "source_path": str(source.data_dir),
        "mode": deployment.resume_mode,
        "files": files,
        "decisions": dict(plan.decisions),
        "drift": drift,
        "guard_report": guard_report.to_dict(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    new_data_dir.mkdir(parents=True, exist_ok=True)
    (new_data_dir / "resume.manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    # 7. Structured events + the §13 one-liner.
    decision_summary = {
        decision: sum(1 for d in plan.decisions.values() if d == decision)
        for decision in sorted(set(plan.decisions.values()))
    }
    logger.info(
        "bot_resume_seeded: instance=%s source=%s files=%d decisions=%s",
        new_instance_name,
        source.instance_name,
        len(files),
        decision_summary,
        extra={
            "event": "bot_resume_seeded",
            "resume_instance": new_instance_name,
            "resume_source": source.instance_name,
            "resume_file_count": len(files),
            "resume_decisions": decision_summary,
        },
    )
    logger.info(_resume_one_liner(new_instance_name, source.instance_name, files, plan, drift))
    return manifest


async def seed_resume_state(
    deployment,
    new_instance_dir,
    bots_path,
    docker_client,
    db_manager=None,
    bot_run_repo=None,
    new_instance_name: Optional[str] = None,
    created_by_this_attempt: bool = False,
) -> dict:
    """Seed a new instance's ``data/`` from a prior run — the copy-forward hook.

    Called by ``docker_service.create_hummingbot_instance`` after config
    staging and strictly before ``containers.run``, gated on
    ``deployment.resume_mode != "off"`` (when off, this function is never
    invoked and the deploy path is byte-identical to pre-hook behavior).

    Args:
        deployment: The deploy model (``V2ControllerDeployment`` /
            ``V2ScriptDeployment``) carrying the resume fields.
        new_instance_dir: The just-created instance root (its ``conf/`` is
            staged, its ``data/`` empty).
        bots_path: The ``bots/`` directory containing ``instances/`` and
            ``archived/``.
        docker_client: The Docker SDK client (container-state guard).
        db_manager: The app's ``AsyncDatabaseManager``; when given (and no
            ``bot_run_repo``), a session is opened around the seeding and the
            existing ``BotRunRepository`` is used for lineage/guard queries.
        bot_run_repo: A ready-made repository (tests / preview flows). ``None``
            with no ``db_manager`` degrades per design: ``latest`` falls back
            to directory listing, source history counts as ungraceful.
        new_instance_name: The instance's LOGICAL name. Defaults to
            ``new_instance_dir.name``, which is correct only when the directory
            is named after the instance. The deploy path builds in a staging
            sibling (CDX-001) and so passes the real name explicitly — identity
            (``latest`` base-stripping, exclude-self, the manifest, the events)
            must never come from the staging directory's name.
        created_by_this_attempt: Whether the CALLER exclusively created
            ``new_instance_dir`` for this deploy. Only then may a failure clean
            it up. Defaults to ``False`` — a directory this attempt did not
            create is never deleted (CDX-001), because its ``data/`` may be the
            operator's only copy of a ledger.

    Returns:
        The resume manifest dict (also written to ``data/resume.manifest.json``).

    Raises:
        ResumeError: fail-closed on any §11 condition — after logging
            ``bot_resume_failed`` and (when this attempt created it) removing
            the instance dir, so the deploy aborts loudly and no container ever
            starts.
    """
    new_instance_dir = Path(new_instance_dir)
    bots_path = Path(bots_path)
    if new_instance_name is None:
        new_instance_name = new_instance_dir.name
    try:
        if bot_run_repo is None and db_manager is not None:
            from database import BotRunRepository

            async with db_manager.get_session_context() as session:
                return await _seed(
                    deployment, new_instance_dir, bots_path, docker_client,
                    BotRunRepository(session), new_instance_name,
                )
        return await _seed(
            deployment, new_instance_dir, bots_path, docker_client, bot_run_repo,
            new_instance_name,
        )
    except ResumeError as err:
        _log_resume_failed(new_instance_name, err.reason.value, err.message)
        _cleanup_failed_instance(new_instance_dir, created_by_this_attempt)
        raise
    except Exception as exc:
        # Unexpected failures get the same fail-closed treatment: event,
        # cleanup, re-raise — the deploy must never continue to containers.run.
        _log_resume_failed(new_instance_name, f"UNEXPECTED:{type(exc).__name__}", str(exc))
        _cleanup_failed_instance(new_instance_dir, created_by_this_attempt)
        raise


# ===========================================================================
# Phase 6 — Preview / dry-run (design §5 dry-run note, R6)
# ===========================================================================
#
# ``preview_resume`` is the backend for the read-only
# ``POST /bot-orchestration/deploy-v2-controllers/resume-preview`` endpoint.
# It stages the template controller YAMLs in a temporary directory and runs
# resolve → guards → copy-plan without creating any instance directory, copying
# any files, or touching Docker beyond the single container-state read done by
# the source-container guard. The returned dict is what the router serialises
# as the 200 response.
#
# CLA-008 P1: the preview used to run its guards against that temporary
# directory, so the destination checks graded a path the deploy would never
# write to — an empty temp dir always looks clean, so DEST_NOT_EMPTY could not
# fail and the preview reported a PASS it had not earned. The destination guards
# now run against the real ``bots/instances/<target>`` path, resolved with the
# deploy's own helpers. The temp dir survives for one honest reason: computing
# the copy plan requires the staged controller YAMLs somewhere, and the preview
# must not create anything in the bot tree.


def _reroot(path, old_root: Path, new_root: Path) -> Path:
    """Re-express ``path`` (under ``old_root``) as the same relative path under
    ``new_root`` — used to report the preview's planned destinations at their
    real target paths instead of the temp dir the plan was computed in."""
    return Path(new_root) / Path(path).relative_to(Path(old_root))


def _preview_base_name_collision(bots_path: Path, base_name: str, report: GuardReport) -> None:
    """Report — never abort on — an existing instance dir at the operator's bare
    base name.

    The deploy endpoint appends a unique suffix, so this can NOT make that
    deploy collide, and treating it as fatal would refuse perfectly good
    redeploys of a familiar name. It is still real, observable state that the
    preview is uniquely placed to surface: a caller that deploys this exact name
    directly (bypassing the router's name generation) is the one case that
    would be refused ``DEST_EXISTS``.
    """
    base_dir = resolve_deploy_target(bots_path, base_name)
    if base_dir.exists():
        report._warn(
            f"An instance directory already exists at '{base_dir}' for the bare "
            f"base name '{base_name}'. The deploy endpoint appends a unique "
            f"suffix, so this deploy will NOT collide with it; a deploy of this "
            f"exact name would be refused (DEST_EXISTS). Nothing was touched."
        )


async def preview_resume(
    deployment,
    bots_path,
    docker_client,
    db_manager=None,
    bot_run_repo=None,
) -> dict:
    """Read-only preview of what a resume deploy would copy.

    Stages the deploy's template controller YAMLs in a temporary directory
    (which is cleaned up automatically), runs ``resolve_source`` → ``run_guards``
    → ``compute_copy_plan``, and returns the plan as a JSON-serialisable dict.
    No directories are created in the bot tree; no files are copied; Docker is
    touched only for the container-state guard (a single read-only API call).

    CLA-008 P1 — what makes this a genuine pre-deploy check rather than a
    plausible-looking one:

    * The candidate target is minted with the deploy's own
      :func:`generate_instance_name` and located with the deploy's own
      :func:`resolve_deploy_target`, so preview and deploy cannot disagree
      about where the instance goes.
    * The destination guards (§7.0 ``DEST_EXISTS``, §7.4 ``DEST_NOT_EMPTY``)
      run against that REAL path via the same :func:`run_guards` /
      :func:`guard_target_available` the deploy runs — not a reimplementation,
      and not against the temp dir, which was always empty and therefore always
      passed.
    * ``latest`` resolution uses the fully-stamped candidate name, so it strips
      the same base the deploy will.

    What it still cannot do, and does not claim: reserve the name. The deploy
    mints a fresh one (new stamp, new entropy) when it runs. The response's
    ``target`` block says so via ``name_is_representative``. A live dry-run
    against a real stopped instance remains the only thing that closes the
    mock/reality gap.

    Args:
        deployment: The deploy model (``V2ControllerDeployment`` /
            ``V2ScriptDeployment``) with resume fields.
        bots_path: The ``bots/`` directory (same convention as
            ``seed_resume_state`` — e.g. ``Path("bots")`` relative to CWD).
        docker_client: The Docker SDK client (container-state lookup only).
        db_manager: ``AsyncDatabaseManager`` (optional); a session is opened
            when no ``bot_run_repo`` is provided. ``None`` degrades gracefully:
            ``latest`` falls back to directory listing, unknown source history
            is treated as ungraceful (same semantics as ``seed_resume_state``).
        bot_run_repo: A ready-made repository (tests / callers that already
            hold a session). Takes precedence over ``db_manager``.

    Returns:
        Dict with keys: ``resolved_source``, ``target`` (the resolved target
        path + the representative name it was checked under), ``files`` (with
        ``dst`` at real target paths), ``decisions``, ``guard_report``,
        ``would_succeed`` (always ``True`` — failures raise).

    Raises:
        ResumeError: fail-closed on any §11 condition (caller maps to HTTP 409).
    """
    import tempfile

    bots_path = Path(bots_path)

    # CLA-008 P1 — the target the DEPLOY would use, derived with the deploy's own
    # helpers (``generate_instance_name`` + ``resolve_deploy_target``), not a
    # lookalike. The name carries a fresh stamp + entropy exactly as a deploy
    # launched right now would: it is representative, not predictive (see the
    # ``target`` block of the response). What matters is that everything below
    # this line is measured against the REAL bots/instances/ tree instead of the
    # temp directory the preview used to validate — which always looked clean and
    # so could never surface a collision.
    base_name = deployment.instance_name
    candidate_name = generate_instance_name(base_name)
    target_dir = resolve_deploy_target(bots_path, candidate_name)

    # Normalise controller filenames: the model stores them without ``.yml``
    # but the template files on disk have ``.yml``.
    ctrl_names = []
    for name in (getattr(deployment, "controllers_config", None) or []):
        ctrl_names.append(name if name.endswith(".yml") else f"{name}.yml")

    with tempfile.TemporaryDirectory() as _tmpdir:
        tmp_instance_dir = Path(_tmpdir)

        # Stage template controller YAMLs so compute_copy_plan can read them.
        controllers_dst = tmp_instance_dir / "conf" / "controllers"
        controllers_dst.mkdir(parents=True)
        for ctrl_name in ctrl_names:
            src = bots_path / "conf" / "controllers" / ctrl_name
            if src.is_file():
                shutil.copy2(src, controllers_dst / ctrl_name)
            else:
                logger.warning(
                    "preview_resume: template '%s' not found at '%s' — "
                    "controller skipped in copy plan.",
                    ctrl_name, src,
                )

        # Stage conf_client.yml for the sqlite-mode check.
        credentials_profile = getattr(deployment, "credentials_profile", None)
        if credentials_profile:
            src_client = (
                bots_path / "credentials" / credentials_profile / "conf_client.yml"
            )
            if src_client.is_file():
                shutil.copy2(src_client, tmp_instance_dir / "conf" / "conf_client.yml")

        # Run the pipeline against the REAL target. ``candidate_name`` is the
        # fully-stamped name (what the deploy resolves lineage with), so
        # ``latest`` strips the same base and excludes self exactly as the deploy
        # will — the old code passed the pre-stamp name here, which stripped a
        # DIFFERENT base whenever the operator's name itself ended in a stamp.
        async def _preview_run(repo):
            source = await resolve_source(deployment, candidate_name, bots_path, repo)
            # Guards run against the real target dir and its real ``data/``:
            # ``target_dir`` adds the §7.0 exclusive-creation check (DEST_EXISTS),
            # and DEST_NOT_EMPTY now inspects the path the deploy would actually
            # write to. Neither creates anything — both are read-only stats.
            guard_report = await run_guards(
                source,
                target_dir / "data",
                deployment,
                docker_client,
                repo,
                target_dir=target_dir,
            )
            _preview_base_name_collision(bots_path, base_name, guard_report)
            plan = compute_copy_plan(tmp_instance_dir, source, deployment)
            return source, guard_report, plan

        if bot_run_repo is None and db_manager is not None:
            from database import BotRunRepository

            async with db_manager.get_session_context() as session:
                source, guard_report, plan = await _preview_run(BotRunRepository(session))
        else:
            source, guard_report, plan = await _preview_run(bot_run_repo)

        # Build the files list from the planned (not executed) copy set.
        # Sizes and sha256 are computed from the SOURCE files — no copy occurs.
        # ``dst`` is re-rooted from the throwaway staging temp dir onto the real
        # target, so the operator reads the path the file would land at rather
        # than a temp path that will not exist a millisecond from now.
        files = []
        for item in plan.files_to_copy:
            entry: dict = {
                "src": str(item.src),
                "dst": str(_reroot(item.dst, tmp_instance_dir, target_dir)),
                "kind": item.kind,
            }
            try:
                raw = Path(item.src).read_bytes()
                entry["size"] = len(raw)
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
            except OSError:
                pass  # best-effort; guard already validated the ledger
            files.append(entry)

        return {
            "resolved_source": {
                "instance_name": source.instance_name,
                "data_dir": str(source.data_dir),
                "origin": source.origin,
            },
            "target": {
                "base_name": base_name,
                "instance_name": candidate_name,
                "path": str(target_dir),
                # Honesty, not a disclaimer: the deploy mints its own name (with
                # fresh entropy) when it runs, so this exact name is not the one
                # that will exist. The guards above ran against the real tree
                # under this name's real path; what they cannot do is reserve it.
                "name_is_representative": True,
                "note": (
                    "The deploy generates a fresh unique instance name at deploy "
                    "time, so the final name will differ in its timestamp and "
                    "random suffix. Guards above were evaluated against the real "
                    "bots/instances/ tree."
                ),
            },
            "files": files,
            "decisions": dict(plan.decisions),
            "guard_report": guard_report.to_dict(),
            "would_succeed": True,
        }
