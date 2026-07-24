"""hbpurseapi P2 — the versioned PURSE-JOURNAL envelope contract, API half.

The one api-side predicate for a controller's purse journal ENVELOPE, a sibling of
:mod:`services.ledger_envelope_contract` and for the same reason: the contract is
shared with the engine, and a single home is what lets "the API and the engine
agree" be checked by reading two files rather than grepping for lookalikes. This
formalizes the deliberately-narrow structural check P1 shipped inline
(``_purse_envelope_reason_minimal``) into the FULL "Purse journal contract v1", and
P1's plan call site (``services.resume_service._validate_purse``) now delegates
here.

The engine half is not ours to write — it already exists, and it is the SPEC this
module mirrors. Every rule below is transcribed from
``hummingbot/controllers/_shared/purse_ledger.py``:

  * ``PurseLedger._validate_document``   :545 — the load-time gate
  * ``PurseLedger._validate_record``     :621 — the per-kind field gate
  * ``_require_known_epoch``             :682 — the epoch-reference gate
  * ``PURSE_SCHEMA_VERSION = 1``         :29
  * ``OPENING_BASIS_QUALITIES``          :31
  * ``FLOW_KINDS``                       :32
  * ``FLOW_CONFIRMATIONS``               :33
  * ``REANCHOR_CLASSIFICATIONS``         :34
  * ``EPOCH_OPENING_KINDS``              :37
  * ``KNOWN_KINDS``                      :39-41
  * ``_MONEY_FIELDS``                    :100-131 — per-kind money fields + signs
  * ``_parse_decimal`` / ``_require_ts`` / ``_require_str`` / ``_require_enum``
    :56-95 — the field parsers

Why the API validates a purse the engine will validate anyway
-------------------------------------------------------------
The same reason the ledger envelope exists, sharpened for money. The purse journal
is FAIL-CLOSED engine-side: ``PurseLedger.load`` raises ``PurseIntegrityError`` on
any contract violation, and the controller reacts by setting ``accounting_degraded``
— halting NEW order proposals until an operator intervenes. So a purse the engine
refuses does not silently reset (as a quarantined ledger does); it STALLS the bot.
Either way the deploy is already doomed, and the API is the only layer that can
still refuse it while refusing is free. Copying a doubtful money journal forward
resumes a bot on suspect accounting — the exact thing the copy-forward hook exists
to prevent. Uncertainty is ``PURSE_INVALID``, never a copy.

Independence from the ledger schema (F22)
-----------------------------------------
The purse has its OWN version (:data:`PURSE_SCHEMA_VERSION` = 1), deliberately
decoupled from the ledger's ``SUPPORTED_LEDGER_SCHEMA_VERSIONS`` ({6..10}). A purse
change never bumps the ladder state schema, and a ladder schema bump never touches
the purse — the two contracts drift independently and each carries its own,
separate manual-sync obligation to its engine source. This module MUST be kept in
sync with ``purse_ledger.py`` exactly as the ledger contract is kept in sync with
``_validate_loaded_state``; ``test_copyforward_purse_envelope.py`` pins the copy.

Identity: mirror the engine's resolved config, never skip
---------------------------------------------------------
The engine constructs ``PurseLedger(controller_id=config.id,
controller_name=config.controller_name, trading_pair=config.trading_pair)`` and
``_validate_document`` compares the journal's ``controller_id`` (:553),
``controller_name`` (:558) and ``trading_pair`` (:563) EXACTLY against those
RESOLVED config values. So this mirror checks all three:

  * ``controller_id`` — compared verbatim against the C2-canonical staged id (the
    journal's copy is never stripped, mirroring the engine's exact ``!=``); passed
    in rather than re-read so an unvalidated id never reaches the comparison.
  * ``controller_name`` / ``trading_pair`` — resolved from the staged YAML the same
    way the ledger contract resolves its identity fields (:data:`ENGINE_IDENTITY_DEFAULTS`
    — absent means the engine's model default, NOT "unknowable"), then compared
    verbatim. Checking only ``controller_id`` would bless a journal whose name/pair
    the engine quarantines — the F7 class of fail-open, reborn for the purse.

Deliberate DIVERGENCE from the engine (strictly stricter, strictly safe)
------------------------------------------------------------------------
The engine's ``kind not in KNOWN_KINDS`` (:593) is a bare ``frozenset`` membership
that would raise ``TypeError`` on an unhashable ``kind`` (a list/dict off untrusted
disk). The engine loads only files it wrote (single writer), so its ``kind`` is
always a string; this module validates UNTRUSTED disk state, so it guards
``isinstance(kind, str)`` first — a non-string kind is invalid regardless, and a
structured ``PURSE_INVALID`` beats an opaque 500. Same total-input discipline as
the ledger contract: every hostile shape yields a verdict, never an exception.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from services.ledger_envelope_contract import ENGINE_IDENTITY_DEFAULTS

# Mirrors purse_ledger.py:29 — the purse's OWN version, independent of the ledger's
# SUPPORTED_LEDGER_SCHEMA_VERSIONS (F22). MUST be kept in sync with the engine.
PURSE_SCHEMA_VERSION = 1

# Mirrors purse_ledger.py:31-34 — the closed enums each record kind draws from.
OPENING_BASIS_QUALITIES = ("reconstructed", "current_equity_only")
FLOW_KINDS = ("deposit", "withdrawal")
FLOW_CONFIRMATIONS = ("wallet_delta_matched", "drift")
REANCHOR_CLASSIFICATIONS = ("undeclared_outflow", "drift")

# Mirrors purse_ledger.py:37 — kinds that OPEN an accounting epoch. Every
# fills_rollup/reanchor/checkpoint must reference an epoch one of these opened.
EPOCH_OPENING_KINDS = ("opening_epoch", "reseed_epoch")

# Mirrors purse_ledger.py:39-41.
KNOWN_KINDS = frozenset(
    {"opening_epoch", "flow", "fills_rollup", "reseed_epoch", "reanchor", "checkpoint"}
)

# Mirrors purse_ledger.py:100-131 — per-kind money fields; the value is True when
# the contract pins the field non-negative (signed cumulative deltas carry False).
# MUST be kept in sync with the engine's ``_MONEY_FIELDS``.
PURSE_MONEY_FIELDS = {
    "opening_epoch": {
        "owned_quote": True, "owned_base": True, "seed_value_quote": True,
        "reference_price": True, "wallet_quote_total": True, "wallet_base_total": True,
        "unavailable_quote": True, "unavailable_base": True,
        "contributed_opening_quote": True,
        # A reconstructed opening basis may legitimately carry a negative earned-to-date.
        "earned_opening_quote": False,
    },
    "flow": {
        "native_amount": True, "quote_valuation": True, "valuation_price": True,
    },
    "fills_rollup": {
        # Signed cumulative deltas: base bought-sold, quote received-spent (fees netted).
        "base_delta_cum": False, "quote_delta_cum": False, "fees_quote_cum": True,
    },
    "reseed_epoch": {
        "old_owned_quote": True, "old_owned_base": True, "old_seed_value_quote": True,
        "new_owned_quote": True, "new_owned_base": True, "new_seed_value_quote": True,
        "reference_price": True,
    },
    "reanchor": {
        "old_owned_quote": True, "old_owned_base": True,
        "new_owned_quote": True, "new_owned_base": True,
        "overclaim_quote": True, "wallet_quote_total": True, "wallet_base_total": True,
    },
    "checkpoint": {
        "owned_quote": True, "owned_base": True, "reference_price": True,
        "equity_quote": True, "wallet_quote_total": True, "wallet_base_total": True,
        "external_holds_quote": True, "external_holds_base": True,
    },
}


@dataclass(frozen=True)
class PurseVerdict:
    """The outcome of :func:`classify_purse_envelope`.

    Attributes:
        is_valid: True only when every mirrored engine rule passed.
        reason: Operator-facing explanation naming the exact rule and the engine
            line it mirrors. Empty for accepted envelopes.
    """

    is_valid: bool
    reason: str = ""


class _PurseEnvelopeInvalid(Exception):
    """Internal control-flow signal carrying an operator-facing ``reason``.

    Raised by the field parsers and validation steps and caught ONCE at the top of
    :func:`classify_purse_envelope`, which converts it to a ``PurseVerdict``. This
    is the same never-raises-to-the-caller contract the ledger envelope offers, but
    the purse's validation is deeply nested (per-record, per-field), so an internal
    exception mirrors the engine's ``PurseIntegrityError`` structure 1:1 while the
    public predicate still returns a verdict for every input.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Field parsers — mirrors of purse_ledger.py:56-95, raising _PurseEnvelopeInvalid
# (the engine raises PurseIntegrityError) so the whole validation is one try block.
# ---------------------------------------------------------------------------

def _parse_decimal(value, field_name: str) -> Decimal:
    """Finite Decimal from a journal field value; invalid on anything else.

    Mirrors purse_ledger.py:56-66, including the ``bool`` rejection (``True``/
    ``False`` are not money) and the ``is_finite`` check (JSON parses NaN/Infinity).
    """
    if value is None or value == "" or isinstance(value, bool):
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' must be a finite decimal, got {value!r}"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' is not a decimal: {value!r}"
        ) from exc
    if not parsed.is_finite():
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' must be finite, got {value!r}"
        )
    return parsed


def _require_nonneg(value: Decimal, field_name: str) -> Decimal:
    """Mirrors purse_ledger.py:69-72."""
    if value < Decimal("0"):
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' must be non-negative, got {value}"
        )
    return value


def _require_ts(value, field_name: str) -> float:
    """Mirrors purse_ledger.py:75-81 — a finite, non-negative real number (not bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' must be a number, got {value!r}"
        )
    # A JSON integer literal is arbitrary-precision in Python, so an overflow-sized
    # int off untrusted disk (e.g. a 400-digit `ts`) raises OverflowError from
    # float() — parseable JSON, yet unrepresentable as an epoch. Convert it to a
    # structured verdict rather than let it escape classify_purse_envelope as an
    # opaque exception: the never-raises contract must hold for EVERY parseable
    # input, not only the ones float() happens to accept. (Overflow-ts audit finding.)
    try:
        ts = float(value)
    except (OverflowError, ValueError) as exc:
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' is not a representable finite number, got {value!r}"
        ) from exc
    if ts != ts or ts in (float("inf"), float("-inf")) or ts < 0.0:
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' must be a finite non-negative number"
        )
    return ts


def _require_str(value, field_name: str) -> str:
    """Mirrors purse_ledger.py:84-87 — a non-empty string."""
    if not isinstance(value, str) or value == "":
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' must be a non-empty string, got {value!r}"
        )
    return value


def _require_enum(value, field_name: str, allowed) -> str:
    """Mirrors purse_ledger.py:90-95 — membership in a closed TUPLE (== comparison,
    so an unhashable ``value`` is rejected, never a TypeError)."""
    if value not in allowed:
        raise _PurseEnvelopeInvalid(
            f"purse field '{field_name}' must be one of {sorted(allowed)}, got {value!r}"
        )
    return value


def _resolve_expected_identity(staged_config: dict, field_name: str) -> str:
    """Resolve what the engine's ``PurseLedger`` was handed for ``field_name``.

    The engine passes ``config.controller_name`` / ``config.trading_pair`` — the
    RESOLVED Pydantic values, whose defaults are exactly
    :data:`ENGINE_IDENTITY_DEFAULTS` (shared with the ledger contract). So an absent
    staged field is NOT "unknowable": the engine resolves its model default and
    compares against it. A present but non-``str``/blank value is one the engine's
    ``str``-typed field could never resolve — uncertainty, hence ``PURSE_INVALID``.
    """
    if field_name not in staged_config:
        return ENGINE_IDENTITY_DEFAULTS[field_name]
    value = staged_config[field_name]
    if not isinstance(value, str) or not value.strip():
        raise _PurseEnvelopeInvalid(
            f"staged controller's {field_name} is {value!r}, which is not a usable "
            f"identity value: the engine's config model types this field as a bare "
            f"`str` (range_inventory_ladder.py:192-206), so the purse journal's "
            f"identity cannot be established against this config — refusing on "
            f"uncertainty rather than skipping the comparison (fail-closed)."
        )
    # Present -> compared VERBATIM; the engine applies no strip to these fields.
    return value


# ---------------------------------------------------------------------------
# Record validation — mirrors purse_ledger.py:621-690.
# ---------------------------------------------------------------------------

def _require_known_epoch(record: dict, seq, opened_epochs: set) -> None:
    """Mirrors purse_ledger.py:682-690 — a fills_rollup/reanchor/checkpoint must
    reference an ``epoch_id`` some earlier opening_epoch/reseed_epoch opened. Because
    ``opened_epochs`` accumulates as records are walked in order, a record that
    references an epoch opened only LATER (or never) is rejected, exactly as the
    engine rejects it."""
    epoch_id = record.get("epoch_id")
    _require_str(epoch_id, f"records[{seq}].epoch_id")
    if epoch_id not in opened_epochs:
        raise _PurseEnvelopeInvalid(
            f"records[{seq}] references epoch {epoch_id!r} that no opening_epoch/"
            f"reseed_epoch record opened"
        )


def _validate_record(record: dict, opened_epochs: set) -> None:
    """Mirrors purse_ledger.py:621-680 — money fields, then per-kind required fields.

    ``record["kind"]`` is guaranteed a KNOWN string by the caller (the KNOWN_KINDS
    gate runs first), so ``PURSE_MONEY_FIELDS[kind]`` never KeyErrors.
    """
    kind = record["kind"]
    seq = record.get("seq")
    for field, non_negative in PURSE_MONEY_FIELDS[kind].items():
        value = _parse_decimal(record.get(field), f"records[{seq}].{field}")
        if non_negative:
            _require_nonneg(value, f"records[{seq}].{field}")
    if kind == "opening_epoch":
        _require_str(record.get("epoch_id"), f"records[{seq}].epoch_id")
        _require_enum(
            record.get("opening_basis_quality"),
            f"records[{seq}].opening_basis_quality",
            OPENING_BASIS_QUALITIES,
        )
        # purse_ledger.py:632-647 — `predecessor` and `note` are REQUIRED keys whose
        # VALUES may be null; presence is checked before nullability.
        for nullable_field in ("predecessor", "note"):
            if nullable_field not in record:
                raise _PurseEnvelopeInvalid(
                    f"records[{seq}].{nullable_field} is required (null is allowed, "
                    "absence is not)"
                )
        predecessor = record.get("predecessor")
        if predecessor is not None and not isinstance(predecessor, str):
            raise _PurseEnvelopeInvalid(
                f"records[{seq}].predecessor must be a string or null, got {predecessor!r}"
            )
        note = record.get("note")
        if note is not None and not isinstance(note, str):
            raise _PurseEnvelopeInvalid(f"records[{seq}].note must be a string or null")
    elif kind == "flow":
        _require_str(record.get("token"), f"records[{seq}].token")
        _require_enum(record.get("flow_kind"), f"records[{seq}].flow_kind", FLOW_KINDS)
        _require_str(record.get("asset"), f"records[{seq}].asset")
        _require_ts(record.get("valuation_ts"), f"records[{seq}].valuation_ts")
        _require_enum(
            record.get("confirmation"), f"records[{seq}].confirmation", FLOW_CONFIRMATIONS
        )
    elif kind == "fills_rollup":
        _require_known_epoch(record, seq, opened_epochs)
        fills_seen = record.get("fills_seen")
        if isinstance(fills_seen, bool) or not isinstance(fills_seen, int) or fills_seen < 0:
            raise _PurseEnvelopeInvalid(
                f"records[{seq}].fills_seen must be a non-negative integer"
            )
        _require_ts(record.get("last_update_ts"), f"records[{seq}].last_update_ts")
    elif kind == "reseed_epoch":
        _require_str(record.get("epoch_id"), f"records[{seq}].epoch_id")
        if "prev_epoch_id" not in record:
            raise _PurseEnvelopeInvalid(
                f"records[{seq}].prev_epoch_id is required (null is allowed, absence is not)"
            )
        prev_epoch = record.get("prev_epoch_id")
        if prev_epoch is not None and not isinstance(prev_epoch, str):
            raise _PurseEnvelopeInvalid(
                f"records[{seq}].prev_epoch_id must be a string or null"
            )
        _require_str(record.get("token"), f"records[{seq}].token")
    elif kind == "reanchor":
        _require_known_epoch(record, seq, opened_epochs)
        _require_enum(
            record.get("classification"),
            f"records[{seq}].classification",
            REANCHOR_CLASSIFICATIONS,
        )
    elif kind == "checkpoint":
        _require_known_epoch(record, seq, opened_epochs)


def _validate_document(payload, canonical_controller_id: str, staged_config: dict) -> None:
    """Mirrors purse_ledger.py:545-619 — the whole-document envelope, in engine order."""
    # 0. The staged config is the yardstick the identity comparisons need. (API-side;
    #    the engine holds a resolved config object here.) Not a mapping -> we cannot
    #    establish identity at all -> refuse on uncertainty.
    if not isinstance(staged_config, dict):
        raise _PurseEnvelopeInvalid(
            f"staged controller config must be a mapping (got "
            f"{type(staged_config).__name__}); the purse journal's identity cannot be "
            f"established without it — refusing on uncertainty."
        )

    # 1. Top-level shape (purse_ledger.py:546-547).
    if not isinstance(payload, dict):
        raise _PurseEnvelopeInvalid("purse journal payload must be a JSON object")

    # 2. purse_schema_version == 1 (:548-552). Independent of the ledger schema (F22).
    if payload.get("purse_schema_version") != PURSE_SCHEMA_VERSION:
        raise _PurseEnvelopeInvalid(
            f"unsupported purse_schema_version {payload.get('purse_schema_version')!r}; "
            f"supported: {PURSE_SCHEMA_VERSION} (the purse has its own version, independent "
            f"of the ledger schema)"
        )

    # 3. controller_id — compared EXACTLY against the C2-canonical staged id
    #    (:553-557); the journal's copy is never stripped.
    if payload.get("controller_id") != canonical_controller_id:
        raise _PurseEnvelopeInvalid(
            f"purse journal controller_id {payload.get('controller_id')!r} does not match "
            f"the staged controller's canonical id {canonical_controller_id!r}; refusing to "
            f"adopt a foreign journal (the engine compares these exactly at "
            f"purse_ledger.py:553 and fails closed on mismatch)."
        )

    # 4-5. controller_name (:558-562) and trading_pair (:563-567) — compared against
    #      the RESOLVED staged config, never skipped.
    expected_name = _resolve_expected_identity(staged_config, "controller_name")
    if payload.get("controller_name") != expected_name:
        raise _PurseEnvelopeInvalid(
            f"purse journal controller_name {payload.get('controller_name')!r} does not match "
            f"the staged controller's {expected_name!r}; the engine rejects this at "
            f"purse_ledger.py:558 and the controller degrades (accounting_degraded)."
        )
    expected_pair = _resolve_expected_identity(staged_config, "trading_pair")
    if payload.get("trading_pair") != expected_pair:
        raise _PurseEnvelopeInvalid(
            f"purse journal trading_pair {payload.get('trading_pair')!r} does not match the "
            f"staged controller's {expected_pair!r}; the engine rejects this at "
            f"purse_ledger.py:563 and the controller degrades (accounting_degraded)."
        )

    # 6. records is a list (:568-570).
    records = payload.get("records")
    if not isinstance(records, list):
        raise _PurseEnvelopeInvalid("purse journal 'records' must be a list")

    # 7. Per-record loop (:571-607): shape, 1-based strictly-increasing seq, ts, known
    #    kind, per-kind fields, single-open-per-epoch, single-rollup-per-epoch.
    opened_epochs: set = set()
    rollup_epochs: set = set()
    prev_seq = 0
    index = 0
    for record in records:
        index += 1
        if not isinstance(record, dict):
            raise _PurseEnvelopeInvalid(f"purse record #{index} must be a JSON object")
        seq = record.get("seq")
        # `bool` excluded (isinstance(True, int) is True). First record's seq must be
        # exactly 1 (1-based); every later seq strictly greater — no contiguity demand
        # (a gap is contract-valid, purse_ledger.py:573-576/584-585).
        if (
            isinstance(seq, bool)
            or not isinstance(seq, int)
            or (index == 1 and seq != 1)
            or seq <= prev_seq
        ):
            raise _PurseEnvelopeInvalid(
                f"purse record seq {seq!r} violates the 1-based strictly-increasing order "
                f"(record #{index}, previous seq {prev_seq})"
            )
        prev_seq = seq
        _require_ts(record.get("ts"), f"records[{seq}].ts")
        kind = record.get("kind")
        # Stricter-than-engine guard (see module docstring): a non-str kind can never
        # be a KNOWN kind, and checking type first avoids a frozenset TypeError on an
        # unhashable value off untrusted disk.
        if not isinstance(kind, str) or kind not in KNOWN_KINDS:
            raise _PurseEnvelopeInvalid(f"purse record #{seq} has unknown kind {kind!r}")
        _validate_record(record, opened_epochs)
        if kind in EPOCH_OPENING_KINDS:
            epoch_id = record.get("epoch_id")
            if epoch_id in opened_epochs:
                raise _PurseEnvelopeInvalid(f"epoch id {epoch_id!r} opened twice")
            opened_epochs.add(epoch_id)
        if kind == "fills_rollup":
            epoch_id = record.get("epoch_id")
            if epoch_id in rollup_epochs:
                raise _PurseEnvelopeInvalid(
                    f"epoch {epoch_id!r} has more than one fills_rollup record"
                )
            rollup_epochs.add(epoch_id)

    # 8. Top-level sequence == the highest record seq (:608-613).
    sequence = payload.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != prev_seq:
        raise _PurseEnvelopeInvalid(
            f"purse journal 'sequence' {sequence!r} does not match the highest record seq "
            f"({prev_seq})"
        )

    # 9. The journal must BEGIN with an opening_epoch (:614-618). An on-disk journal
    #    that does not (including records: []) is the forbidden start-empty fallback.
    if not records or records[0].get("kind") != "opening_epoch":
        raise _PurseEnvelopeInvalid("purse journal must begin with an opening_epoch record")


def classify_purse_envelope(
    payload,
    *,
    canonical_controller_id: str,
    staged_config: dict,
) -> PurseVerdict:
    """Classify a PARSED purse-journal payload against the engine's contract v1.

    Split from the file read/JSON parse the same way the ledger contract and the
    engine split syntax from envelope: the caller
    (``services.resume_service._validate_purse``) owns the read errors, zero-length,
    and JSON-syntax failures; this owns the envelope.

    Args:
        payload: The parsed JSON payload — ANY type. Untrusted disk state, so ``{}``,
            ``[]``, ``None`` and every other shape must yield a verdict, never an
            exception.
        canonical_controller_id: The C2-CANONICAL (stripped) staged id, compared
            EXACTLY against the journal's ``controller_id``. Taken as a parameter so
            this function cannot see an unvalidated id.
        staged_config: The staged controller's raw config dict (the YAML as read).
            Its ``controller_name``/``trading_pair`` are resolved against
            :data:`ENGINE_IDENTITY_DEFAULTS` and compared — never skipped.

    Returns:
        A :class:`PurseVerdict`. Never raises: the caller maps the verdict to its own
        fail-closed action (a structured ``PURSE_INVALID`` abort), so a hostile
        journal yields a structured refusal rather than an opaque 500.
    """
    try:
        _validate_document(payload, canonical_controller_id, staged_config)
    except _PurseEnvelopeInvalid as exc:
        return PurseVerdict(False, exc.reason)
    return PurseVerdict(True)
