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
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from database.repositories.bot_run_repository import (
    RETIREMENT_VERIFIED,
    missing_retirement_evidence,
)
from services.controller_id_contract import classify_controller_id
from services.ledger_envelope_contract import classify_ledger_envelope
from services.state_file_contract import (
    ALLOW_ABSOLUTE_FIELD,
    StateFileStatus,
    classify_state_file_name,
)

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
    # CONTRACT C1 (CDX-007/CLA-004): a state_file_name outside the C1 accept set.
    # Added after the up-front declaration below because the C1 api half had no
    # abort reason at all — the violating deploy used to SUCCEED (skip-and-proceed).
    STATE_FILE_PATH_INVALID = "STATE_FILE_PATH_INVALID"
    # CONTRACT C2 (CDX-008/CLA-002): a staged range-ladder controller id outside
    # the C2 accept set. Added for the same reason as C1's above — the violating
    # deploy used to SUCCEED, skipping the controller whose ledger it could not
    # name and letting the bot re-seed from the wallet.
    CONTROLLER_ID_INVALID = "CONTROLLER_ID_INVALID"
    EXTRA_PATH_ESCAPE = "EXTRA_PATH_ESCAPE"
    EXTRA_PATH_MISSING = "EXTRA_PATH_MISSING"
    COPY_IO_ERROR = "COPY_IO_ERROR"
    # CDX-015: a staged 'db_engine' outside the known set. The old code compared
    # it to "sqlite" exactly and treated EVERY other value — including a typo or
    # a casing variant like "SQLite" — as a live Postgres deployment with no
    # per-instance sqlite to carry, silently dropping the source DB from the copy
    # set. An unrecognised engine is now an abort, never a guess.
    DB_ENGINE_UNKNOWN = "DB_ENGINE_UNKNOWN"
    # CLA-008 P2: the source container's state could not be verified because the
    # Docker client itself failed (API error, daemon unreachable). Distinct from
    # SOURCE_RUNNING (verified active) and from NotFound (verified absent): here
    # we know nothing, so we refuse rather than proceed on an unverified source.
    SOURCE_STATE_UNVERIFIED = "SOURCE_STATE_UNVERIFIED"
    # CLA-M02: the API's own bots/ write root and the host directory the bot
    # containers bind-mount are PROVEN to be different directories. The hook
    # would seed a data/ the bot can never read. Raised only on proof (a
    # successful self-inspection that disagrees), never on an unverified guess —
    # see ``DockerService._check_bots_path_coupling``.
    BOTS_PATH_MISCONFIGURED = "BOTS_PATH_MISCONFIGURED"
    # CTRLRESUME P1: a controller config yml carried a top-level ``resume_mode``
    # whose value is neither ``"latest"`` nor ``"off"``, or whose flag could not
    # be safely stripped out of the staged copy the engine loads (the engine
    # sets ``extra="forbid"``, so an unknown key fails its config load). Both are
    # fail-closed refusals of the deploy — the value is never guessed and the key
    # is never allowed to reach the engine unstripped.
    RESUME_FLAG_INVALID = "RESUME_FLAG_INVALID"
    # CTRLRESUME P2: two or more latest-flagged controllers resolved to DIFFERENT
    # newest source instances. A flag-driven resume copies from exactly ONE prior
    # instance (the sqlite half is whole-instance), so divergent winners mean the
    # controllers' history is split across instances and ANY single pick would
    # silently resume older/foreign state for the other controller(s). The message
    # names each controller and its winning instance so the operator can settle it
    # with a request-level resume_mode='explicit'. A NEW member — never overload
    # an existing reason for this.
    CONTROLLER_SOURCES_DIVERGENT = "CONTROLLER_SOURCES_DIVERGENT"


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
# Controller-level resume flag (CTRLRESUME P1 — §1 "The flag" / §2 "Strip at
# staging"). ONE shared parser, TWO call sites: the staging strip in
# ``DockerService`` (this phase) and the resume-preview endpoint reading SOURCE
# ymls (phase 3). Both go through ``parse_controller_resume_flag`` so the flag a
# preview reports and the flag a deploy acts on can never drift.
# ---------------------------------------------------------------------------

CONTROLLER_RESUME_MODE_KEY = "resume_mode"

# A top-level ``resume_mode:`` key (column 0, no leading indentation), matched
# against a single physical line. A nested/indented ``resume_mode:`` or a
# ``resume_mode_*`` key does NOT match — only the exact top-level key is a flag.
_TOP_LEVEL_RESUME_MODE_LINE = re.compile(r"^resume_mode[ \t]*:")

# Sentinel: no top-level ``resume_mode`` key at all (or the document is not a
# top-level mapping). Distinct from a present-but-invalid value.
_FLAG_ABSENT = object()


@dataclass(frozen=True)
class ControllerResumeFlag:
    """The parsed, validated controller-level ``resume_mode`` flag.

    Attributes:
        present: Whether the SOURCE yml carried a top-level ``resume_mode`` key
            at all. Drives staging: a present key is stripped (the engine's
            ``extra="forbid"`` rejects it), an absent key means a plain
            byte-identical copy with the strip filter never touching the file.
        mode: The validated flag — ``"latest"`` or ``"off"``. ``"off"`` also
            stands for an absent key, so callers can branch on ``mode`` alone.
    """

    present: bool
    mode: str


def _top_level_resume_mode_spelling(source_text: str):
    """Return the RAW scalar spelling of the top-level ``resume_mode`` value.

    Uses ``yaml.compose`` (NOT ``safe_load``) so the flag is validated on the
    exact characters the operator wrote, BEFORE YAML 1.1 type coercion collapses
    every false-y spelling (``off``, ``false``, ``no``, ``OFF`` ...) onto the
    single Python ``False`` — ``safe_load`` alone cannot tell the documented
    ``off`` apart from an invalid ``false``/``no``/``OFF``.

    Returns one of:
      * ``_FLAG_ABSENT`` — no top-level ``resume_mode`` key (or not a mapping).
      * the raw scalar string (e.g. ``"off"``, ``"false"``, ``"latest"``, ``"0"``)
        when the value is a scalar — quoting is transparent, so ``"off"`` (quoted)
        and ``off`` (plain) both yield ``"off"``.
      * ``None`` — the key is present but its value is a collection, not a scalar.
    Raises ``yaml.YAMLError`` on malformed input (the caller decides the policy).
    """
    node = yaml.compose(source_text)
    if not isinstance(node, yaml.MappingNode):
        return _FLAG_ABSENT
    for key_node, value_node in node.value:
        if (
            isinstance(key_node, yaml.ScalarNode)
            and key_node.value == CONTROLLER_RESUME_MODE_KEY
        ):
            if isinstance(value_node, yaml.ScalarNode):
                return value_node.value
            return None
    return _FLAG_ABSENT


def parse_controller_resume_flag(source_text: str, *, controller_name: str) -> ControllerResumeFlag:
    """Read and validate a controller config yml's optional top-level
    ``resume_mode`` flag from its raw text.

    Accepted values are EXACTLY the scalar spellings ``"latest"`` and ``"off"``
    (quoting is transparent); an absent key is ``"off"``. Validation is on the
    raw spelling, so YAML 1.1 false-aliases that are NOT ``off`` — ``false``,
    ``no``, ``OFF``, ``NO`` — are rejected rather than silently coerced to off.
    ANY other value aborts fail-closed with ``RESUME_FLAG_INVALID`` — never
    guessed, never silently ignored (§1).

    A yml whose text does not parse as YAML is NON-flagged (``present=False``)
    ONLY when it carries no top-level ``resume_mode:`` line: staging then falls
    through to today's plain copy and the engine's own config load stays the
    backstop, so a malformed non-flagged yml behaves byte-for-byte as today. A
    malformed yml that DOES carry a top-level ``resume_mode:`` line holds the
    engine-forbidden key yet cannot be parsed or safely stripped, so it aborts
    fail-closed HERE (``RESUME_FLAG_INVALID``) — the key must never reach the
    engine unstripped.
    """
    try:
        spelling = _top_level_resume_mode_spelling(source_text)
    except yaml.YAMLError:
        if _source_has_top_level_resume_mode_line(source_text):
            raise ResumeError(
                ResumeAbortReason.RESUME_FLAG_INVALID,
                f"Controller '{controller_name}' carries a top-level resume_mode "
                f"line but does not parse as YAML; refusing to stage the "
                f"engine-forbidden key.",
            )
        return ControllerResumeFlag(present=False, mode="off")
    if spelling is _FLAG_ABSENT:
        return ControllerResumeFlag(present=False, mode="off")
    if spelling == "latest":
        return ControllerResumeFlag(present=True, mode="latest")
    if spelling == "off":
        return ControllerResumeFlag(present=True, mode="off")
    raise ResumeError(
        ResumeAbortReason.RESUME_FLAG_INVALID,
        f"Controller '{controller_name}' has resume_mode={spelling!r}; the only "
        f"accepted values are 'latest' and 'off'.",
    )


def _source_has_top_level_resume_mode_line(source_text: str) -> bool:
    """True iff any physical line is a top-level ``resume_mode:`` line. Used as a
    lexical fallback when YAML parsing fails, to keep a malformed file that still
    carries the engine-forbidden key from being routed to the plain-copy path."""
    return any(
        _TOP_LEVEL_RESUME_MODE_LINE.match(line) for line in source_text.split("\n")
    )


def _strip_top_level_resume_mode(source_text: str) -> str:
    """Return ``source_text`` with every top-level ``resume_mode:`` line removed,
    every other byte preserved exactly.

    Each line is split on ``"\\n"`` and carries its OWN ``\\n`` terminator (the
    last element keeps none if the source did not end in a newline). Dropping a
    matched line then removes exactly that line's content AND its own terminator
    — never the PRECEDING line's, which is the byte-identity bug when the flag is
    the final, unterminated line. CRLF endings survive (the ``\\r`` rides along as
    line content). Splitting only on ``"\\n"`` (not ``str.splitlines``' full set
    of Unicode breaks) keeps a ``resume_mode``-looking substring after an exotic
    in-value separator from being mistaken for a top-level line. Only column-0
    ``resume_mode:`` lines match, so comments (``# resume_mode: ...``), nested
    keys and ``resume_mode_*`` keys are untouched.
    """
    parts = source_text.split("\n")
    # Re-attach the separator '\n' that split() consumed to every line except the
    # last, so each line owns its terminator.
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1] != "":
        lines.append(parts[-1])  # trailing unterminated remainder, if any
    kept = [line for line in lines if not _TOP_LEVEL_RESUME_MODE_LINE.match(line)]
    return "".join(kept)


def _verify_resume_mode_stripped(source_text: str, staged_text: str, *, controller_name: str) -> None:
    """Fail-closed post-strip verification (§2): the staged copy must yaml-parse,
    must contain NO ``resume_mode`` key, and must equal ``safe_load(source)``
    minus that one key. Any mismatch — including a flow-style mapping the
    line-strip cannot touch — aborts with ``RESUME_FLAG_INVALID`` rather than
    letting an unstripped or corrupted config reach the engine."""
    try:
        staged_data = yaml.safe_load(staged_text)
    except yaml.YAMLError as exc:
        raise ResumeError(
            ResumeAbortReason.RESUME_FLAG_INVALID,
            f"Controller '{controller_name}' did not parse after the resume_mode "
            f"strip: {exc}",
        )
    staged_map = staged_data or {}
    if not isinstance(staged_map, dict):
        raise ResumeError(
            ResumeAbortReason.RESUME_FLAG_INVALID,
            f"Controller '{controller_name}' is not a mapping after the "
            f"resume_mode strip.",
        )
    if CONTROLLER_RESUME_MODE_KEY in staged_map:
        raise ResumeError(
            ResumeAbortReason.RESUME_FLAG_INVALID,
            f"Controller '{controller_name}' still carries a resume_mode key "
            f"after staging; the strip could not remove it (e.g. flow style).",
        )
    source_map = yaml.safe_load(source_text) or {}
    expected = {k: v for k, v in source_map.items() if k != CONTROLLER_RESUME_MODE_KEY}
    if staged_map != expected:
        raise ResumeError(
            ResumeAbortReason.RESUME_FLAG_INVALID,
            f"Controller '{controller_name}' staging changed more than the "
            f"resume_mode key; refusing to deploy a mutated config.",
        )


def stage_controller_config(source_text: str, *, controller_name: str) -> "tuple[str, ControllerResumeFlag]":
    """Produce the staged controller yml and its resume flag for the staging loop.

    Returns ``(staged_text, flag)``. A NON-flagged yml is returned UNCHANGED —
    the filter never runs on it, so the caller can (and does) keep today's plain
    byte-identical copy. A flagged yml is validated, passed through the
    line-level strip that removes ONLY the top-level ``resume_mode:`` line(s)
    (trailing comment included), then fail-closed verified before it is returned.
    """
    flag = parse_controller_resume_flag(source_text, controller_name=controller_name)
    if not flag.present:
        return source_text, flag
    staged_text = _strip_top_level_resume_mode(source_text)
    _verify_resume_mode_stripped(source_text, staged_text, controller_name=controller_name)
    return staged_text, flag


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
    over ``bots/instances/`` and ``bots/archived/`` is the fallback ONLY when
    the DB is unavailable.

    The winner is resolved with ``instances/`` precedence: if
    ``bots/instances/<winner>/data`` exists it wins outright; otherwise the
    resolution falls through to the local-move archive at
    ``bots/archived/<winner>`` (archiving is the DEFAULT on stop, so the common
    stop-then-redeploy flow moves the source there). Compressed
    (``*_archive.tar.gz``) and S3 archives have no extract path and are not
    resumable — that case fails closed as ``SOURCE_NOT_FOUND``.
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
    if data_dir.is_dir():
        return ResolvedSource(
            instance_name=winner,
            data_dir=data_dir,
            instance_dir=instance_dir,
            origin="instances",
        )

    # instances/ is gone -> fall through to the local-move archive, with the
    # same nested-archive handling as the explicit path (§5). ARCHIVE_NESTED
    # from the helper propagates — an ambiguous nest is a refusal, never a pick.
    archive_base = bots_path / "archived" / winner
    archived_instance_dir = _resolve_archive_instance_dir(archive_base, winner)
    if archived_instance_dir is not None:
        archived_data_dir = archived_instance_dir / "data"
        if archived_data_dir.is_dir():
            return ResolvedSource(
                instance_name=winner,
                data_dir=archived_data_dir,
                instance_dir=archived_instance_dir,
                origin="archived",
            )

    raise ResumeError(
        ResumeAbortReason.SOURCE_NOT_FOUND,
        f"Resolved latest source '{winner}' but no data directory was found "
        f"on disk. Searched '{data_dir}' and archived path '{archive_base}'. "
        f"Note: the source may have been archived compressed "
        f"(*_archive.tar.gz) or to S3 — those archives are not resumable "
        f"(the repo has no download/extract path).",
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
            "listing over instances/ and archived/ for latest resolution.",
            exc,
        )
        # Set-union DEDUPE across the two trees: the same name in both
        # instances/ and archived/ is ONE instance in two places, not
        # duplicated lineage — without the dedupe it would spuriously trip the
        # LATEST_AMBIGUOUS tie check in _pick_newest. DB lineage rows above
        # keep their duplicate-preserving behavior. Compressed
        # ``*_archive.tar.gz`` archives are files, so ``is_dir()`` drops them.
        # No ``_looks_like_instance()`` pre-filter here: a singly-nested
        # archive fails it at the base level but is still resolvable —
        # plausibility is judged at resolution time.
        unique_names = set()
        for tree in ("instances", "archived"):
            tree_dir = bots_path / tree
            if tree_dir.is_dir():
                unique_names.update(d.name for d in tree_dir.iterdir() if d.is_dir())
        candidates = _match_candidates(sorted(unique_names), new_instance_name, target_base)
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


# ---------------------------------------------------------------------------
# CTRLRESUME P2 — controller-identity resolution (spec §4)
# ---------------------------------------------------------------------------
#
# A NEW resolution strategy in front of the EXISTING pipeline: dashboards cannot
# send request-level resume fields, and dashboard instance names embed a
# timestamp so base-name lineage never matches across deploys. The controller
# ``id`` is the one identity that IS stable, so this resolver keys off it: for
# every ``resume_mode: latest``-flagged controller it searches ALL prior
# instances (live and archived) for that controller's expected ledger and picks
# the newest carrier. It returns the SAME :class:`ResolvedSource` shape as
# :func:`resolve_source`, so ``run_guards`` -> ``compute_copy_plan`` -> copy ->
# manifest downstream is untouched.
#
# The fail-closed spine holds: the ONLY fresh-seed this resolver can signal is a
# TRUE first run (no flagged controller's ledger exists in ANY candidate).
# Found-but-unusable history — an unrankable carrier, a corrupt ``.owner`` on
# the winner, an ambiguous nested archive, divergent winners — is ALWAYS a
# refusal, never a fallback and never a fresh seed.
#
# This phase only defines the resolver; the deploy/preview gate wiring is
# phase 3's. Nothing here writes to disk.

@dataclass(frozen=True)
class FlaggedController:
    """One ``resume_mode: latest``-flagged controller's derived identity.

    Attributes:
        config_name: The controller yml filename — the key in the staging
            loop's ``{controller_file: mode}`` collection (phase 1's shape).
        controller_id: The C2-CANONICAL id from :func:`classify_controller_id`.
        ledger_name: The expected ledger filename from
            :func:`_expected_ledger_name` (C1-canonical ``state_file_name`` if
            set, else the default ``range_inventory_ladder_<id>.json``).
        custom_named: True when ``state_file_name`` was set (C1 ``RELATIVE_OK``).
            Drives the winner identity rule: a custom-named ledger with no
            ``.owner`` sidecar is unverifiable (abort), while a default-named
            one embeds the id in its filename (accept) — the same rule as
            :func:`_plan_controller`.
    """

    config_name: str
    controller_id: str
    ledger_name: str
    custom_named: bool


@dataclass
class ControllerFlagResolution:
    """The outcome of :func:`resolve_controller_flag_source`.

    Attributes:
        source: The single agreed :class:`ResolvedSource` (SAME shape as the
            request-level strategies produce — downstream is untouched), or
            ``None`` for a TRUE first run: no flagged controller's ledger exists
            in any candidate instance, so the deploy proceeds WITHOUT resume
            (phase 3 surfaces the ``RESUME_FIRST_RUN_FRESH_SEED`` structured
            warnings, one per entry in ``flagged``).
        flagged: The derived identity of every latest-flagged controller, in
            collection order. Phase 3 feeds these to the first-run warnings and
            the manifest's flagged-controller-ids field.
    """

    source: Optional[ResolvedSource]
    flagged: List[FlaggedController]

    @property
    def first_run(self) -> bool:
        """True when no prior state exists anywhere — the ONLY fresh-seed."""
        return self.source is None


def _derive_flagged_controller(config_path: Path, config_name: str) -> FlaggedController:
    """Derive one flagged controller's canonical identity from its config yml.

    REUSES the contracts — C2 via :func:`classify_controller_id`, C1 via
    :func:`classify_state_file_name`, and the ledger-name derivation via
    :func:`_expected_ledger_name` — never reimplements them.

    Raises:
        ResumeError:
            * ``CONTROLLER_ID_INVALID`` — the config cannot be read/parsed as a
              mapping (identity underivable), or its ``id`` fails C2. The
              operator FLAGGED this controller for identity-keyed resume; an
              identity we cannot derive is a refusal, not a skip.
            * ``STATE_FILE_PATH_INVALID`` — ``state_file_name`` fails C1, or is
              ABSOLUTE. The flag is an explicit resume request on a ledger the
              hook cannot manage — a contradiction, so the C1 opt-out's
              skip-and-warn path must NOT swallow it (spec §4); there is no
              opt-out parameter here on purpose.
    """
    try:
        config = _load_yaml(config_path)
    except (OSError, yaml.YAMLError) as exc:
        raise ResumeError(
            ResumeAbortReason.CONTROLLER_ID_INVALID,
            f"Flagged controller '{config_name}': config '{config_path}' could "
            f"not be read or parsed ({exc}); its identity cannot be derived — "
            f"failing closed (CONTRACT C2).",
        )
    if not isinstance(config, dict):
        raise ResumeError(
            ResumeAbortReason.CONTROLLER_ID_INVALID,
            f"Flagged controller '{config_name}': config '{config_path}' is not "
            f"a mapping; its identity cannot be derived — failing closed "
            f"(CONTRACT C2).",
        )

    id_verdict = classify_controller_id(config.get("id"))
    if not id_verdict.is_valid:
        raise ResumeError(
            ResumeAbortReason.CONTROLLER_ID_INVALID,
            f"Flagged controller '{config_name}': {id_verdict.reason} Refusing "
            f"to resolve a flag-driven resume for an identity outside CONTRACT "
            f"C2 (fail-closed).",
        )

    sfn_verdict = classify_state_file_name(config.get("state_file_name"))
    if sfn_verdict.status is StateFileStatus.INVALID:
        raise ResumeError(
            ResumeAbortReason.STATE_FILE_PATH_INVALID,
            f"Flagged controller '{id_verdict.canonical}': {sfn_verdict.reason} "
            f"Refusing to deploy (CONTRACT C1, fail-closed).",
        )
    if sfn_verdict.status is StateFileStatus.ABSOLUTE:
        # Spec §4: a flagged controller whose state_file_name is ABSOLUTE is a
        # contradiction — resume_mode: latest asks the hook to carry a ledger
        # the hook cannot manage. Unconditional abort; the request-level
        # allow_absolute opt-out (which permits a SKIP, not a resume) does not
        # apply to a controller that explicitly asked to be resumed.
        raise ResumeError(
            ResumeAbortReason.STATE_FILE_PATH_INVALID,
            f"Flagged controller '{id_verdict.canonical}': state_file_name "
            f"'{sfn_verdict.canonical}' is an absolute path the resume hook "
            f"cannot manage, yet resume_mode: latest explicitly requests a "
            f"resume — a contradiction. Remove the flag or make the "
            f"state_file_name data/-relative (CONTRACT C1, fail-closed).",
        )

    return FlaggedController(
        config_name=config_name,
        controller_id=id_verdict.canonical,
        ledger_name=_expected_ledger_name(id_verdict.canonical, sfn_verdict.canonical),
        custom_named=sfn_verdict.canonical is not None,
    )


def _enumerate_candidate_instances(
    bots_path: Path, new_instance_name: str
) -> "Dict[str, tuple]":
    """Enumerate candidate prior instances: ``name -> (instance_dir, origin)``.

    Spec §4 candidate rules:
      * every dir under ``bots/instances/`` plus every name under
        ``bots/archived/``, the latter resolved through
        :func:`_resolve_archive_instance_dir` (REUSED — ``ARCHIVE_NESTED``
        propagates: an ambiguous nest is a refusal, never a pick, even when the
        nested name turns out to carry no flagged ledger — its carrying cannot
        be checked without first picking a level).
      * the instance being created is excluded (both trees).
      * missing ``instances/``/``archived/`` dirs count as empty (fresh
        install) — the CLA-M02 bots-path coupling check remains the guard
        against a wrongly-rooted bots path masquerading as empty.

    A name present in BOTH trees resolves with the same precedence as the
    request-level ``latest`` path (:func:`_resolve_latest`): live
    ``instances/<name>/data`` wins outright and the archive is not consulted
    for that name; without live data the resolution falls through to the
    archive. Compressed ``*_archive.tar.gz`` entries are files, which the
    archive resolver rejects as implausible (dropped — they have no extract
    path and can carry no ledger the hook could read).
    """
    candidates: Dict[str, tuple] = {}

    instances_dir = bots_path / "instances"
    if instances_dir.is_dir():
        for entry in sorted(instances_dir.iterdir()):
            if not entry.is_dir() or entry.name == new_instance_name:
                continue
            candidates[entry.name] = (entry, "instances")

    archived_dir = bots_path / "archived"
    if archived_dir.is_dir():
        for entry in sorted(archived_dir.iterdir()):
            name = entry.name
            if name == new_instance_name:
                continue
            if name in candidates and (candidates[name][0] / "data").is_dir():
                # Live-tree precedence, byte-consistent with _resolve_latest:
                # instances/<name>/data exists -> the archive is not consulted.
                continue
            resolved = _resolve_archive_instance_dir(archived_dir / name, name)
            if resolved is not None:
                candidates[name] = (resolved, "archived")

    return candidates


def _pick_newest_carrier(flagged: FlaggedController, carriers: List[tuple]) -> tuple:
    """Pick the unique newest carrier of one controller's ledger.

    Mirrors :func:`_pick_newest`'s semantics on purpose — ordering by the
    CONTAINING INSTANCE's :func:`_parse_api_timestamp` (REUSED; NEVER file
    mtime, which archiving perturbs), unparseable names dropped as unrankable
    with a warning, a tie on the newest timestamp -> ``LATEST_AMBIGUOUS``. It is
    a separate function only because the subject differs: ``_pick_newest`` ranks
    base-name lineage and its messages talk about base names; here the subject
    is a controller and the operator needs the controller named. The request-
    level path stays byte-for-byte untouched.

    Args:
        flagged: The controller whose ledger the carriers hold.
        carriers: ``[(instance_name, instance_dir, origin), ...]`` — non-empty.

    Returns:
        The winning ``(instance_name, instance_dir, origin)``.

    Raises:
        ResumeError:
            * ``SOURCE_NOT_FOUND`` — carriers exist but NONE is rankable. The
              ledger provably exists on disk, so this is found-but-unusable
              history: a refusal, NEVER a first-run fresh seed.
            * ``LATEST_AMBIGUOUS`` — two carriers share the newest timestamp.
    """
    ranked = []
    unrankable: List[str] = []
    for name, instance_dir, origin in carriers:
        ts = _parse_api_timestamp(name)
        if ts is None:
            logger.warning(
                "Flagged controller '%s': carrier instance '%s' has no "
                "parseable API timestamp suffix; dropping it as unrankable.",
                flagged.controller_id,
                name,
            )
            unrankable.append(name)
            continue
        ranked.append((ts, name, instance_dir, origin))

    if not ranked:
        raise ResumeError(
            ResumeAbortReason.SOURCE_NOT_FOUND,
            f"Controller '{flagged.controller_id}': its ledger "
            f"'{flagged.ledger_name}' exists in {sorted(unrankable)!r} but none "
            f"of those instance names carries a parseable API timestamp, so "
            f"'latest' cannot be ranked. Found-but-unrankable history is a "
            f"refusal, not a fresh seed. Use a request-level "
            f"resume_mode='explicit' to name the exact source.",
        )

    max_ts = max(ts for ts, _, _, _ in ranked)
    top = [(name, d, o) for ts, name, d, o in ranked if ts == max_ts]
    if len(top) > 1:
        raise ResumeError(
            ResumeAbortReason.LATEST_AMBIGUOUS,
            f"Controller '{flagged.controller_id}': {len(top)} candidate "
            f"instances share the newest timestamp: "
            f"{sorted(name for name, _, _ in top)!r}. Use a request-level "
            f"resume_mode='explicit' to name the exact source.",
        )
    return top[0]


def _verify_winner_identity(flagged: FlaggedController, winner: tuple) -> None:
    """Verify one controller's identity ON THE WINNER ONLY (spec §4).

    Applies :func:`_plan_controller`'s ``.owner`` identity rules, via the REUSED
    :func:`_read_owner_controller_id`:

      * sidecar present -> it must parse and its ``controller_id`` must match
        the C2-canonical id (mismatch/corrupt -> ``OWNER_MISMATCH``);
      * no sidecar + custom ``state_file_name`` -> identity unverifiable ->
        ``OWNER_MISMATCH``;
      * no sidecar + default-named ledger -> the filename embeds the id ->
        acceptable (``compute_copy_plan`` re-verifies and warns at plan time).

    ANY failure here aborts the whole resolution. Deliberately NO fallback to
    the second-newest carrier: that would silently resume older state, which is
    the exact failure mode the fail-closed spine forbids. Ledger ENVELOPE
    validation is not duplicated here — the winner flows into the unchanged
    ``compute_copy_plan``, whose ``_validate_ledger`` aborts the deploy on a
    bad envelope (still a refusal, never a fallback).
    """
    name, instance_dir, _origin = winner
    src_ledger = instance_dir / "data" / flagged.ledger_name
    src_owner = instance_dir / "data" / f"{flagged.ledger_name}.owner"

    if src_owner.exists():
        owner_id = _read_owner_controller_id(src_owner)  # OWNER_MISMATCH if corrupt
        if owner_id != flagged.controller_id:
            raise ResumeError(
                ResumeAbortReason.OWNER_MISMATCH,
                f"Owner mismatch for ledger '{src_ledger}' in winning instance "
                f"'{name}': sidecar claims controller '{owner_id}' but the "
                f"flagged controller is '{flagged.controller_id}'. Wrong ledger "
                f"— failing closed; never falling back to an older candidate.",
            )
        return

    if flagged.custom_named:
        raise ResumeError(
            ResumeAbortReason.OWNER_MISMATCH,
            f"Ledger '{src_ledger}' in winning instance '{name}' has a custom "
            f"state_file_name but no '.owner' sidecar; controller identity "
            f"cannot be verified — failing closed; never falling back to an "
            f"older candidate.",
        )
    # Default-named ledger, no sidecar: the filename embeds the id — identity
    # is acceptable per _plan_controller's rule (which will log the warning at
    # plan time when the copy is actually made).


def resolve_controller_flag_source(
    controller_resume_flags: "Dict[str, str]",
    new_instance_name: str,
    bots_path,
    controllers_dir=None,
) -> ControllerFlagResolution:
    """Resolve the single prior instance a flag-driven resume copies from.

    The controller-identity resolution strategy (spec §4), keyed off the one
    identity that is stable across dashboard deploys: the controller ``id``.
    NOT yet wired to the deploy gate — phase 3 activates it (request-level
    resume always wins outright; this runs only when the request says ``off``
    and at least one staged controller is flagged ``latest``).

    Args:
        controller_resume_flags: Phase 1's staging collection,
            ``{controller_file: "latest" | "off"}``. Only ``"latest"`` entries
            participate; ``"off"`` entries are non-flagged and constrain
            nothing.
        new_instance_name: The instance being created — excluded from the
            candidates (both trees).
        bots_path: The ``bots/`` directory containing ``instances/`` and
            ``archived/``.
        controllers_dir: Where the flagged controllers' config ymls are read
            from. Defaults to the SOURCE tree ``<bots_path>/conf/controllers``
            (what the preview reads); the deploy call site may pass its staged
            ``conf/controllers`` instead — the identity fields are identical by
            phase 1's fail-closed strip verification (only ``resume_mode`` may
            differ).

    Returns:
        A :class:`ControllerFlagResolution`: either the ONE agreed
        :class:`ResolvedSource` every flagged controller resolves to, or the
        TRUE-first-run signal (``source=None``) when NO flagged controller's
        ledger exists in any candidate — the only fresh-seed in the feature.

    Raises:
        ResumeError: fail-closed on any resolution failure — see the helpers
            for the per-reason conditions (``CONTROLLER_ID_INVALID``,
            ``STATE_FILE_PATH_INVALID``, ``ARCHIVE_NESTED``,
            ``SOURCE_NOT_FOUND``, ``LATEST_AMBIGUOUS``, ``OWNER_MISMATCH``,
            ``CONTROLLER_SOURCES_DIVERGENT``).
        ValueError: if NO entry is flagged ``latest`` (programmer error — the
            phase-3 gate must check before calling, exactly as
            :func:`resolve_source` refuses ``resume_mode='off'``).
    """
    bots_path = Path(bots_path)
    if controllers_dir is None:
        controllers_dir = bots_path / "conf" / "controllers"
    controllers_dir = Path(controllers_dir)

    flagged_names = [
        name for name, mode in controller_resume_flags.items() if mode == "latest"
    ]
    if not flagged_names:
        # The gate filters on the flags; reaching here without one is a wiring bug.
        raise ValueError(
            "resolve_controller_flag_source called with no latest-flagged controllers"
        )

    flagged = [
        _derive_flagged_controller(controllers_dir / name, name)
        for name in flagged_names
    ]

    # ARCHIVE_NESTED propagates from here — never swallowed, never picked-around.
    candidates = _enumerate_candidate_instances(bots_path, new_instance_name)

    # Per flagged controller: carriers -> newest wins -> verify the winner ONLY.
    winners: Dict[str, tuple] = {}
    for fc in flagged:
        carriers = [
            (name, instance_dir, origin)
            for name, (instance_dir, origin) in candidates.items()
            if (instance_dir / "data" / fc.ledger_name).is_file()
        ]
        if not carriers:
            # No candidate holds this controller's ledger. Alone this does NOT
            # decide anything: if EVERY flagged controller lands here it is a
            # true first run; if others resolve, this controller fresh-seeds
            # per compute_copy_plan's existing per-controller semantics.
            continue
        winner = _pick_newest_carrier(fc, carriers)
        _verify_winner_identity(fc, winner)
        winners[fc.controller_id] = winner

    if not winners:
        # TRUE FIRST RUN — no flagged controller's ledger exists anywhere. The
        # ONLY fresh-seed this resolver can signal; every abort above outranks it.
        logger.info(
            "Controller-flag resume: no prior ledger found for any flagged "
            "controller (%s) — true first run, deploy proceeds without resume.",
            ", ".join(sorted(fc.controller_id for fc in flagged)),
        )
        return ControllerFlagResolution(source=None, flagged=flagged)

    distinct_winner_names = {name for name, _, _ in winners.values()}
    if len(distinct_winner_names) > 1:
        details = "; ".join(
            f"controller '{controller_id}' -> instance '{name}'"
            for controller_id, (name, _, _) in sorted(winners.items())
        )
        raise ResumeError(
            ResumeAbortReason.CONTROLLER_SOURCES_DIVERGENT,
            f"Flagged controllers resolve to different source instances: "
            f"{details}. A flag-driven resume copies from exactly one prior "
            f"instance; use a request-level resume_mode='explicit' to name it.",
        )

    winner_name, winner_dir, winner_origin = next(iter(winners.values()))
    resolved = ResolvedSource(
        instance_name=winner_name,
        data_dir=winner_dir / "data",
        instance_dir=winner_dir,
        origin=winner_origin,
    )
    logger.info(
        "Resume source resolved: mode=controller_flag instance=%s origin=%s "
        "data_dir=%s controllers=%s",
        resolved.instance_name,
        resolved.origin,
        resolved.data_dir,
        sorted(winners),
    )
    return ControllerFlagResolution(source=resolved, flagged=flagged)


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
        structured_warnings: The machine-readable half of ``warnings`` — the
            entries an API caller can branch on instead of grepping prose. A log
            line is invisible to whoever posted the deploy; C1's opt-out is only
            honest if the resulting skip comes back in the response.
    """

    items: List[CopyItem] = field(default_factory=list)
    decisions: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    structured_warnings: List[dict] = field(default_factory=list)

    @property
    def files_to_copy(self) -> List[CopyItem]:
        """The subset of ``items`` Phase 5 will actually copy."""
        return [it for it in self.items if it.copyable]

    def _warn(self, message: str) -> None:
        logger.warning(message)
        self.warnings.append(message)

    def _warn_structured(self, code: str, message: str, **fields) -> None:
        """Record a warning in BOTH channels: the human string list and the
        structured list that reaches the deploy/preview response body."""
        self._warn(message)
        entry = {"code": code, "message": message}
        entry.update(fields)
        self.structured_warnings.append(entry)


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


def _validate_staged_controller_ids(new_instance_dir: Path) -> List[tuple]:
    """CONTRACT C2 (CDX-008/CLA-002), API half: validate EVERY staged range-ladder
    controller's ``id`` before any of them is planned.

    This is a whole-pass gate, not a per-controller check inside the planning
    loop, and that is the point. C2 says a violation aborts the deploy; an abort
    that happens on the third controller after the first two are already in
    ``deployed_ids`` and ``plan.items`` has produced a partial plan, and a partial
    plan is precisely the half-resumed state the hook exists to prevent. Failing
    the whole pass first makes "abort" mean nothing was planned at all.

    The configs are returned rather than re-read by the caller so the YAMLs are
    parsed exactly once: a second pass could read a different file than the one
    validated here.

    Args:
        new_instance_dir: The staged instance directory.

    Returns:
        ``[(config, canonical_id), ...]`` for the range-ladder controllers only,
        in staging order, each id C2-canonical (stripped).

    Raises:
        ResumeError: ``CONTROLLER_ID_INVALID`` on the first C2 violation. The
            old code used ``continue`` here instead, which let the deploy succeed
            while silently leaving that controller's ledger behind.
    """
    staged: List[tuple] = []
    for yaml_path, config in _iter_staged_controllers(new_instance_dir):
        if config.get("controller_name") != RANGE_LADDER_CONTROLLER_NAME:
            continue
        verdict = classify_controller_id(config.get("id"))
        if not verdict.is_valid:
            raise ResumeError(
                ResumeAbortReason.CONTROLLER_ID_INVALID,
                f"Staged controller '{yaml_path.name}': {verdict.reason} Refusing to "
                f"deploy (CONTRACT C2, fail-closed). Deploying without this "
                f"controller's ledger would re-seed it from the wallet.",
            )
        staged.append((config, verdict.canonical))
    return staged


_DB_ENGINE_SQLITE = "sqlite"
_DB_ENGINE_POSTGRES_PREFIX = "postgres"


def _classify_db_engine(engine) -> bool:
    """CDX-015 — classify a staged ``db_mode.db_engine`` value. True == sqlite.

    The old test was ``engine is None or engine == "sqlite"``: an exact,
    case-sensitive comparison whose ELSE branch meant "Postgres, so there is no
    per-instance sqlite to carry forward". Every value that was not literally
    ``"sqlite"`` therefore took the Postgres path silently — ``"SQLite"``,
    ``" sqlite "`` and a typo like ``"sqlkite"`` all dropped the source DB from
    the copy set and the deploy still SUCCEEDED. That is fail-open on a value
    nobody validates, so this normalizes and then refuses what it cannot name.

    Args:
        engine: The raw ``db_mode.db_engine`` value as staged (any YAML type).

    Returns:
        True for sqlite (carry the per-instance DB), False for Postgres (§15:
        nothing per-instance to carry).

    Raises:
        ResumeError: ``DB_ENGINE_UNKNOWN`` for any value that is neither. An
            unrecognised engine is genuine uncertainty about whether a DB must
            be carried forward, and guessing either way is a silent data
            decision — so it aborts.
    """
    if engine is None:
        # Unset -> the engine's documented default (DBSqliteMode,
        # client_config_map.py:772). This is not fail-open: it is the value the
        # bot itself will use.
        return True
    normalized = str(engine).strip().casefold()
    if normalized == _DB_ENGINE_SQLITE:
        return True
    if normalized.startswith(_DB_ENGINE_POSTGRES_PREFIX):
        # "postgres", "postgresql", "postgresql+asyncpg", ... — DBOtherMode.
        return False
    raise ResumeError(
        ResumeAbortReason.DB_ENGINE_UNKNOWN,
        f"Staged conf_client.yml declares an unrecognised db_mode.db_engine "
        f"{engine!r} (normalized: '{normalized}'). Refusing to deploy: the hook "
        f"cannot tell whether a per-instance sqlite DB must be carried forward, "
        f"and assuming Postgres would silently drop the source DB from the copy "
        f"set. Expected 'sqlite' or a 'postgres...' engine (CDX-015, fail-closed).",
    )


def _is_sqlite_deployment(new_instance_dir: Path) -> bool:
    """Parse the staged ``conf_client.yml`` and decide if the engine DB is sqlite.

    The engine default is ``DBSqliteMode`` (client_config_map.py:772), so a
    missing file / missing ``db_mode`` block is treated as sqlite. A Postgres
    (``DBOtherMode``) deployment carries a ``db_mode.db_engine`` naming Postgres
    and has no per-instance sqlite to carry (§15). Anything else aborts — see
    :func:`_classify_db_engine`.
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
    return _classify_db_engine(db_mode.get("db_engine"))


# ---------------------------------------------------------------------------
# Per-controller ledger resolution
# ---------------------------------------------------------------------------

def _expected_ledger_name(
    canonical_controller_id: str, canonical_state_file_name: Optional[str]
) -> str:
    """Return the expected state-file name for a controller.

    Mirrors ``range_inventory_ladder.py:1624`` — the state file name if set, else
    ``range_inventory_ladder_<id>.json``.

    Both arguments are CANONICAL values, taken as parameters rather than re-read
    from the config, so neither contract can be bypassed by a caller reaching
    around it: only a C1-classified name and a C2-classified id ever reach a path
    here. Passing the raw ``id`` would have named the ledger of a whitespace-only
    controller ``range_inventory_ladder_   .json`` — which is exactly what the
    old code did.

    Args:
        canonical_controller_id: The C2-CANONICAL (stripped) id from
            :func:`classify_controller_id`.
        canonical_state_file_name: The C1-CANONICAL (stripped) value from
            :func:`classify_state_file_name`, or ``None`` when unset.
    """
    if canonical_state_file_name:
        return canonical_state_file_name
    return f"range_inventory_ladder_{canonical_controller_id}.json"


def _validate_ledger(src_ledger: Path, config: dict, canonical_controller_id: str) -> None:
    """Fail-closed unless an expected ledger is a VALID ENGINE ENVELOPE (CDX-M02).

    Never seed garbage — the engine would quarantine and re-seed from the wallet
    (the insufficient-funds bug this whole hook exists to prevent).

    This used to check length + UTF-8 + ``json.loads`` and stop, which proved only
    that the bytes were JSON. ``{"levels": [1, 2]}`` passed it, got copied forward
    as a "resumed" ledger, and then quarantined engine-side on load — re-seeding
    from the wallet anyway, but now with the operator believing state had been
    carried. Syntax is not an envelope. The envelope rules live in
    :mod:`services.ledger_envelope_contract`, mirrored from the engine.

    The split is deliberate: this function owns the file (read errors, zero-length,
    JSON syntax) and the contract module owns the envelope, exactly as the engine
    splits ``_load_state`` from ``_validate_loaded_state``.

    Args:
        src_ledger: The source ledger path (already containment-checked).
        config: The staged controller config, passed whole to the contract, which
            resolves the identity fields the engine will compare the ledger
            against — falling back to the engine's own model defaults for fields
            the YAML omits, never skipping a comparison (CDX-R02).
        canonical_controller_id: The C2-CANONICAL (stripped) staged id. Passed in
            rather than re-read from ``config`` so no unvalidated id reaches an
            identity comparison — the same reasoning as :func:`_plan_controller`'s.

    Raises:
        ResumeError: ``LEDGER_INVALID`` on any read error, zero-length file, JSON
            syntax error, or envelope violation. Every uncertainty is a refusal.
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
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResumeError(
            ResumeAbortReason.LEDGER_INVALID,
            f"Expected ledger '{src_ledger}' is not valid JSON ({exc}) — refusing to seed garbage.",
        )
    except RecursionError as exc:
        # CDX-R03: json.loads recurses per nesting level, so a ledger nested past
        # the interpreter's recursion limit raises RecursionError — a RuntimeError,
        # NOT a ValueError, so it slipped the handler above and surfaced as an
        # opaque 500 instead of this contract's structured 409. The deploy was
        # still refused (the exception propagated), but a fail-closed abort that
        # cannot say why is a broken contract, not a safe one.
        raise ResumeError(
            ResumeAbortReason.LEDGER_INVALID,
            f"Expected ledger '{src_ledger}' is nested too deeply to parse ({exc}) — "
            f"refusing to seed garbage.",
        )

    verdict = classify_ledger_envelope(
        payload,
        canonical_controller_id=canonical_controller_id,
        staged_config=config,
        now_timestamp=time.time(),
    )
    if not verdict.is_valid:
        raise ResumeError(
            ResumeAbortReason.LEDGER_INVALID,
            f"Expected ledger '{src_ledger}' is not a valid engine ledger: {verdict.reason} "
            f"Copying it forward would deploy a bot that quarantines this state on load and "
            f"re-seeds from the wallet. Refusing to deploy (CDX-M02, fail-closed).",
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


def _assert_contained(candidate: Path, root: Path, label: str, controller_id) -> Path:
    """Runtime half of CONTRACT C1: resolve ``candidate`` and assert it is a
    STRICT descendant of ``root``, aborting rather than proceeding on violation.

    Mirrors ``_assert_path_contained`` (range_inventory_ladder.py:116). The
    lexical classifier cannot see the filesystem, so it cannot see a symlink: a
    perfectly C1-clean ``sub/ledger.json`` still escapes if ``sub`` is a symlink
    to ``/etc``. Resolving is the only check that catches it, so it runs at plan
    time — before any copy, and on the preview path too, which never copies at all
    and would otherwise never be checked.

    Not memoized, deliberately (same reasoning as the engine's): what is being
    checked is the path's resolution, and that is mutable.

    If either path cannot be RESOLVED, this fails closed. There is no lexical
    fallback: a lexical path proves only where the string points, and what is being
    checked here is where the filesystem points — the two differ by exactly the
    symlink this function exists to catch. Substituting the lexical path would let
    an unresolvable candidate pass containment and be classified ``fresh_seed``,
    i.e. deploy without prior state. Uncertainty is a refusal, not a pass.

    Returns:
        The resolved path.

    Raises:
        ResumeError: ``STATE_FILE_PATH_INVALID`` if it escapes ``root``, or if
            either path cannot be resolved.
    """
    resolved_paths = []
    for path, what in ((root, f"{label} containment root"), (candidate, label)):
        try:
            resolved_paths.append(Path(path).resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            # OSError: filesystem refused; RuntimeError: symlink loop;
            # ValueError: embedded null byte. Any of them means "cannot prove
            # containment" — the only safe answer is no.
            raise ResumeError(
                ResumeAbortReason.STATE_FILE_PATH_INVALID,
                f"Controller '{controller_id}': the {what} '{path}' could not be "
                f"resolved ({type(exc).__name__}: {exc}); CONTRACT C1 containment "
                f"cannot be verified. Failing closed.",
            )
    root_resolved, resolved = resolved_paths
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise ResumeError(
            ResumeAbortReason.STATE_FILE_PATH_INVALID,
            f"Controller '{controller_id}': the {label} '{candidate}' resolves to "
            f"'{resolved}', which is not contained in '{root_resolved}' — CONTRACT C1 "
            f"containment violation (a symlink escape resolves here even when the "
            f"configured name is lexically clean). Failing closed.",
        )
    return resolved


def _plan_controller(
    config: dict,
    controller_id: str,
    source: "ResolvedSource",
    new_data_dir: Path,
    plan: CopyPlan,
    allow_absolute_state_file_name: bool = False,
) -> Optional[str]:
    """Plan the copy for one range-ladder controller; mutate ``plan`` in place.

    CONTRACT C1 (CDX-007/CLA-004) is enforced here, on the raw
    ``state_file_name``, BEFORE it is ever joined to a path. The old code asked
    only "is this absolute?" and, when the answer was yes, SKIPPED the controller
    and let the deploy succeed — a bot resumed with no ledger and re-seeded from
    the wallet, which is the insufficient-funds bug this hook exists to prevent.
    A skip is not a safe default for a path we cannot honour; an abort is.

    Args:
        config: The staged controller config.
        controller_id: The C2-CANONICAL (stripped) id from
            :func:`classify_controller_id`, already validated by
            :func:`_validate_staged_controller_ids`. Taken as a parameter rather
            than re-read from ``config`` so this function cannot see a raw or
            invalid id: CONTRACT C2 is enforced before any planning begins, and
            re-reading ``config["id"]`` here would quietly reintroduce the raw
            value into the ledger filename and the ``.owner`` match.
        allow_absolute_state_file_name: The deploy request's explicit C1 opt-out.
            Rescues ABSOLUTE names ONLY (they take the old skip path, with a
            structured warning in the response) — never traversal, never
            drive-/root-relative forms.

    Returns:
        The expected ledger *filename* handled for this controller (so the
        reverse scan can skip it), or ``None`` when nothing on disk was named
        (opt-out skip).

    Raises:
        ResumeError: ``STATE_FILE_PATH_INVALID`` on any C1 violation.
    """
    raw_state_file_name = config.get("state_file_name")
    verdict = classify_state_file_name(raw_state_file_name)

    # C1 REJECT: traversal, drive-/root-relative, `.`, non-str, non-descendant.
    # The opt-out cannot reach this branch — it permits absolute paths, never these.
    if verdict.status is StateFileStatus.INVALID:
        raise ResumeError(
            ResumeAbortReason.STATE_FILE_PATH_INVALID,
            f"Controller '{controller_id}': {verdict.reason} Refusing to deploy "
            f"(CONTRACT C1, fail-closed).",
        )

    if verdict.status is StateFileStatus.ABSOLUTE:
        if not allow_absolute_state_file_name:
            raise ResumeError(
                ResumeAbortReason.STATE_FILE_PATH_INVALID,
                f"Controller '{controller_id}': {verdict.reason} The resume hook cannot "
                f"carry it forward, and silently deploying without this controller's "
                f"ledger would re-seed it from the wallet. Refusing to deploy "
                f"(CONTRACT C1, fail-closed). Set '{ALLOW_ABSOLUTE_FIELD}: true' on the "
                f"deploy request to accept the skip deliberately.",
            )
        # Opt-out taken: the old skip path, but now an explicitly requested one
        # that is reported back to the caller rather than buried in a log line.
        plan.items.append(
            CopyItem(
                src=Path(verdict.canonical),
                dst=None,
                controller_id=controller_id,
                kind="absolute_skipped",
            )
        )
        plan.decisions[controller_id] = "skipped"
        plan._warn_structured(
            "STATE_FILE_ABSOLUTE_SKIPPED",
            f"Controller '{controller_id}': state_file_name '{verdict.canonical}' is an "
            f"absolute path — it escapes data/ and is not the resume hook's to manage "
            f"(shared-mount scheme assumed). Skipping this controller's state because "
            f"'{ALLOW_ABSOLUTE_FIELD}' was set on the deploy request: it will start from "
            f"whatever is at that absolute path, or re-seed from the wallet if nothing is.",
            controller_id=controller_id,
            state_file_name=verdict.canonical,
        )
        return None

    # UNSET -> the engine's default name; RELATIVE_OK -> the canonical (stripped) value.
    ledger_name = _expected_ledger_name(controller_id, verdict.canonical)
    src_ledger = source.data_dir / ledger_name
    src_owner = source.data_dir / f"{ledger_name}.owner"

    # Runtime containment (C1's belt-and-braces half): the concrete source and
    # destination must resolve strictly inside their data/ roots. Lexically clean
    # names still escape through symlinks, and the destination is where a bad name
    # would have us WRITE.
    _assert_contained(src_ledger, source.data_dir, "source ledger path", controller_id)
    _assert_contained(new_data_dir / ledger_name, new_data_dir, "ledger destination", controller_id)
    # The sidecar is guarded on the same terms as the ledger it describes: it is
    # written to a path derived from the same (attacker-influenced) name, so it
    # needs the same containment proof rather than inheriting the ledger's.
    _assert_contained(src_owner, source.data_dir, "source owner sidecar path", controller_id)
    _assert_contained(
        new_data_dir / f"{ledger_name}.owner", new_data_dir, "owner sidecar destination", controller_id
    )

    # No ledger in source -> warn + fresh seed for THIS controller only (§6 table).
    if not src_ledger.exists():
        plan.decisions[controller_id] = "fresh_seed"
        plan._warn(
            f"Controller '{controller_id}': no ledger '{ledger_name}' in source '{source.data_dir}' — "
            f"fresh-seeding this controller (legitimate for a newly added controller)."
        )
        return ledger_name

    # Ledger present -> must be a valid engine envelope, else abort (never seed
    # garbage). Runs BEFORE the .owner check on purpose: the envelope carries the
    # controller's own identity claim, so a ledger that is not an engine ledger at
    # all is rejected as such rather than as an owner mismatch.
    _validate_ledger(src_ledger, config, controller_id)

    # Identity via .owner controller_id, never the filename (§6).
    if src_owner.exists():
        owner_id = _read_owner_controller_id(src_owner)
        if owner_id != controller_id:
            raise ResumeError(
                ResumeAbortReason.OWNER_MISMATCH,
                f"Owner mismatch for ledger '{src_ledger}': sidecar claims controller "
                f"'{owner_id}' but the deploy expects '{controller_id}'. Wrong ledger — failing closed.",
            )
        # CLA-008 #7: the sidecar keeps the ledger's RELATIVE path, exactly as the
        # ledger item below does. Using ``src_owner.name`` flattened it to the
        # bare basename, so a state_file_name of 'sub/dir/x.json' copied the
        # ledger to 'data/sub/dir/x.json' but its sidecar to 'data/x.json.owner'.
        # The engine looks for the sidecar ADJACENT to the ledger, so the pair was
        # separated: the ledger arrived with its identity marker missing.
        plan.items.append(
            CopyItem(
                src=src_owner,
                dst=new_data_dir / f"{ledger_name}.owner",
                controller_id=controller_id,
                kind="owner",
            )
        )
    else:
        # No sidecar. A custom state_file_name carries no id in its name, so
        # identity is unverifiable -> fail closed. A default-named ledger embeds
        # the id in its filename -> warn + copy.
        #
        # Keyed off the C1-CANONICAL value, not the raw one: a whitespace-only
        # state_file_name is UNSET by contract, so it takes the default-named
        # branch (whose filename does carry the id) rather than being treated as a
        # custom name of "   ".
        if verdict.canonical is not None:
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
        deployment: The deploy model (reads ``resume_extra_paths`` and the
            CONTRACT C1 opt-out ``allow_absolute_state_file_name``).

    Returns:
        A :class:`CopyPlan` (items, per-controller decisions, warnings).

    Raises:
        ResumeError: fail-closed on ``LEDGER_INVALID`` / ``OWNER_MISMATCH`` /
            ``STATE_FILE_PATH_INVALID`` / ``CONTROLLER_ID_INVALID`` /
            ``EXTRA_PATH_ESCAPE`` / ``EXTRA_PATH_MISSING`` (§11).
    """
    new_instance_dir = Path(new_instance_dir)
    new_data_dir = new_instance_dir / "data"
    plan = CopyPlan()

    # CONTRACT C1 opt-out. ``getattr`` default False is the fail-closed default:
    # a deploy model that never heard of the field, or a caller that omitted it,
    # gets the rejection — the opt-out is only ever reachable by asking for it.
    allow_absolute = getattr(deployment, ALLOW_ABSOLUTE_FIELD, False) is True

    deployed_ids: set = set()
    handled_names: set = set()

    # CONTRACT C2 gate: every staged range-ladder id is validated BEFORE the first
    # one is planned, so a violation aborts with an empty plan rather than a
    # partial one. Raises CONTROLLER_ID_INVALID; nothing below has run yet.
    staged_controllers = _validate_staged_controller_ids(new_instance_dir)

    # Per-controller: range-ladder controllers in the new deploy (staged YAMLs).
    # ``controller_id`` is C2-canonical and drives every identity derivation from
    # here down — deployed_ids, the ledger filename, the '.owner' match.
    for config, controller_id in staged_controllers:
        deployed_ids.add(controller_id)
        handled = _plan_controller(config, controller_id, source, new_data_dir, plan, allow_absolute)
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
# (database/models.py — CREATED / RUNNING / STOPPED / ERROR). Anything else
# (or a missing end marker / absent row) is treated as ungraceful (§7.5).
# CDX-005: STOPPED alone is NOT trusted — the row must also carry
# ``retirement_status == RETIREMENT_VERIFIED``, written only by the
# acknowledged-retirement state machine once every postcondition (stop ack,
# exchange-confirmed zero open orders, fill drain, clean exit, archive) has
# persisted evidence. Legacy rows predate that schema and are UNVERIFIED by
# definition.
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

    CLA-008 P2: any OTHER Docker failure (API error, daemon unreachable) used to
    escape as an opaque 500 from whatever handler caught it last. A 500 reads as
    "the API broke", not as "we never verified the source was stopped" — and that
    verification is the guard's whole job. An unreachable daemon is not evidence
    of a quiesced container, so it now refuses with ``SOURCE_STATE_UNVERIFIED``
    (409) rather than proceeding against a source that may still be running and
    writing to the ledger.

    Only Docker-client exceptions are mapped. Programming errors (``TypeError``,
    ``AttributeError``) propagate untouched: a blanket ``except Exception`` here
    would turn our own bugs into a tidy "could not verify" 409 that blames the
    daemon and hides them.
    """
    from docker.errors import DockerException, NotFound
    from requests.exceptions import RequestException

    name = source.instance_name
    try:
        container = docker_client.containers.get(name)
    except NotFound:
        # Verified ABSENT. NotFound subclasses APIError, so this arm must stay
        # ahead of the DockerException arm below or a gone container would
        # become a refusal.
        report._record(
            "source_container",
            True,
            f"No container named '{name}' — source is a stopped/removed instance on disk.",
        )
        return
    except (DockerException, RequestException) as exc:
        # DockerException covers APIError and friends; RequestException covers the
        # transport layer beneath docker-py (daemon down, socket refused, timeout),
        # which does NOT subclass DockerException.
        _abort_guard(
            ResumeAbortReason.SOURCE_STATE_UNVERIFIED,
            f"Source container state could not be verified for '{name}': the Docker "
            f"client failed with {type(exc).__name__}: {exc}. Refusing to deploy — an "
            f"unverifiable source may still be running and holding the ledger "
            f"(CLA-008 P2, fail-closed).",
            report,
        )

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


def _parse_retirement_evidence(raw) -> Optional[Dict]:
    """``bot_runs.retirement_evidence`` (JSON text) → dict, else None.

    None / empty / unparseable / non-dict all map to None — absent or
    malformed evidence can never validate (fail-closed, CDX-005)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def _guard_ungraceful_source(
    source: "ResolvedSource", deployment, bot_run_repo, report: GuardReport
) -> None:
    """§7.5 — advisory: the source must have VERIFIABLY retired (CDX-005).

    The source's most recent ``bot_runs`` row is graceful iff its ``run_status``
    is ``STOPPED`` AND it carries an end marker (``stopped_at``) AND its
    ``retirement_status`` is ``VERIFIED`` AND its persisted
    ``retirement_evidence`` actually VALIDATES — parses to a dict carrying
    every postcondition the state machine requires (the same
    ``missing_retirement_evidence`` predicate the writer used). The marker
    alone is never trusted: ``retirement_status`` is an unconstrained column,
    so a bare/hand-set ``VERIFIED`` with null, malformed or incomplete
    evidence is treated as unverified (CDX-005). A non-stopped / errored
    status, a missing end marker, an absent row (unknown history), or an
    UNVERIFIED/unevidenced retirement — which includes EVERY legacy STOPPED
    row predating the evidence schema — is ungraceful →
    ``UNGRACEFUL_SOURCE`` unless the explicit human override
    ``resume_accept_ungraceful=True`` is set, in which case the guard PASSES
    with a loud warning recorded. Default is refusal.
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
        retirement = getattr(run, "retirement_status", None)
        evidence = _parse_retirement_evidence(getattr(run, "retirement_evidence", None))
        evidence_gaps = missing_retirement_evidence(evidence) if evidence is not None else None
        graceful = (
            status == _GRACEFUL_RUN_STATUS
            and stopped_at is not None
            and retirement == RETIREMENT_VERIFIED
            and evidence_gaps == []
        )
        detail = (
            f"Source '{source.instance_name}' last run_status={status!r}, "
            f"stopped_at={stopped_at!r}, retirement_status={retirement!r}."
        )
        if not graceful and status == _GRACEFUL_RUN_STATUS:
            if retirement != RETIREMENT_VERIFIED:
                detail += (
                    " STOPPED without VERIFIED retirement evidence (legacy row or"
                    " unconfirmed stop) is UNVERIFIED by definition (CDX-005)."
                )
            elif evidence is None:
                detail += (
                    " retirement_status=VERIFIED but the persisted retirement"
                    " evidence is absent or malformed — the marker alone is never"
                    " trusted (CDX-005); treated as unverified."
                )
            elif evidence_gaps:
                detail += (
                    f" retirement_status=VERIFIED but the persisted evidence is"
                    f" missing postconditions {evidence_gaps} — the marker alone"
                    f" is never trusted (CDX-005); treated as unverified."
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

# ---------------------------------------------------------------------------
# CLA-M01 — sizing-critical drift classification
# ---------------------------------------------------------------------------
#
# Template-wins is DELIBERATE and unchanged: a resumed bot runs the staged
# template's parameters, not the live edits made to the source instance's YAML
# while it ran. What CLA-M01 fixes is that the operator only learned this from a
# log line they never saw. Drift in a field that decides HOW MUCH MONEY the bot
# deploys is surfaced as a structured warning on the deploy and preview
# responses, so the decision to accept template-wins is an informed one.
#
# Field names enumerated (read-only) from the engine's ladder config,
# ``hummingbot/controllers/market_making/range_inventory_ladder.py``:
#   :210 total_amount_quote        — the managed quote fund
#   :218 max_fund_value_quote      — hard cap on deployable fund value
#   :226 shared_account_quote_quota— clamps free_buy_budget_quote
#   :262 use_wallet_balance        — whether base inventory is claimed at all
#   :270 claimed_base_value_quote  — quote value of base claimed on first start
#   :278 claimed_base_amount       — explicit base amount claimed (overrides ^)
#   :291 buy_prices                — the ladder's buy-side range bounds
#   :302 buy_amounts_pct           — per-level buy sizing weights
#   :310 sell_prices               — the ladder's sell-side range bounds
#   :320 sell_amounts_pct          — per-level sell sizing weights
# Matched by NAME, not by controller type: these names mean the same thing in
# any controller that carries them, and mis-classifying drift as ordinary is the
# failure this fix exists to prevent.
_SIZING_CRITICAL_FIELDS = frozenset({
    "total_amount_quote",
    "max_fund_value_quote",
    "shared_account_quote_quota",
    "use_wallet_balance",
    "buy_prices",
    "buy_amounts_pct",
    "sell_prices",
    "sell_amounts_pct",
})

# ``claimed_base_*`` per the triage's glob: covers claimed_base_value_quote and
# claimed_base_amount above, and any future sibling the engine adds — a new
# base-claiming field must be loud by default rather than silent until someone
# remembers to list it here.
_SIZING_CRITICAL_PREFIXES = ("claimed_base_",)


def _is_sizing_critical(field: str) -> bool:
    """True if drift in ``field`` can change the size of the resumed bot's
    positions or the capital it deploys (CLA-M01)."""
    return field in _SIZING_CRITICAL_FIELDS or field.startswith(_SIZING_CRITICAL_PREFIXES)


def _warn_sizing_critical_drift(drift: List[dict], plan: CopyPlan) -> None:
    """Surface sizing-critical drift on the response, one entry per field.

    Structured (not just logged) because a log line is invisible to whoever
    posted the deploy — the whole point of CLA-M01. Per FIELD rather than per
    file so a caller can branch on ``field`` without re-parsing prose.

    Behavior is unchanged: this only reports. The template still wins.
    """
    for entry in drift:
        for field in entry["fields"]:
            if not field.get("sizing_critical"):
                continue
            plan._warn_structured(
                "SIZING_CRITICAL_DRIFT",
                f"Sizing-critical config drift for controller "
                f"'{entry['controller_id']}' ({entry['file']}): '{field['field']}' "
                f"was {field['source']!r} on the source instance but is "
                f"{field['template']!r} in the staged template. The template WINS "
                f"— the resumed bot deploys capital per the template value, not "
                f"the value the stopped bot was running.",
                file=entry["file"],
                controller_id=entry["controller_id"],
                field=field["field"],
                source=field["source"],
                template=field["template"],
            )


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

    CLA-M01: each field entry additionally carries ``sizing_critical``
    (:func:`_is_sizing_critical`) and each file entry lists
    ``sizing_critical_fields``. Callers surface those on the deploy/preview
    response via :func:`_warn_sizing_critical_drift`. The classification is
    reporting only — no copy or template semantics change here.
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
                    {
                        "field": key,
                        "source": source_value,
                        "template": template_value,
                        "sizing_critical": _is_sizing_critical(key),
                    }
                )
        if fields:
            entry = {
                "file": staged.name,
                "controller_id": new_cfg.get("id"),
                "fields": fields,
                "sizing_critical_fields": [
                    f["field"] for f in fields if f["sizing_critical"]
                ],
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
    # CLA-M01: sizing-critical drift also rides the response, not just the log.
    # Ordered before the manifest is built so plan.structured_warnings (which
    # the manifest snapshots, and create_hummingbot_instance lifts onto the
    # deploy response) already carries these entries.
    _warn_sizing_critical_drift(drift, plan)

    # 6. Audit manifest (§13) — also the double-resume detector: it lands in
    #    data/ and trips the DEST_NOT_EMPTY guard of any later re-seed attempt.
    manifest = {
        "source_instance": source.instance_name,
        "source_path": str(source.data_dir),
        "mode": deployment.resume_mode,
        "files": files,
        "decisions": dict(plan.decisions),
        # The structured half of plan.warnings — C1's opt-out skip has to reach
        # whoever posted the deploy, not just the log (see _warn_structured).
        # ``create_hummingbot_instance`` lifts these onto the response.
        "warnings": list(plan.structured_warnings),
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
        ``dst`` at real target paths), ``decisions``, ``warnings`` (structured;
        includes CLA-M01 sizing-critical drift), ``drift`` (the full classified
        field-level config diff), ``guard_report``, ``would_succeed`` (always
        ``True`` — failures raise).

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
            # CLA-M01: the same diff the deploy runs, against the same two
            # inputs — the source instance's live YAMLs and the staged templates
            # (which is exactly what ``tmp_instance_dir/conf/controllers`` holds
            # above). Preview is where an operator can still act on sizing drift;
            # learning about it from the deploy response is learning too late.
            drift = _diff_controller_configs(source, tmp_instance_dir)
            _warn_sizing_critical_drift(drift, plan)
            return source, guard_report, plan, drift

        if bot_run_repo is None and db_manager is not None:
            from database import BotRunRepository

            async with db_manager.get_session_context() as session:
                source, guard_report, plan, drift = await _preview_run(BotRunRepository(session))
        else:
            source, guard_report, plan, drift = await _preview_run(bot_run_repo)

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
            # Structured, machine-readable warnings (C1 opt-out skips land here,
            # CLA-M01's sizing-critical drift entries alongside them). The preview
            # is a pre-deploy check: a skip — or a silent resize — the deploy
            # would perform is exactly what an operator needs to see BEFORE
            # deploying.
            "warnings": list(plan.structured_warnings),
            # The full field-level diff (CLA-M01), classified. The warnings above
            # are the sizing-critical subset; this is everything, for an operator
            # who wants to see what else the template will overwrite.
            "drift": drift,
            "guard_report": guard_report.to_dict(),
            "would_succeed": True,
        }
