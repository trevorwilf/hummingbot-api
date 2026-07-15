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

import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

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
