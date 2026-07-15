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
import json
import logging
import re
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

# The API's own instance-name suffix: "<name>-YYYYMMDD-HHMMSS"
# (routers/bot_orchestration.py:501 — datetime.strftime("%Y%m%d-%H%M%S")).
# Anchored at the END and applied exactly ONCE, so operator names that embed
# their own timestamp-like tokens (e.g. "KRAKEN_LADDER_V1-20260712-2302") are
# never double-stripped.
_API_SUFFIX_RE = re.compile(r"-(\d{8}-\d{6})$")
_API_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


def _strip_api_suffix(instance_name: str) -> str:
    """Return the logical base name = instance name with ONLY the final
    ``-YYYYMMDD-HHMMSS`` suffix removed (single application, anchored at end)."""
    return _API_SUFFIX_RE.sub("", instance_name, count=1)


def _parse_api_timestamp(instance_name: str):
    """Parse the datetime from the final API suffix, or ``None`` if absent.

    Uses ``datetime.strptime`` (not mtime — archiving/backup perturbs mtime).
    Import is local so the module has no import-time ``datetime`` dependency
    beyond what it uses, and to keep the top of the file about types only.
    """
    from datetime import datetime

    match = _API_SUFFIX_RE.search(instance_name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), _API_TIMESTAMP_FORMAT)
    except ValueError:
        return None


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
