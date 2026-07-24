"""Spec-derived ledger fixtures (CDX-M02).

Every value here is transcribed from the ENGINE's load-time contract —
``hummingbot/controllers/market_making/range_inventory_ladder.py`` — and NEVER
captured by running the API's validator. That direction matters: a fixture
captured from the implementation proves only that the implementation agrees with
itself, and would keep passing if both drifted away from the engine together.

Sources (read-only):
  * ``_validate_loaded_state``           :1962 — the gate this envelope must pass
  * ``required_keys``                    :1967-1983 — the 15 keys below
  * ``SUPPORTED_STATE_SCHEMA_VERSIONS``  :1386 — {6..10}
  * ``STATE_SCHEMA_VERSION = 10``        :1385 — what the writer stamps (:2329)
  * the numeric loop                     :2033-2047 — finite, non-negative
  * ``initialized`` must be True         :2029

The payload built here is MINIMAL: exactly the engine's required keys and nothing
else. A real v10 ledger carries far more (positions, executors, owned_quote,
...), but the engine's *validator* requires only these — so this is the smallest
payload the engine would accept, which is precisely what a boundary fixture
should be. Extra keys are the caller's to add via ``**overrides``.
"""

# Mirrors range_inventory_ladder.py:1385 — what a current engine writes (:2329).
CURRENT_LEDGER_SCHEMA_VERSION = 10

# The controller_name the resume hook filters staged configs on
# (resume_service.RANGE_LADDER_CONTROLLER_NAME) and the controller_type the
# engine's config carries (a market-making controller).
LEDGER_CONTROLLER_NAME = "range_inventory_ladder"
LEDGER_CONTROLLER_TYPE = "market_making"


def valid_ledger_payload(
    controller_id="ctrl_a",
    *,
    connector_name="nonkyc",
    trading_pair="XMR-USDT",
    schema_version=CURRENT_LEDGER_SCHEMA_VERSION,
    **overrides,
):
    """Build the MINIMAL ledger payload the engine's validator accepts.

    Args:
        controller_id: The ledger's INTERNAL controller_id. The engine compares it
            exactly against its config id (:2013) — no stripping on either side —
            so tests that want a mismatch pass a differing value here.
        connector_name / trading_pair: Identity fields the engine compares against
            its resolved config (:2017, :2021). ``base_asset``/``quote_asset`` are
            split from ``trading_pair`` exactly the way the engine's
            ``split_hb_trading_pair`` (hummingbot/connector/utils.py:29, called at
            :1966) does — ``split("-")`` unpacked into exactly two — so the fixture
            stays self-consistent AND so a pair the engine could not split raises
            here too, rather than being quietly papered over with a maxsplit.
        schema_version: Overridable so version-boundary tests (5 / 11) can build
            an otherwise-perfect envelope and vary ONE field.
        **overrides: Applied last — set a key to a bad value, or use
            ``payload.pop(key)`` at the call site to build a missing-key case.

    Returns:
        A fresh dict (never a shared module-level object — callers mutate these).
    """
    base_asset, quote_asset = trading_pair.split("-")
    payload = {
        # range_inventory_ladder.py:1967-1983 — the required_keys set, in order.
        "schema_version": schema_version,
        "controller_name": LEDGER_CONTROLLER_NAME,
        "controller_type": LEDGER_CONTROLLER_TYPE,
        "controller_id": controller_id,
        "connector_name": connector_name,
        "trading_pair": trading_pair,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        # :2029 — must be exactly True.
        "initialized": True,
        # :2033-2047 — parsed with _safe_decimal, must be finite and >= 0. Written
        # as strings because that is how the engine persists them (:2047 stores
        # ``str(parsed)``).
        "reserve_quote_balance": "0",
        "reserve_base_balance": "0",
        "initial_managed_quote": "100",
        "initial_claimed_base_amount": "0",
        "initial_reference_price": "150",
        "initialized_timestamp": "1700000000",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Purse-journal fixtures (hbpurseapi P1)
# ---------------------------------------------------------------------------
#
# Spec-derived from the PINNED "Purse journal contract v1" (the batch prompt) and
# cross-checked against the engine's ``controllers/_shared/purse_ledger.py`` —
# NEVER captured by running the API's minimal validator. A fixture built by
# running the validator proves only that the validator agrees with itself; these
# money values are transcribed from the contract so a validator that drifts from
# the contract is caught. Money fields are decimal STRINGS, exactly as the engine
# persists them.

# Mirrors purse_ledger.py:29 — the purse's own version, independent of the ledger.
PURSE_SCHEMA_VERSION = 1


def opening_epoch_record(seq=1, ts=1700000000.0, epoch_id="epoch-1", **overrides):
    """The MINIMAL ``opening_epoch`` record the PINNED contract requires.

    Every required field from the contract's ``opening_epoch`` kind is present
    (epoch_id, owned/wallet/unavailable quote+base, seed_value_quote,
    reference_price, contributed/earned opening quote, opening_basis_quality,
    predecessor, note) so this fixture is a genuine valid record — forward-
    compatible with P2's full per-kind validator, not just P1's structural core.

    Args:
        seq: 1-based record sequence (strictly increasing across records).
        ts: epoch seconds (a finite non-negative number).
        epoch_id: the epoch this record opens.
        **overrides: applied last — set a field to a bad value, or ``pop`` one at
            the call site to build a missing-field case.
    """
    record = {
        "seq": seq,
        "ts": ts,
        "kind": "opening_epoch",
        "epoch_id": epoch_id,
        "owned_quote": "100",
        "owned_base": "0",
        "seed_value_quote": "100",
        "reference_price": "150",
        "wallet_quote_total": "100",
        "wallet_base_total": "0",
        "unavailable_quote": "0",
        "unavailable_base": "0",
        "contributed_opening_quote": "100",
        "earned_opening_quote": "0",
        "opening_basis_quality": "current_equity_only",
        "predecessor": None,
        "note": "spec-derived opening epoch",
    }
    record.update(overrides)
    return record


def valid_purse_payload(
    controller_id="ctrl_a",
    *,
    controller_name="range_inventory_ladder",
    trading_pair="XMR-USDT",
    records=None,
    **overrides,
):
    """Build the MINIMAL valid purse journal the contract v1 defines.

    Top level = ``purse_schema_version``, ``controller_id``, ``controller_name``,
    ``trading_pair``, ``sequence`` (the highest record seq), ``records`` (an
    append-only list beginning with an ``opening_epoch``). ``sequence`` is derived
    from the records so a fixture stays self-consistent.

    Args:
        controller_id: the journal's INTERNAL controller_id — compared EXACTLY
            (no stripping) against the staged controller's canonical id, so a
            mismatch test passes a differing value here.
        controller_name / trading_pair: contract identity fields.
        records: override the record list (default: one ``opening_epoch``). Pass a
            multi-record list to exercise the monotonic-seq rule.
        **overrides: applied last to the top-level dict — set ``purse_schema_version``
            to 2, drop a key via ``pop`` at the call site, etc.

    Returns:
        A fresh dict (callers mutate these).
    """
    if records is None:
        records = [opening_epoch_record()]
    sequence = max((r.get("seq", 0) for r in records), default=0)
    payload = {
        "purse_schema_version": PURSE_SCHEMA_VERSION,
        "controller_id": controller_id,
        "controller_name": controller_name,
        "trading_pair": trading_pair,
        "sequence": sequence,
        "records": list(records),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Per-kind purse record fixtures (hbpurseapi P2)
# ---------------------------------------------------------------------------
#
# One builder per non-opening record kind, each carrying EXACTLY the fields the
# PINNED "Purse journal contract v1" requires for that kind (cross-checked against
# purse_ledger.py ``_MONEY_FIELDS`` :100-131 + ``_validate_record`` :621-690 —
# read-only). Money values are decimal STRINGS. ``fills_rollup``/``reanchor``/
# ``checkpoint`` reference an epoch that an ``opening_epoch``/``reseed_epoch`` must
# have opened earlier in the same journal, so their default ``epoch_id`` is
# ``"epoch-1"`` (the opening fixture's default). Every value is transcribed from the
# contract, never captured by running the validator.


def flow_record(seq=2, ts=1700000100.0, *, flow_kind="deposit", **overrides):
    """A ``flow`` record (deposit/withdrawal). Carries no epoch reference — the
    contract's flow kind is epoch-independent (purse_ledger.py:648-654)."""
    record = {
        "seq": seq,
        "ts": ts,
        "kind": "flow",
        "token": "flow-token-1",
        "flow_kind": flow_kind,
        "asset": "USDT",
        "native_amount": "50",
        "quote_valuation": "50",
        "valuation_price": "1",
        "valuation_ts": ts,
        "confirmation": "wallet_delta_matched",
    }
    record.update(overrides)
    return record


def fills_rollup_record(seq=2, ts=1700000100.0, *, epoch_id="epoch-1", **overrides):
    """A ``fills_rollup`` record. ``base_delta_cum``/``quote_delta_cum`` are SIGNED
    (bought-sold / received-spent), so they may be negative; ``fees_quote_cum`` is
    non-negative (purse_ledger.py:112-115)."""
    record = {
        "seq": seq,
        "ts": ts,
        "kind": "fills_rollup",
        "epoch_id": epoch_id,
        "base_delta_cum": "1",
        "quote_delta_cum": "-150",
        "fees_quote_cum": "0",
        "fills_seen": 3,
        "last_update_ts": ts,
    }
    record.update(overrides)
    return record


def reseed_epoch_record(seq=2, ts=1700000100.0, *, epoch_id="epoch-2",
                        prev_epoch_id="epoch-1", **overrides):
    """A ``reseed_epoch`` record — itself an epoch-OPENING kind (purse_ledger.py:37),
    so its ``epoch_id`` opens a new epoch that later records may reference."""
    record = {
        "seq": seq,
        "ts": ts,
        "kind": "reseed_epoch",
        "epoch_id": epoch_id,
        "prev_epoch_id": prev_epoch_id,
        "token": "reseed-token-1",
        "old_owned_quote": "100",
        "old_owned_base": "0",
        "old_seed_value_quote": "100",
        "new_owned_quote": "120",
        "new_owned_base": "0",
        "new_seed_value_quote": "120",
        "reference_price": "150",
    }
    record.update(overrides)
    return record


def reanchor_record(seq=2, ts=1700000100.0, *, epoch_id="epoch-1", **overrides):
    """A ``reanchor`` record. ``classification`` is one of the closed set
    (undeclared_outflow / drift, purse_ledger.py:34)."""
    record = {
        "seq": seq,
        "ts": ts,
        "kind": "reanchor",
        "epoch_id": epoch_id,
        "old_owned_quote": "100",
        "old_owned_base": "0",
        "new_owned_quote": "90",
        "new_owned_base": "0",
        "overclaim_quote": "10",
        "classification": "undeclared_outflow",
        "wallet_quote_total": "90",
        "wallet_base_total": "0",
    }
    record.update(overrides)
    return record


def checkpoint_record(seq=2, ts=1700000100.0, *, epoch_id="epoch-1", **overrides):
    """A ``checkpoint`` record — a wallet-observation snapshot referencing an open
    epoch (purse_ledger.py:126-130)."""
    record = {
        "seq": seq,
        "ts": ts,
        "kind": "checkpoint",
        "epoch_id": epoch_id,
        "owned_quote": "100",
        "owned_base": "0",
        "reference_price": "150",
        "equity_quote": "100",
        "wallet_quote_total": "100",
        "wallet_base_total": "0",
        "external_holds_quote": "0",
        "external_holds_base": "0",
    }
    record.update(overrides)
    return record
