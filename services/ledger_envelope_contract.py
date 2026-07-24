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
  * ``STATE_MAX_FUTURE_SKEW_SECONDS = 86400``        :1387
  * ``_safe_decimal``                                :165 — numeric field parsing
  * ``RangeInventoryLadderConfig`` identity defaults :192-206
  * ``split_hb_trading_pair``  hummingbot/connector/utils.py:29
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

Identity resolution: mirror the engine's defaults, never skip (CDX-R02)
-----------------------------------------------------------------------
The engine compares the ledger against a RESOLVED Pydantic config, while the API
holds only the staged YAML. An earlier revision of this module treated a field the
staged YAML omitted as "unknowable" and SKIPPED that comparison. That was
fail-open on exactly the axis this validator exists to close: the engine does not
skip anything — it resolves its model default and compares. A staged config with
no ``connector_name`` resolves to ``"binance"`` engine-side (:196), so a ledger
carrying ``"nonkyc"`` is a guaranteed engine-side mismatch → quarantine → wallet
re-seed, and the skip blessed it.

The defaults are not a guess: they are read from the engine's config model exactly
as :data:`REQUIRED_LEDGER_KEYS` and :data:`SUPPORTED_LEDGER_SCHEMA_VERSIONS` are
read from its validator, and they carry the same sync obligation. See
:data:`ENGINE_IDENTITY_DEFAULTS`. Resolution is therefore TOTAL — every identity
field yields an expected value, and every comparison runs:

  * key absent from the staged YAML  -> the engine's model default
  * present, non-blank ``str``       -> that value, compared VERBATIM
  * present but non-``str`` or blank -> ``LEDGER_INVALID`` (uncertainty). Pydantic
    v2 rejects ``None``/non-``str`` for a ``str`` field, so such a config never
    resolves engine-side at all; a blank can never match a valid ledger's
    non-empty field. Either way the deploy is already doomed — refuse it here,
    where refusing is still free.

What this module deliberately does NOT check (honest limits)
-------------------------------------------------------------
The API cannot fully replicate the engine's gate, and pretending otherwise would
be worse than the gap. This validator is NECESSARY, not SUFFICIENT: it proves a
ledger is known-bad, never that it will load. It does not RUN the v9->v10
migration (:2531-2545) — it cannot derive a missing ``owned_quote`` — nor touch
anything requiring live exchange/wallet state. It DOES now validate those v10
monetary fields when PRESENT (F7, :data:`OPTIONAL_MONETARY_LEDGER_FIELDS`) and the
``booked_fill_progress`` map (CLA-M03), because both are cases the engine
quarantines on and the old six-field mirror silently blessed. The other optional
v10 keys the engine batch added (``purse_initialized``, ``reanchor_events``,
``reanchor_offset_*``, ``init_unavailable_*``, ``last_flow_token`` …) are
accept-and-ignored here: unknown-key tolerance is unchanged, so a widened engine
that quarantines one of THEM is a known residual, not a claim this mirror covers
it. The controller-owned PURSE journal is a wholly separate contract with its own
version — see :mod:`services.purse_envelope_contract`; it does NOT ride this
envelope and is not coupled to :data:`SUPPORTED_LEDGER_SCHEMA_VERSIONS` (F22).

The clock is the caller's (:2049-2054)
---------------------------------------
The engine's future-skew check reads ``market_data_provider.time()`` — the BOT's
clock, which the API does not have. It is mirrored anyway, against a clock the
caller injects, because the engine's tolerance is a full day
(``STATE_MAX_FUTURE_SKEW_SECONDS`` = 86400, :1387): a valid ledger's
``initialized_timestamp`` lies in the PAST, so tripping this check on a real
ledger would need the API's clock to run >24h BEHIND the bot's — implausible for
two containers on one host, while the ledger it DOES catch (a timestamp far in the
future) is one the engine is guaranteed to quarantine. An unsupplied or
unparseable clock is uncertainty, and uncertainty is ``LEDGER_INVALID``.

Keeping this mirror in sync with the engine is a recorded, permanent obligation —
listed in the batch's final report. The obligation is not just the version set: it
now spans :data:`SUPPORTED_LEDGER_SCHEMA_VERSIONS`, :data:`ENGINE_IDENTITY_DEFAULTS`,
:data:`NUMERIC_LEDGER_FIELDS`, the F7-widened :data:`OPTIONAL_MONETARY_LEDGER_FIELDS`,
and the ``booked_fill_progress`` rules (CLA-M03) — every rule this module transcribes
from ``_validate_loaded_state``. The drift is asymmetric in both directions: a
widened/relaxed engine that is not mirrored here costs a FALSE ABORT (a valid deploy
refused — a lost deploy, recoverable); a NARROWED/stricter engine that is not mirrored
here costs a WALLET RE-SEED (the API blesses a ledger the engine quarantines — the F7
class of defect, unrecoverable accounting loss). The ``test_mirror_matches_engine_*``
suite is what makes a broken sync loud instead of silent.
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

# Mirrors the numeric loop at range_inventory_ladder.py:2507-2521: each is parsed
# with ``_safe_decimal`` (:167) and must be finite and non-negative (:2519). All
# six are in :data:`REQUIRED_LEDGER_KEYS`, so by the time the numeric loop runs
# they are guaranteed present — their absence is already a missing-key rejection.
NUMERIC_LEDGER_FIELDS = (
    "reserve_quote_balance",
    "reserve_base_balance",
    "initial_managed_quote",
    "initial_claimed_base_amount",
    "initial_reference_price",
    "initialized_timestamp",
)

# F7 (CDX-008/CLA-003) — the v10 monetary ledger fields the engine ALSO validates
# and quarantines on, at a SEPARATE site from the six above
# (range_inventory_ladder.py:2551-2558). They are NOT required keys: the engine's
# v9->v10 migration DERIVES them when absent (:2531-2545), so a state file that
# omits them is still valid. But a PRESENT value that is non-finite or negative is
# ``LEDGER_INVALID`` (:2553-2557) — exactly the case the old six-field mirror
# missed, which let the API bless a ledger the engine then quarantines and
# wallet-reseeds (the F7 defect this widening closes). Kept a distinct tuple, not
# folded into :data:`NUMERIC_LEDGER_FIELDS`, precisely because their ABSENCE
# semantics differ: the six required fields must be present, these three may be
# absent. Folding them in would make an absent (migration-derived) field a false
# abort — mirror drift in the reject-too-much direction. MUST be kept in sync with
# the engine, the same standing obligation as the version set and the defaults.
OPTIONAL_MONETARY_LEDGER_FIELDS = (
    "owned_quote",
    "owned_base",
    "seed_value_quote",
)

# The engine's RESOLVED config defaults for the identity fields, mirrored from
# ``RangeInventoryLadderConfig`` (range_inventory_ladder.py:192-206):
#     controller_name: str = "range_inventory_ladder"   :192
#     controller_type: str = "market_making"            :193
#     connector_name:  str = Field(default="binance")   :196
#     trading_pair:    str = Field(default="ETH-USDT")  :203
# A staged YAML that omits one of these is NOT "unknowable" — the engine resolves
# exactly these values and compares the ledger against them. MUST be kept in sync
# with the engine, the same standing obligation as the version set above.
ENGINE_IDENTITY_DEFAULTS = {
    "controller_name": "range_inventory_ladder",
    "controller_type": "market_making",
    "connector_name": "binance",
    "trading_pair": "ETH-USDT",
}

# Mirrors range_inventory_ladder.py:1387 — the engine's future-skew tolerance for
# ``initialized_timestamp`` (:2049-2054). One day.
STATE_MAX_FUTURE_SKEW_SECONDS = Decimal("86400")


def _split_hb_trading_pair(trading_pair: str):
    """Mirror of ``split_hb_trading_pair`` (hummingbot/connector/utils.py:29).

    The engine's body is ``base, quote = trading_pair.split("-")`` — a split on
    EVERY hyphen, unpacked into exactly two names. So "XMR-USDT" splits, while
    "XMRUSDT" (0 hyphens) and "A-B-C" (2) raise ValueError at the unpack. That
    ValueError is raised at :1966, inside ``_validate_loaded_state`` and before any
    ledger key is read — i.e. it quarantines the state just like every other
    rejection here, which is why this mirror maps it to LEDGER_INVALID.

    Raises:
        ValueError: exactly where the engine's unpack does.
    """
    base, quote = trading_pair.split("-")
    return base, quote


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


def _mirror_safe_decimal(value, field_name: str, default: Optional[str] = None) -> Decimal:
    """Mirror of the engine's ``_safe_decimal`` (range_inventory_ladder.py:167).

    Transcribed rather than imported: the API cannot import engine code. Kept
    decision-for-decision, including the ``is_finite`` check — JSON parses ``NaN``
    and ``Infinity`` by default, and a non-finite reserve balance would poison
    every figure the controller derives.

    The ``default`` argument mirrors the engine's own (:167-171): a MISSING value
    (``None``/``""``) resolves to ``Decimal(default)`` when a default is supplied,
    else it raises. The default covers ONLY ``None``/``""`` — a PRESENT but
    non-parseable/non-finite value still raises even when a default is given
    (CLA-M03: the engine's booking read is ``_safe_decimal(entry.get(k), ...,
    default="0")`` at :2593, so a missing ``base``/``quote``/``fees`` sub-key is a
    valid 0 while a garbage one quarantines). The six REQUIRED numeric fields pass
    no default (their absence is already a missing-required-key rejection), so they
    keep the strict blank-raises behaviour.

    Raises:
        ValueError: exactly where the engine's raises.
    """
    if value is None or value == "":
        if default is not None:
            return Decimal(default)
        raise ValueError(f"{field_name} cannot be blank")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid decimal value for {field_name}: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal value")
    return parsed


def _resolve_expected_identity(staged_config: dict, field_name: str):
    """Resolve what the ENGINE's config will hold for ``field_name``.

    Total by construction — there is no "skip this comparison" outcome, because the
    engine has none (see the module docstring's identity-resolution section).

    Returns:
        ``(value, None)`` with the value the engine will compare against, or
        ``(None, verdict)`` when the staged value is one the engine could never
        resolve (non-``str``, blank) — uncertainty, hence ``LEDGER_INVALID``.
    """
    if field_name not in staged_config:
        # Absent -> the engine resolves its model default and compares against it.
        return ENGINE_IDENTITY_DEFAULTS[field_name], None

    value = staged_config[field_name]
    if not isinstance(value, str) or not value.strip():
        return None, _invalid(
            f"staged controller's {field_name} is {value!r}, which is not a usable "
            f"identity value: the engine's config model types this field as a bare "
            f"`str` (range_inventory_ladder.py:192-206), so Pydantic v2 rejects "
            f"None/non-str outright and a blank could never match a valid ledger's "
            f"non-empty {field_name}. The ledger's identity cannot be established "
            f"against this config — refusing on uncertainty rather than skipping the "
            f"comparison (CDX-M02/CDX-R02, fail-closed)."
        )
    # Present -> compared VERBATIM. The engine applies no strip to these fields
    # (unlike C2's `id`), so staged " binance " genuinely mismatches ledger
    # "binance" engine-side, and this mirror must say so rather than tidy it up.
    return value, None


def _check_identity(
    payload: dict,
    canonical_controller_id: str,
    staged_config: dict,
) -> Optional[LedgerVerdict]:
    """The engine's identity comparisons (:2005-2028), all of them.

    ``controller_id`` is compared EXACTLY against the C2-canonical staged id —
    the ledger's copy is never stripped. See the module docstring's DIVERGENCES:
    stripping here would bless a ledger the engine quarantines.

    Every other identity field is resolved against the staged config or the
    engine's model default and then compared; nothing is skipped.
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

    # Engine order: controller_name (:2005), controller_type (:2009), connector_name
    # (:2017), trading_pair (:2021).
    for field_name in ("controller_name", "controller_type", "connector_name", "trading_pair"):
        expected_value, failure = _resolve_expected_identity(staged_config, field_name)
        if failure is not None:
            return failure
        if payload.get(field_name) != expected_value:
            return _invalid(
                f"ledger's {field_name} {payload.get(field_name)!r} does not match the staged "
                f"controller's {expected_value!r}; the engine rejects this mismatch at load "
                f"(range_inventory_ladder.py:2005-2024) and quarantines the state."
            )

    # base_asset / quote_asset vs the trading pair (:2025-2028). The engine derives
    # these from its CONFIG's pair (:1966) — not the ledger's — so this mirror
    # derives from the resolved expected pair too. The ledger's own trading_pair
    # already had to equal it to get here, so the two are the same string by now.
    expected_pair, failure = _resolve_expected_identity(staged_config, "trading_pair")
    if failure is not None:
        return failure
    try:
        expected_base, expected_quote = _split_hb_trading_pair(expected_pair)
    except ValueError:
        return _invalid(
            f"staged controller's trading_pair {expected_pair!r} cannot be split into a "
            f"base and a quote asset; the engine's split_hb_trading_pair "
            f"(hummingbot/connector/utils.py:29) raises on it at "
            f"range_inventory_ladder.py:1966, quarantining the state before it reads a "
            f"single ledger key."
        )
    if payload.get("base_asset") != expected_base or payload.get("quote_asset") != expected_quote:
        return _invalid(
            f"ledger's assets {payload.get('base_asset')!r}-{payload.get('quote_asset')!r} do "
            f"not match {expected_base!r}-{expected_quote!r} as split from the trading pair "
            f"{expected_pair!r}; the engine rejects this at range_inventory_ladder.py:2025-2028 "
            f"and quarantines the state. A ledger whose assets contradict its own pair is "
            f"not this controller's ledger."
        )
    return None


def classify_ledger_envelope(
    payload,
    *,
    canonical_controller_id: str,
    staged_config: dict,
    now_timestamp,
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
        staged_config: The staged controller's raw config dict (the YAML as read).
            Its identity fields are resolved against :data:`ENGINE_IDENTITY_DEFAULTS`
            and compared — never skipped. See the module docstring.
        now_timestamp: The caller's clock, as seconds since the epoch (anything
            ``Decimal(str(...))`` accepts). Injected rather than read here so the
            skew check is deterministic under test. None/unparseable is
            uncertainty -> invalid.

    Returns:
        A :class:`LedgerVerdict`. Never raises: callers map the verdict to their
        own fail-closed action, so a hostile ledger yields a structured 409 rather
        than an opaque 500.
    """
    # 0. The staged config is the yardstick every identity comparison is measured
    #    against. If it is not even a mapping we cannot establish identity at all.
    if not isinstance(staged_config, dict):
        return _invalid(
            f"staged controller config must be a mapping (got "
            f"{type(staged_config).__name__}); the ledger's identity cannot be "
            f"established without it — refusing on uncertainty."
        )

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

    identity_failure = _check_identity(payload, canonical_controller_id, staged_config)
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
    parsed_numerics = {}
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
        parsed_numerics[field_name] = parsed

    # 7. initialized_timestamp future-skew. Mirrors :2049-2054 against the caller's
    #    clock — see the module docstring for why the API's clock is close enough
    #    at a 1-day tolerance, and why an unusable clock is a refusal.
    try:
        now = _mirror_safe_decimal(now_timestamp, "now_timestamp")
    except ValueError as exc:
        return _invalid(
            f"the current time could not be established to check the ledger's "
            f"initialized_timestamp against the engine's future-skew limit ({exc}); "
            f"refusing on uncertainty rather than skipping the check."
        )
    initialized_timestamp = parsed_numerics["initialized_timestamp"]
    if initialized_timestamp > (now + STATE_MAX_FUTURE_SKEW_SECONDS):
        return _invalid(
            f"ledger's initialized_timestamp {initialized_timestamp} is unreasonably far in "
            f"the future (more than {STATE_MAX_FUTURE_SKEW_SECONDS}s past the current "
            f"{now}); the engine rejects it at range_inventory_ladder.py:2049-2054 "
            f"(STATE_MAX_FUTURE_SKEW_SECONDS, :1387) and quarantines the state."
        )

    # 8. F7 — the v10 monetary fields (owned_quote/owned_base/seed_value_quote).
    #    ABSENT is valid: the engine's v9->v10 migration derives them
    #    (range_inventory_ladder.py:2531-2545) BEFORE re-validating them, so a state
    #    file that omits them still loads. PRESENT-but-invalid (non-finite / negative)
    #    is the case the engine quarantines at :2551-2558 — and the case the old
    #    six-field mirror silently blessed, letting the API say "yes" to a ledger the
    #    engine then quarantines and wallet-reseeds. Same parse discipline as the six.
    for field_name in OPTIONAL_MONETARY_LEDGER_FIELDS:
        if field_name not in payload:
            continue  # engine derives it via v9->v10 migration; absent is valid.
        try:
            parsed = _mirror_safe_decimal(payload.get(field_name), f"state field '{field_name}'")
        except ValueError as exc:
            return _invalid(
                f"ledger's {field_name} is not a valid decimal ({exc}); the engine validates "
                f"this v10 monetary field at range_inventory_ladder.py:2553 and quarantines "
                f"the state on a non-finite value (F7). A missing field is fine — the engine's "
                f"v9->v10 migration derives it (:2531-2545) — but a present garbage one is not."
            )
        if parsed < Decimal("0"):
            return _invalid(
                f"ledger's {field_name} must be non-negative (got {parsed}); the engine rejects "
                f"it at range_inventory_ladder.py:2556-2557 and quarantines the state (F7)."
            )

    # 9. CLA-M03 — booked_fill_progress. An OPTIONAL v10 key: absent/None is a valid
    #    absence (the engine's booking read treats it as {}). But a value that loads
    #    clean yet is unbookable — a non-dict payload, a non-string key, an entry that
    #    is not a dict, or a base/quote/fees that is non-finite / negative — would
    #    raise EVERY booking cycle engine-side, silently killing booking while trading
    #    continues, AND ride copy-forward to the next deploy. The engine now quarantines
    #    it at load (range_inventory_ladder.py:2571-2602); mirror that so the API refuses
    #    the deploy while refusing is still free. A MISSING base/quote/fees sub-key is
    #    fine (the engine defaults it to 0 via ``_safe_decimal(..., default="0")`` at
    #    :2593) — rejecting it would be a false abort on a ledger the engine loads.
    progress = payload.get("booked_fill_progress")
    if progress is None:
        pass  # absent/None -> valid (engine: :2572-2577).
    elif not isinstance(progress, dict):
        return _invalid(
            f"ledger's booked_fill_progress must be a JSON object (got "
            f"{type(progress).__name__}); the engine rejects it at "
            f"range_inventory_ladder.py:2578-2579 and quarantines the state (CLA-M03)."
        )
    else:
        for entry_key, entry_val in progress.items():
            if not isinstance(entry_key, str):
                return _invalid(
                    f"ledger's booked_fill_progress key {entry_key!r} must be a string; the "
                    f"engine rejects it at range_inventory_ladder.py:2582-2585 (CLA-M03)."
                )
            if not isinstance(entry_val, dict):
                return _invalid(
                    f"ledger's booked_fill_progress[{entry_key!r}] entry must be a JSON object "
                    f"(got {type(entry_val).__name__}); the engine rejects it at "
                    f"range_inventory_ladder.py:2586-2589 and quarantines the state (CLA-M03)."
                )
            for money_key in ("base", "quote", "fees"):
                # default="0" mirrors the engine's booking read (:2593): a MISSING sub-key
                # is a valid 0, a PRESENT non-numeric/non-finite one raises.
                try:
                    parsed = _mirror_safe_decimal(
                        entry_val.get(money_key),
                        f"booked_fill_progress[{entry_key}].{money_key}",
                        default="0",
                    )
                except ValueError as exc:
                    return _invalid(
                        f"ledger's booked_fill_progress[{entry_key!r}].{money_key} is not a "
                        f"valid decimal ({exc}); the engine parses it with _safe_decimal at "
                        f"range_inventory_ladder.py:2593 and quarantines the state on a "
                        f"non-finite value (CLA-M03)."
                    )
                if parsed < Decimal("0"):
                    return _invalid(
                        f"ledger's booked_fill_progress[{entry_key!r}].{money_key} must be "
                        f"non-negative (got {parsed}); the engine rejects it at "
                        f"range_inventory_ladder.py:2598-2601 (CLA-M03)."
                    )

    return LedgerVerdict(True)
