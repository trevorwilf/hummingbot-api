"""CONTRACT C2 (CDX-008 / CLA-002) — the controller ``id`` contract, API half.

The ONE api-side predicate for a staged controller's ``id``, in a module of its
own for the same reason as its C1 sibling (``state_file_contract.py``): the
contract is shared with the engine, and a single home is what lets "the API and
the engine agree" be checked by reading two files rather than grepping for
lookalikes.

The engine half (Run A, landed) lives at
``hummingbot/strategy_v2/controllers/controller_base.py``:

  * ``ControllerConfigBase.id``  :68 — ``str`` + ``min_length=1``
  * ``validate_id``              :95 — the strip validator; canonical = stripped

CONTRACT C2
-----------
ACCEPT
    a ``str`` whose stripped length is >= 1. Canonical id = the stripped value;
    all identity derivations (ledger filename, ``.owner`` match) use it.

REJECT
    non-``str`` (int, bool, None — Pydantic v2 already rejects these engine-side;
    preserve that), ``""``, whitespace-only.

PROHIBITED (both sides)
    the ``str(owner_id) != str(controller_id)`` comparison fix — both review
    engines independently called it unsound.

Why the API enforces a contract the engine already enforces
-----------------------------------------------------------
The two layers see different inputs. The engine validates a config it is about
to LOAD; the API validates a staged YAML it is about to derive FILENAMES from,
before any engine exists to object. The old api-side check was ``if not
controller_id: continue`` — falsy, so it silently dropped ``id: 0`` and ``id:
""`` alike, and ``continue`` meant the deploy SUCCEEDED with that controller's
ledger left behind. The bot then re-seeded from the wallet: the
insufficient-funds bug the resume hook exists to prevent. A skip is not a safe
default for a controller whose identity we cannot derive; an abort is.

Note the two rejections differ in kind and both matter:

  * ``id: 0`` — falsy, so the old check dropped it. It is also non-``str``, which
    the engine rejects; the API must not disagree with the engine about what a
    valid config is.
  * ``id: "   "`` — TRUTHY, so the old check let it through, and every identity
    derivation downstream became ambiguous: the ledger filename
    ``range_inventory_ladder_   .json`` and an ``.owner`` match against ``"   "``.
    This is the fail-open C2 closes (COPY_FORWARD_HOOK_REVIEW.md's "requires
    ``id: 0``/``id: False``" TL;DR is refuted — ``""`` and ``"   "`` were the
    reachable ones).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ControllerIdStatus(str, Enum):
    """The C2 verdict for a controller ``id``.

    Two-valued on purpose: C2 has no ``UNSET`` (unlike C1, where unset selects
    the engine's default state-file name). ``id`` is REQUIRED — a missing ``id``
    is a violation, not a default.
    """

    VALID = "VALID"      # a str, non-empty after strip
    INVALID = "INVALID"  # non-str (incl. None/bool/int), empty, whitespace-only


@dataclass(frozen=True)
class ControllerIdVerdict:
    """The outcome of :func:`classify_controller_id`.

    Attributes:
        status: The :class:`ControllerIdStatus`.
        canonical: The stripped value for ``VALID``; ``None`` for ``INVALID``
            (an invalid id has no canonical form — callers must never fall back
            to the raw value).
        reason: Operator-facing explanation. Empty for accepted values.
    """

    status: ControllerIdStatus
    canonical: Optional[str]
    reason: str = ""

    @property
    def is_valid(self) -> bool:
        return self.status is ControllerIdStatus.VALID


def classify_controller_id(value) -> ControllerIdVerdict:
    """Classify a raw staged-controller ``id`` under CONTRACT C2.

    Mirrors the engine's ``ControllerConfigBase.id`` + ``validate_id``
    (controller_base.py:68, :95) decision for decision.

    ``bool`` is called out explicitly even though ``isinstance(True, str)`` is
    already False: ``id: yes`` in YAML parses to ``True``, and naming the type in
    the rejection is what makes that legible to whoever wrote the config.

    Args:
        value: The raw config value (any type — this is untrusted input).

    Returns:
        A :class:`ControllerIdVerdict`. This function never raises: callers map
        the verdict to their own fail-closed action, so a hostile config yields a
        structured 409 rather than an opaque 500.
    """
    if not isinstance(value, str):
        return ControllerIdVerdict(
            ControllerIdStatus.INVALID,
            None,
            f"controller id must be a string (got {type(value).__name__} {value!r}); "
            f"CONTRACT C2 accepts a non-empty string only. The engine's "
            f"ControllerConfigBase rejects this value too (controller_base.py:68).",
        )

    stripped = value.strip()
    if not stripped:
        return ControllerIdVerdict(
            ControllerIdStatus.INVALID,
            None,
            f"controller id {value!r} is empty or whitespace-only; every identity "
            f"derivation from it (ledger filename, '.owner' sidecar match) would be "
            f"ambiguous. CONTRACT C2 requires a stripped length of at least 1.",
        )

    return ControllerIdVerdict(ControllerIdStatus.VALID, stripped)
