"""CDX-M02 — the versioned ledger-envelope contract, API half.

The ONE api-side predicate for a range-ladder ledger's ENVELOPE, in a module of
its own for the same reason as its C1/C2 siblings (``state_file_contract.py``,
``controller_id_contract.py``): the contract is shared with the engine, and a
single home is what lets "the API and the engine agree" be checked by reading two
files rather than grepping for lookalikes.

The engine half is not ours to write — it already exists, and it is the SPEC this
module mirrors. Every rule below is transcribed from
``hummingbot/controllers/market_making/range_inventory_ladder.py``:

  * ``RangeInventoryLadder._validate_loaded_state``  :1962 — the load-time gate
  * ``STATE_SCHEMA_VERSION = 10``                    :1385
  * ``SUPPORTED_STATE_SCHEMA_VERSIONS = {6..10}``    :1386
  * ``_safe_decimal``                                :165 — numeric field parsing
  * the state WRITER                                 :2329 — ``schema_version`` is
    written as an ``int``

Why the API validates a ledger the engine will validate anyway
--------------------------------------------------------------
Because by the time the engine says no, the money is already gone. The engine's
rejection path is QUARANTINE: ``_validate_loaded_state`` raises, the state file is
renamed aside (:1953), and the controller re-initializes **from current wallet
balances** — the insufficient-funds bug this whole hook exists to prevent. The
API is the only layer that can still refuse the deploy while refusing is free.

So the two layers reject the same ledgers for opposite reasons: the engine to
protect its own state machine, the API to protect the operator from a deploy that
would silently re-seed. The old api-side check was length + UTF-8 + ``json.loads``
— it proved the bytes were JSON, nothing more. ``{"levels": [1, 2]}`` sailed
through it and quarantined on the far side.

The rollback trigger (why the version check is the point)
---------------------------------------------------------
Forward drift migrates fine (:2091-2093 stamps and records
``migrated_from_schema_version``). A ROLLBACK does not: an engine at v10 writes a
v10 ledger, the image is rolled back to a pre-v6 engine, and :1999 raises
"Unsupported state schema_version" → quarantine → wallet re-seed. Rollback is
exactly what follows a bad rebuild, and mutable image tags (CDX-012, accepted
out-of-scope) make the landing version unpredictable. That interaction is why
this validator exists.

Deliberate DIVERGENCES from the engine (both strictly stricter)
----------------------------------------------------------------
1. ``schema_version`` must be a real ``int``. The engine coerces
   (``int(raw.get("schema_version"))``, :1995), so the engine would accept the
   string ``"10"``. The engine's WRITER only ever emits an ``int`` (:2329) — so a
   string here means the file was not written by the engine, and an envelope of
   unknown provenance is uncertainty. Uncertainty is ``LEDGER_INVALID``, not a
   coercion. Stricter than the engine is safe (a false abort costs a deploy);
   looser is not (it costs the wallet).
2. The ledger's internal ``controller_id`` is compared EXACTLY, never stripped.
   This is not an oversight — it is the whole point. The engine compares
   ``raw.get("controller_id") != self.config.id`` (:2013) against an id that C2
   already canonicalized (stripped) engine-side, and it does NOT strip the
   LEDGER's copy. So a ledger carrying ``" ctrl_a "`` against config id
   ``"ctrl_a"`` is a MISMATCH to the engine → quarantine → re-seed. If this
   module stripped the ledger's id and called it a match, the API would bless a
   ledger the engine is guaranteed to reject: fail-open, and precisely the class
   of bug CDX-M02 was filed for. See :func:`_check_identity`.

What this module deliberately does NOT check (honest limits)
-------------------------------------------------------------
The API cannot fully replicate the engine's gate, and pretending otherwise would
be worse than the gap. This validator is NECESSARY, not SUFFICIENT: it proves a
ledger is known-bad, never that it will load.

  * ``base_asset``/``quote_asset`` vs ``trading_pair`` — the engine derives these
    with ``split_hb_trading_pair`` (:1966), engine code the API cannot import.
    Guessing its semantics ("split on the first '-'") would abort real deploys on
    any pair whose format we guessed wrong. Presence and type are enforced; the
    derivation is not re-implemented.
  * ``initialized_timestamp`` future-skew (:2051) — needs the engine's
    ``market_data_provider.time()``, i.e. the bot's clock, not the API's.
  * Engine model DEFAULTS — the engine compares the ledger against a RESOLVED
    Pydantic config; the API holds only the staged YAML. Where the staged config
    omits a field, the comparison is skipped rather than guessed (see the
    ``expected_*`` parameters).

Keeping :data:`SUPPORTED_LEDGER_SCHEMA_VERSIONS` in sync with the engine is a
recorded, permanent obligation of this mirror — it is listed in the batch's final
report. A widened engine set that is not mirrored here costs a false abort; a
NARROWED engine set that is not mirrored here costs a wallet re-seed.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


# Mirrors range_inventory_ladder.py:1386 (``SUPPORTED_STATE_SCHEMA_VERSIONS``).
# The engine's current writer emits STATE_SCHEMA_VERSION = 10 (:1385).
# MUST be kept in sync with the engine — see the module docstring.
SUPPORTED_LEDGER_SCHEMA_VERSIONS = frozenset({6, 7, 8, 9, 10})

# Mirrors the ``required_keys`` set in ``_validate_loaded_state``
# (range_inventory_ladder.py:1967-1983). A ledger missing any of these raises
# engine-side (:1992) -> quarantine -> wallet re-seed.
REQUIRED_LEDGER_KEYS = frozenset(
    {
        "schema_version",
        "controller_name",
        "controller_type",
        "controller_id",
        "connector_name",
        "trading_pair",
        "base_asset",
        "quote_asset",
        "initialized",
        "reserve_quote_balance",
        "reserve_base_balance",
        "initial_managed_quote",
        "initial_claimed_base_amount",
        "initial_reference_price",
        "initialized_timestamp",
    }
)

# The identity/string fields the engine compares against its resolved config
# (range_inventory_ladder.py:2005-2028). Order is the engine's.
_STRING_LEDGER_FIELDS = (
    "controller_name",
    "controller_type",
    "controller_id",
    "connector_name",
    "trading_pair",
    "base_asset",
    "quote_asset",
)

# Mirrors the numeric loop at range_inventory_ladder.py:2033-2047: each is parsed
# with ``_safe_decimal`` (:165) and must be finite and non-negative (:2045).
NUMERIC_LEDGER_FIELDS = (
    "reserve_quote_balance",
    "reserve_base_balance",
    "initial_managed_quote",
    "initial_claimed_base_amount",
    "initial_reference_price",
    "initialized_timestamp",
)


@dataclass(frozen=True)
class LedgerVerdict:
    """The outcome of :func:`classify_ledger_envelope`.

    Attributes:
        is_valid: True only when every mirrored engine rule passed.
        reason: Operator-facing explanation naming the exact rule and the engine
            line it mirrors. Empty for accepted envelopes.
    """

    is_valid: bool
    reason: str = ""


def _invalid(reason: str) -> LedgerVerdict:
    return LedgerVerdict(False, reason)


def _mirror_safe_decimal(value, field_name: str) -> Decimal:
    """Mirror of the engine's ``_safe_decimal`` (range_inventory_ladder.py:165).

    Transcribed rather than imported: the API cannot import engine code. Kept
    decision-for-decision, including the blank rejection (no ``default`` is passed
    at the engine's call site :2042, so ``None``/``""`` raise) and the
    ``is_finite`` check — JSON parses ``NaN`` and ``Infinity`` by default, and a
    non-finite reserve balance would poison every figure the controller derives.

    Raises:
        ValueError: exactly where the engine's raises.
    """
    if value is None or value == "":
        raise ValueError(f"{field_name} cannot be blank")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid decimal value for {field_name}: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal value")
    return parsed


def _check_identity(
    payload: dict,
    canonical_controller_id: str,
    expected: dict,
) -> Optional[LedgerVerdict]:
    """The engine's identity comparisons (:2005-2024), minus what we cannot know.

    ``controller_id`` is compared EXACTLY against the C2-canonical staged id —
    the ledger's copy is never stripped. See the module docstring's DIVERGENCES:
    stripping here would bless a ledger the engine quarantines.

    The remaining fields are compared only where the caller supplied an expected
    value (i.e. the staged config carried the field). Skipping an unknowable
    comparison is honest; inventing the engine's model default is not.
    """
    ledger_id = payload.get("controller_id")
    if ledger_id != canonical_controller_id:
        return _invalid(
            f"ledger's internal controller_id {ledger_id!r} does not match the staged "
            f"controller's canonical id {canonical_controller_id!r} — this is a different "
            f"controller's ledger. The engine compares these exactly "
            f"(range_inventory_ladder.py:2013) and quarantines on mismatch, re-seeding "
            f"from the wallet. Note the ledger's id is compared VERBATIM: the engine does "
            f"not strip it either, so a padded id is a genuine mismatch, not a formatting nit."
        )

    for field_name, expected_value in expected.items():
        if expected_value is None:
            continue  # staged config did not carry it -> unknowable, not assumed
        if payload.get(field_name) != expected_value:
            return _invalid(
                f"ledger's {field_name} {payload.get(field_name)!r} does not match the staged "
                f"controller's {expected_value!r}; the engine rejects this mismatch at load "
                f"(range_inventory_ladder.py:2005-2020) and quarantines the state."
            )
    return None


def classify_ledger_envelope(
    payload,
    *,
    canonical_controller_id: str,
    expected_controller_name: Optional[str] = None,
    expected_controller_type: Optional[str] = None,
    expected_connector_name: Optional[str] = None,
    expected_trading_pair: Optional[str] = None,
) -> LedgerVerdict:
    """Classify a PARSED ledger payload against the engine's envelope contract.

    Split from the file read/JSON parse for the same reason the engine splits
    ``_load_state`` (:2097) from ``_validate_loaded_state`` (:1962): syntax and
    envelope are different failures, and only the envelope is this contract's.

    Args:
        payload: The parsed JSON payload — ANY type. This is untrusted input read
            off a prior instance's disk; ``{}``, ``[]``, ``None`` and every other
            shape must yield a verdict, never an exception.
        canonical_controller_id: The C2-CANONICAL (stripped) staged id from
            ``classify_controller_id``. Taken as a parameter rather than read from
            the ledger or a raw config so this function cannot see an
            unvalidated id — the same reasoning as ``_plan_controller``'s.
        expected_controller_name: The staged config's ``controller_name``, or None
            when unknown. Same for the other ``expected_*`` values: None means
            "the staged config did not carry it", and the comparison is SKIPPED
            rather than guessed.

    Returns:
        A :class:`LedgerVerdict`. Never raises: callers map the verdict to their
        own fail-closed action, so a hostile ledger yields a structured 409 rather
        than an opaque 500.
    """
    # 1. Top-level shape. Mirrors :1963. Catches {}, [], null, scalars, and the
    #    old validator's blind spot: any JSON that parses is not an envelope.
    if not isinstance(payload, dict):
        return _invalid(
            f"ledger payload must be a JSON object (got {type(payload).__name__}); "
            f"the engine rejects this at range_inventory_ladder.py:1963."
        )

    # 2. Required header keys. Mirrors :1984-1992.
    missing = sorted(REQUIRED_LEDGER_KEYS - set(payload.keys()))
    if missing:
        pre_v6 = (
            " A ledger with no 'schema_version' predates engine v6; the engine "
            "quarantines it and re-initializes from current wallet balances "
            "(range_inventory_ladder.py:1986-1991)."
            if "schema_version" in missing
            else ""
        )
        return _invalid(
            f"ledger is missing required key(s): {', '.join(missing)}. The engine "
            f"requires all of {sorted(REQUIRED_LEDGER_KEYS)} "
            f"(range_inventory_ladder.py:1967-1983).{pre_v6}"
        )

    # 3. schema_version: a real int, in the mirrored supported set.
    #    ``bool`` is excluded explicitly — ``isinstance(True, int)`` is True in
    #    Python, so ``schema_version: true`` would otherwise reach the membership
    #    test as 1. Stricter than the engine's int() coercion, deliberately (see
    #    the module docstring's DIVERGENCES).
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return _invalid(
            f"ledger's schema_version must be an int (got {type(version).__name__} "
            f"{version!r}); the engine's writer only ever emits an int "
            f"(range_inventory_ladder.py:2329), so any other type means this file was "
            f"not written by the engine — refusing on uncertainty."
        )
    if version not in SUPPORTED_LEDGER_SCHEMA_VERSIONS:
        return _invalid(
            f"ledger's schema_version {version} is not supported; the engine accepts "
            f"{sorted(SUPPORTED_LEDGER_SCHEMA_VERSIONS)} (mirrored from "
            f"range_inventory_ladder.py:1386) and raises 'Unsupported state "
            f"schema_version' otherwise (:1999), quarantining the state and re-seeding "
            f"from the wallet. A ledger from a NEWER engine landing on an older one is "
            f"the rollback case this check exists for."
        )

    # 4. Identity/string field types, then the engine's identity comparisons.
    for field_name in _STRING_LEDGER_FIELDS:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return _invalid(
                f"ledger's {field_name} must be a non-empty string (got "
                f"{type(value).__name__} {value!r}); the engine compares this field "
                f"against its resolved config (range_inventory_ladder.py:2005-2028)."
            )

    identity_failure = _check_identity(
        payload,
        canonical_controller_id,
        {
            "controller_name": expected_controller_name,
            "controller_type": expected_controller_type,
            "connector_name": expected_connector_name,
            "trading_pair": expected_trading_pair,
        },
    )
    if identity_failure is not None:
        return identity_failure

    # 5. initialized must be exactly True. Mirrors :2029 (``is not True``), so a
    #    truthy 1 / "yes" is a rejection here exactly as it is engine-side.
    if payload.get("initialized") is not True:
        return _invalid(
            f"ledger's initialized flag must be exactly true (got "
            f"{payload.get('initialized')!r}); the engine requires this at "
            f"range_inventory_ladder.py:2029. An uninitialized ledger carries no "
            f"reserve accounting to resume from."
        )

    # 6. Numeric fields: parseable, finite, non-negative. Mirrors :2033-2047.
    for field_name in NUMERIC_LEDGER_FIELDS:
        try:
            parsed = _mirror_safe_decimal(payload.get(field_name), f"state field '{field_name}'")
        except ValueError as exc:
            return _invalid(
                f"ledger's {field_name} is not a valid decimal ({exc}); the engine "
                f"rejects it at range_inventory_ladder.py:2042 and quarantines the state."
            )
        if parsed < Decimal("0"):
            return _invalid(
                f"ledger's {field_name} must be non-negative (got {parsed}); the engine "
                f"rejects it at range_inventory_ladder.py:2045-2046."
            )

    return LedgerVerdict(True)
