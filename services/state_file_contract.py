"""CONTRACT C1 (CDX-007 / CLA-004) — the ``state_file_name`` path contract, API half.

This is the ONE api-side predicate for ``state_file_name``. It is deliberately a
module of its own rather than a helper inside ``resume_service``: the contract is
shared with the engine and with any model-layer validation, and a single home is
what lets "the API and the engine agree" be checked by reading two files instead
of grepping for lookalikes.

The engine half (Run A, landed) lives at
``hummingbot/controllers/market_making/range_inventory_ladder.py``:

  * ``_is_absolute_either_flavor``     :38
  * ``_has_parent_reference``          :47
  * ``_validate_data_relative_file_name`` :55  — the lexical accept/reject set
  * ``_assert_path_contained``         :116 — the runtime containment half

The accept/reject set below mirrors ``_validate_data_relative_file_name`` decision
for decision, INCLUDING its ordering, because the ordering is load-bearing (see
``classify_state_file_name``). Keeping the two in sync is a recorded obligation in
the final report — if you change one, change the other.

CONTRACT C1
-----------
ACCEPT
    unset/None; or a ``str`` whose stripped value is non-empty and, parsed as BOTH
    ``PurePosixPath`` and ``PureWindowsPath``: ``is_absolute()`` is False, has no
    drive and no root/anchor, contains no ``..`` component, is not ``.``, and its
    POSIX normalization remains a strict descendant of ``data/`` when joined.
    Purely lexical — no filesystem access at validation time.

REJECT (fail-closed)
    absolute POSIX or Windows paths, drive letters, UNC paths, any ``..``
    component, ``.`` — UNLESS an explicit opt-out (``allow_absolute_state_file_name``,
    default False) is set, which permits ABSOLUTE paths only, never traversal.

CANONICAL VALUE
    the stripped string. Empty-after-strip maps to unset/None, which is NOT
    fail-open: None selects the engine's default state file name
    (``range_inventory_ladder_<id>.json``, range_inventory_ladder.py:1624).

Why both path flavors (this is the whole point, not defensive noise) — prod is
Linux, dev is Windows, and each flavor is blind to the other's absolute forms:

  * ``PurePosixPath("C:\\\\x.json")``  -> one relative component; POSIX cannot see the drive.
  * ``PurePosixPath("\\\\\\\\share\\\\x")`` -> one relative component; POSIX cannot see the UNC.
  * ``PureWindowsPath("/tmp/x.json").is_absolute()`` -> False (rooted but driveless).
  * ``PurePosixPath("sub\\\\..\\\\x.json").parts`` -> one opaque component; POSIX cannot
    see the ``..``, so a POSIX-only traversal check misses it entirely.

A POSIX-only check therefore ACCEPTS ``C:\\x.json`` and ``\\\\share\\x`` as ordinary
relative filenames — they even normalize to a strict descendant of ``data/``.
That is the fail-open this contract closes.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Optional

# The instance-relative directory every state file must live strictly under.
# Mirrors ``_DATA_DIR_NAME`` at range_inventory_ladder.py:35.
DATA_DIR_NAME = "data"

# The deploy-request field that opts out of the absolute-path rejection.
ALLOW_ABSOLUTE_FIELD = "allow_absolute_state_file_name"


class StateFileStatus(str, Enum):
    """The C1 verdict for a ``state_file_name``.

    ``ABSOLUTE`` is the ONLY status the opt-out can rescue. Traversal, drive- and
    root-relative forms, ``.`` and non-``str`` values are ``INVALID`` and are
    refused no matter what the request asks for — C1 permits absolute paths, never
    traversal.
    """

    UNSET = "UNSET"              # None, or empty after strip -> engine default name
    RELATIVE_OK = "RELATIVE_OK"  # accepted: relative, contained, no traversal
    ABSOLUTE = "ABSOLUTE"        # absolute under some flavor; opt-out may permit
    INVALID = "INVALID"          # never acceptable, opt-out or not


@dataclass(frozen=True)
class StateFileVerdict:
    """The outcome of :func:`classify_state_file_name`.

    Attributes:
        status: The :class:`StateFileStatus`.
        canonical: The stripped string for ``RELATIVE_OK``/``ABSOLUTE``; ``None``
            for ``UNSET`` and for ``INVALID`` (an invalid value has no canonical
            form — callers must never fall back to it).
        reason: Operator-facing explanation. Empty for accepted values.
    """

    status: StateFileStatus
    canonical: Optional[str]
    reason: str = ""

    @property
    def is_unset(self) -> bool:
        return self.status is StateFileStatus.UNSET


def _is_absolute_either_flavor(name: str) -> bool:
    """True when ``name`` is absolute under POSIX *or* Windows semantics.

    Mirrors range_inventory_ladder.py:38.
    """
    return PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute()


def _has_parent_reference(name: str) -> bool:
    """True when ``name`` contains a ``..`` component under either flavor.

    The Windows flavor is required to see backslash separators. Mirrors
    range_inventory_ladder.py:47.
    """
    return ".." in PurePosixPath(name).parts or ".." in PureWindowsPath(name).parts


def classify_state_file_name(value) -> StateFileVerdict:
    """Classify a raw ``state_file_name`` under CONTRACT C1 (lexical only).

    Mirrors ``_validate_data_relative_file_name`` (range_inventory_ladder.py:55)
    decision for decision. The check ORDER is part of the contract:

    1. non-``str`` -> INVALID. C1's accept set is "unset/None; or a ``str`` ...";
       anything else is outside it. (Pydantic v2 already rejects these engine-side.)
    2. empty after strip -> UNSET, not INVALID: None selects the default name.
    3. traversal -> INVALID **before** the absolute test, so ``/tmp/../etc/x`` is
       refused as traversal rather than rescued as "absolute" by the opt-out.
       C1 permits absolute paths, never traversal.
    4. absolute (either flavor) -> ABSOLUTE. The caller's opt-out decides; this
       function does not, so the accept/reject set stays one thing.
    5. drive- or root-relative but NOT absolute (``C:``, ``\\x.json``) -> INVALID.
       The engine rejects these after its opt-out early-return (:87), so they are
       refused even under the opt-out: they are not absolute paths, and the opt-out
       permits absolute paths only.
    6. ``.`` / ``./`` (no parts under some flavor) -> INVALID.
    7. not a strict descendant of ``data/`` once joined -> INVALID.

    Args:
        value: The raw config value (any type — this is untrusted input).

    Returns:
        A :class:`StateFileVerdict`. This function never raises: callers map the
        verdict to their own fail-closed action.
    """
    if value is None:
        return StateFileVerdict(StateFileStatus.UNSET, None)

    if not isinstance(value, str):
        return StateFileVerdict(
            StateFileStatus.INVALID,
            None,
            f"state_file_name must be a string (got {type(value).__name__} {value!r}); "
            f"CONTRACT C1 accepts unset or a string only.",
        )

    name = value.strip()
    if name == "":
        # Not fail-open: unset selects the engine's default state file name.
        return StateFileVerdict(StateFileStatus.UNSET, None)

    # (3) Traversal first — unconditionally invalid, never opt-out-able.
    if _has_parent_reference(name):
        return StateFileVerdict(
            StateFileStatus.INVALID,
            None,
            f"state_file_name {value!r} contains a '..' component; it would escape "
            f"{DATA_DIR_NAME}/. Traversal is refused unconditionally — the "
            f"{ALLOW_ABSOLUTE_FIELD} opt-out permits absolute paths, never traversal.",
        )

    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)

    # (4) Absolute under either flavor — the caller's opt-out decides.
    if _is_absolute_either_flavor(name):
        return StateFileVerdict(
            StateFileStatus.ABSOLUTE,
            name,
            f"state_file_name {value!r} is an absolute path; it escapes {DATA_DIR_NAME}/ "
            f"and is not the resume hook's to manage.",
        )

    # (5) Drive-relative (``C:``) / root-relative (``\\x.json``): rejected outright.
    if posix.root or windows.root or windows.drive:
        return StateFileVerdict(
            StateFileStatus.INVALID,
            None,
            f"state_file_name {value!r} carries a drive letter or a root/anchor; "
            f"drive-relative and root-relative paths are refused (they are not "
            f"absolute paths, so {ALLOW_ABSOLUTE_FIELD} does not permit them).",
        )

    # (6) ``.`` / ``./`` name a directory reference, not a file.
    if not posix.parts or not windows.parts:
        return StateFileVerdict(
            StateFileStatus.INVALID,
            None,
            f"state_file_name {value!r} must name a file, not a directory reference.",
        )

    # (7) Lexical containment: strict descendant of ``data/``.
    joined = PurePosixPath(DATA_DIR_NAME).joinpath(posix)
    if PurePosixPath(DATA_DIR_NAME) not in joined.parents:
        return StateFileVerdict(
            StateFileStatus.INVALID,
            None,
            f"state_file_name {value!r} does not normalize to a strict descendant "
            f"of {DATA_DIR_NAME}/.",
        )

    return StateFileVerdict(StateFileStatus.RELATIVE_OK, name)
