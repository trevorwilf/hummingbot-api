"""hbpurseapi P2 — the formal PURSE-JOURNAL envelope contract, API half.

Two things are under test:

  * ``services.purse_envelope_contract.classify_purse_envelope`` — the api-side
    purse envelope predicate, tested directly as a spec table (this file). P1's
    plan call site delegates here; the plan-level wire-through (a formal-only
    rejection ABORTS the deploy) lives in ``test_copyforward_purse_plan.py``.
  * the MIRROR itself — that the constants this module copies from the engine
    (kinds, enums, money-field signs, version) still equal the engine's. A mirror
    nobody checks is a mirror that drifts.

EVERY expected value below is derived from the PINNED "Purse journal contract v1"
(the batch prompt) and the ENGINE's ``controllers/_shared/purse_ledger.py`` — read
read-only, never by running the API's implementation. Where a test needs a value
the engine defines, it is transcribed here with its engine line cited, so a
reviewer can check the transcription against the engine without trusting this
suite. The engine sources:

    PurseLedger._validate_document   :545   the whole-document gate
    PurseLedger._validate_record     :621   the per-kind field gate
    _require_known_epoch             :682   the epoch-reference gate
    PURSE_SCHEMA_VERSION = 1         :29
    OPENING_BASIS_QUALITIES          :31
    FLOW_KINDS / FLOW_CONFIRMATIONS  :32-33
    REANCHOR_CLASSIFICATIONS         :34
    EPOCH_OPENING_KINDS              :37
    KNOWN_KINDS                      :39-41
    _MONEY_FIELDS                    :100-131
    seq 1-based strictly-increasing  :573-590
    sequence == highest seq          :608-613
    must begin with opening_epoch    :614-618

No filesystem, no Docker, no DB — the predicate is pure computation over a parsed
payload.
"""

import pytest

from ledger_fixtures import (
    checkpoint_record,
    fills_rollup_record,
    flow_record,
    opening_epoch_record,
    reanchor_record,
    reseed_epoch_record,
    valid_purse_payload,
)
from services.purse_envelope_contract import (
    FLOW_CONFIRMATIONS,
    FLOW_KINDS,
    KNOWN_KINDS,
    OPENING_BASIS_QUALITIES,
    PURSE_MONEY_FIELDS,
    PURSE_SCHEMA_VERSION,
    REANCHOR_CLASSIFICATIONS,
    classify_purse_envelope,
)


# ---------------------------------------------------------------------------
# The ENGINE's contract, transcribed by hand from read-only purse_ledger.py.
# These are the SPEC — deliberately NOT imported from the implementation, so
# asserting the implementation against them cannot pass if both drifted together.
# ---------------------------------------------------------------------------

# purse_ledger.py:29
ENGINE_PURSE_SCHEMA_VERSION = 1

# purse_ledger.py:39-41
ENGINE_KNOWN_KINDS = {
    "opening_epoch", "flow", "fills_rollup", "reseed_epoch", "reanchor", "checkpoint",
}

# purse_ledger.py:31-34
ENGINE_OPENING_BASIS_QUALITIES = ("reconstructed", "current_equity_only")
ENGINE_FLOW_KINDS = ("deposit", "withdrawal")
ENGINE_FLOW_CONFIRMATIONS = ("wallet_delta_matched", "drift")
ENGINE_REANCHOR_CLASSIFICATIONS = ("undeclared_outflow", "drift")

# purse_ledger.py:100-131 — per-kind money fields; True == pinned non-negative,
# False == signed (may be negative). Transcribed field-for-field.
ENGINE_MONEY_FIELDS = {
    "opening_epoch": {
        "owned_quote": True, "owned_base": True, "seed_value_quote": True,
        "reference_price": True, "wallet_quote_total": True, "wallet_base_total": True,
        "unavailable_quote": True, "unavailable_base": True,
        "contributed_opening_quote": True, "earned_opening_quote": False,
    },
    "flow": {"native_amount": True, "quote_valuation": True, "valuation_price": True},
    "fills_rollup": {
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

# The PINNED contract's non-money required fields per kind (batch prompt "Records"),
# i.e. everything besides seq/ts/kind and the money fields above. Transcribed from
# the contract, cross-checked against _validate_record (:628-680).
ENGINE_NON_MONEY_REQUIRED = {
    "opening_epoch": ["epoch_id", "opening_basis_quality", "predecessor", "note"],
    "flow": ["token", "flow_kind", "asset", "valuation_ts", "confirmation"],
    "fills_rollup": ["epoch_id", "fills_seen", "last_update_ts"],
    "reseed_epoch": ["epoch_id", "prev_epoch_id", "token"],
    "reanchor": ["epoch_id", "classification"],
    "checkpoint": ["epoch_id"],
}

CANON_ID = "ctrl_a"

# The staged config a purse fixture's identity is meant to agree with, kept in
# lockstep with ``valid_purse_payload``'s defaults (controller_name / trading_pair)
# so any single-field divergence a test introduces is the ONLY thing under test.
STAGED_CONFIG = {
    "id": CANON_ID,
    "controller_name": "range_inventory_ladder",
    "trading_pair": "XMR-USDT",
}

# purse_ledger.py:192-206 via the shared ledger identity defaults — an absent staged
# field resolves to the engine's config-model default, never "unknowable".
ENGINE_DEFAULT_CONTROLLER_NAME = "range_inventory_ladder"
ENGINE_DEFAULT_TRADING_PAIR = "ETH-USDT"


def classify(payload, *, canonical_controller_id=CANON_ID, staged_config=None):
    """Call the predicate with the agreeing staged config unless one is given, so
    each test varies exactly ONE thing."""
    return classify_purse_envelope(
        payload,
        canonical_controller_id=canonical_controller_id,
        staged_config=STAGED_CONFIG if staged_config is None else staged_config,
    )


def kind_journal(kind, **record_overrides):
    """Return a valid journal whose LAST record is ``kind`` (with overrides), on top
    of an ``opening_epoch`` that opens ``epoch-1``. The per-kind fixtures already
    default their ``epoch_id``/``prev_epoch_id`` to the opener's epoch, so overrides
    (including ``epoch_id``) flow straight through without colliding."""
    opener = opening_epoch_record(seq=1, epoch_id="epoch-1")
    if kind == "opening_epoch":
        recs = [opening_epoch_record(seq=1, **record_overrides)]
    elif kind == "flow":
        recs = [opener, flow_record(seq=2, **record_overrides)]
    elif kind == "fills_rollup":
        recs = [opener, fills_rollup_record(seq=2, **record_overrides)]
    elif kind == "reseed_epoch":
        recs = [opener, reseed_epoch_record(seq=2, **record_overrides)]
    elif kind == "reanchor":
        recs = [opener, reanchor_record(seq=2, **record_overrides)]
    elif kind == "checkpoint":
        recs = [opener, checkpoint_record(seq=2, **record_overrides)]
    else:  # pragma: no cover - guards a typo'd kind in the test itself
        raise AssertionError(f"unknown kind {kind!r} in test helper")
    return valid_purse_payload(CANON_ID, records=recs)


ALL_KINDS = ["opening_epoch", "flow", "fills_rollup", "reseed_epoch", "reanchor", "checkpoint"]


# ===========================================================================
# 1.  The mirror itself — does the API still agree with the engine?
# ===========================================================================

class TestMirrorMatchesEngine:
    """The API's purse constants are a hand-copy of the engine's. Pin the copy —
    the manual-sync obligation (F22) is separate from the ledger's, and these tests
    are what make a broken purse sync loud instead of silent."""

    def test_mirror_matches_engine_version(self):
        assert PURSE_SCHEMA_VERSION == ENGINE_PURSE_SCHEMA_VERSION

    def test_mirror_matches_engine_known_kinds(self):
        assert set(KNOWN_KINDS) == ENGINE_KNOWN_KINDS

    def test_mirror_matches_engine_enums(self):
        assert set(OPENING_BASIS_QUALITIES) == set(ENGINE_OPENING_BASIS_QUALITIES)
        assert set(FLOW_KINDS) == set(ENGINE_FLOW_KINDS)
        assert set(FLOW_CONFIRMATIONS) == set(ENGINE_FLOW_CONFIRMATIONS)
        assert set(REANCHOR_CLASSIFICATIONS) == set(ENGINE_REANCHOR_CLASSIFICATIONS)

    def test_mirror_matches_engine_money_fields(self):
        # Field NAMES and their non-negative/signed FLAG, per kind. A drift here —
        # e.g. flipping fees_quote_cum to signed, or dropping owned_quote — is
        # exactly what lets the API bless a purse the engine quarantines.
        assert PURSE_MONEY_FIELDS == ENGINE_MONEY_FIELDS


# ===========================================================================
# 2.  Golden fixture accepted (the over-strictness guard)
# ===========================================================================

class TestValidJournals:
    def test_golden_single_opening_epoch_accepted(self):
        verdict = classify(valid_purse_payload(CANON_ID))
        assert verdict.is_valid, verdict.reason
        assert verdict.reason == ""

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_each_kind_in_a_valid_journal_is_accepted(self, kind):
        """Every record kind, appended to a valid journal, is accepted. Without this
        over-strictness guard a ``return PurseVerdict(False, ...)`` unconditionally
        would pass every rejection test."""
        verdict = classify(kind_journal(kind))
        assert verdict.is_valid, verdict.reason

    def test_seq_gap_after_first_is_valid(self):
        # Strict increase, NOT contiguity: a gap after a correct first seq loads
        # (purse_ledger.py:573-576).
        recs = [opening_epoch_record(seq=1, epoch_id="epoch-1"),
                opening_epoch_record(seq=9, epoch_id="epoch-2")]
        assert classify(valid_purse_payload(CANON_ID, records=recs)).is_valid

    def test_signed_delta_fields_accept_negative(self):
        # base_delta_cum / quote_delta_cum are SIGNED (:113-114) — a sold-more-than-
        # bought epoch is legal; earned_opening_quote is signed too (:107).
        recs = [opening_epoch_record(seq=1, epoch_id="epoch-1", earned_opening_quote="-25"),
                fills_rollup_record(seq=2, epoch_id="epoch-1",
                                    base_delta_cum="-3", quote_delta_cum="-500")]
        assert classify(valid_purse_payload(CANON_ID, records=recs)).is_valid

    def test_nullable_opening_fields_accept_null(self):
        # predecessor/note are REQUIRED keys whose VALUE may be null (:632-647).
        rec = opening_epoch_record(seq=1, epoch_id="epoch-1", predecessor=None, note=None)
        assert classify(valid_purse_payload(CANON_ID, records=[rec])).is_valid


# ===========================================================================
# 3.  Shape, version, top-level sequence
# ===========================================================================

class TestShapeAndVersion:
    @pytest.mark.parametrize(
        "payload",
        [pytest.param([], id="list"), pytest.param(None, id="null"),
         pytest.param("s", id="string"), pytest.param(1, id="int"),
         pytest.param(True, id="bool")],
    )
    def test_non_object_payload_rejected(self, payload):
        assert not classify(payload).is_valid

    @pytest.mark.parametrize("version", [2, 0, -1, "1", None])
    def test_wrong_schema_version_rejected(self, version):
        # The engine compares ``!= PURSE_SCHEMA_VERSION`` (purse_ledger.py:548), so the
        # mirror uses the same ``!=``. ``"1"``/None/2/0/-1 are genuinely != 1 and are
        # refused. (1.0/True are deliberately NOT here: Python's 1.0 == 1 and True == 1,
        # so the engine's own ``!=`` accepts them — a stricter API here would be a
        # false-abort mirror drift, so it must not reject them either.)
        verdict = classify(valid_purse_payload(CANON_ID, purse_schema_version=version))
        assert not verdict.is_valid
        assert "purse_schema_version" in verdict.reason

    def test_records_not_a_list_rejected(self):
        # Build valid, then swap records for a non-list (the fixture derives `sequence`
        # by iterating records, so a non-list must be injected post-build).
        payload = valid_purse_payload(CANON_ID)
        payload["records"] = {"a": 1}
        assert not classify(payload).is_valid

    def test_empty_records_rejected(self):
        # records: [] IS the forbidden start-empty fallback (:614-618).
        verdict = classify(valid_purse_payload(CANON_ID, records=[], sequence=0))
        assert not verdict.is_valid

    @pytest.mark.parametrize("bad_sequence", [0, 2, 99, "1", 1.0, True, None])
    def test_top_level_sequence_must_equal_highest_seq(self, bad_sequence):
        # The single record's seq is 1, so sequence must be 1 (:608-613). This is a
        # VALUE relationship P1's minimal check never enforced.
        verdict = classify(valid_purse_payload(CANON_ID, sequence=bad_sequence))
        assert not verdict.is_valid
        assert "sequence" in verdict.reason

    def test_must_begin_with_opening_epoch(self):
        # A reseed_epoch as the FIRST record is individually valid (it opens its own
        # epoch), so this isolates the begin-with-opening_epoch rule (:614-618).
        recs = [reseed_epoch_record(seq=1, epoch_id="epoch-1", prev_epoch_id=None)]
        verdict = classify(valid_purse_payload(CANON_ID, records=recs))
        assert not verdict.is_valid
        assert "opening_epoch" in verdict.reason


# ===========================================================================
# 4.  Identity — controller_id (exact), controller_name / trading_pair (resolved)
# ===========================================================================

class TestIdentity:
    def test_foreign_controller_id_rejected(self):
        verdict = classify(valid_purse_payload("somebody_else"))
        assert not verdict.is_valid
        assert "controller_id" in verdict.reason

    @pytest.mark.parametrize("padded", [" ctrl_a ", "ctrl_a ", "\tctrl_a"])
    def test_padded_controller_id_is_a_mismatch(self, padded):
        # The journal's id is compared VERBATIM (never stripped), mirroring the
        # engine's exact != (:553). Stripping would bless a journal the engine refuses.
        verdict = classify(valid_purse_payload(padded))
        assert not verdict.is_valid
        assert "controller_id" in verdict.reason

    def test_wrong_controller_name_rejected(self):
        verdict = classify(valid_purse_payload(CANON_ID, controller_name="not_the_ladder"))
        assert not verdict.is_valid
        assert "controller_name" in verdict.reason

    def test_wrong_trading_pair_rejected(self):
        verdict = classify(valid_purse_payload(CANON_ID, trading_pair="BTC-USDT"))
        assert not verdict.is_valid
        assert "trading_pair" in verdict.reason

    @pytest.mark.parametrize(
        "field,default,disagreeing",
        [
            ("controller_name", ENGINE_DEFAULT_CONTROLLER_NAME, "not_the_ladder"),
            ("trading_pair", ENGINE_DEFAULT_TRADING_PAIR, "XMR-USDT"),
        ],
    )
    def test_absent_staged_field_compared_against_engine_default(self, field, default, disagreeing):
        """An absent staged field is NOT permission to skip — the engine resolves its
        model default and compares the journal against THAT. Both directions asserted
        so neither 'always refuse when absent' nor 'always skip' passes.

        trading_pair is the sharp case: the engine's default is ETH-USDT, so a purse
        carrying XMR-USDT against a staged config that OMITS trading_pair is a
        guaranteed engine-side mismatch — the fixture's own default disagrees."""
        staged = {k: v for k, v in STAGED_CONFIG.items() if k != field}
        assert disagreeing != default  # the mismatch must be observable

        bad = classify(valid_purse_payload(CANON_ID, **{field: disagreeing}), staged_config=staged)
        assert not bad.is_valid, f"absent staged {field} must be compared against {default!r}"
        assert field in bad.reason

        good = classify(valid_purse_payload(CANON_ID, **{field: default}), staged_config=staged)
        assert good.is_valid, good.reason

    @pytest.mark.parametrize("bad_value", [None, "", "   ", 123, ["x"]])
    def test_unusable_staged_identity_refused(self, bad_value):
        # A staged controller_name the engine's str-typed field could never resolve
        # is uncertainty -> refuse, never skip.
        staged = dict(STAGED_CONFIG, controller_name=bad_value)
        verdict = classify(valid_purse_payload(CANON_ID), staged_config=staged)
        assert not verdict.is_valid
        assert "controller_name" in verdict.reason

    def test_non_dict_staged_config_refused(self):
        verdict = classify(valid_purse_payload(CANON_ID), staged_config=["not", "a", "dict"])
        assert not verdict.is_valid


# ===========================================================================
# 5.  Per-record seq / ts / kind
# ===========================================================================

class TestRecordSeqTsKind:
    def test_first_seq_must_be_one(self):
        recs = [opening_epoch_record(seq=2, epoch_id="epoch-1")]
        assert not classify(valid_purse_payload(CANON_ID, records=recs)).is_valid

    @pytest.mark.parametrize(
        "seqs",
        [
            pytest.param([1, 1], id="equal"),
            pytest.param([2, 1], id="first_not_one"),
            # A TRUE regression that isolates the strictly-increasing guard
            # (:433 ``seq <= prev_seq``): a VALID first record (seq 1) followed by a
            # 3->2 drop, with the top-level ``sequence`` still the highest (4). The
            # ``equal``/``first_not_one`` fixtures trip the equality and 1-based
            # guards respectively, so under a ``<=``->``==`` mutation they still pass;
            # ONLY this case exercises the strict-`<` guard, so it alone fails the
            # mutation — the reason it was added (seq-regression audit finding).
            pytest.param([1, 3, 2, 4], id="regression_after_valid_first"),
        ],
    )
    def test_non_increasing_seq_rejected(self, seqs):
        # Distinct epoch_ids per record so multi-record fixtures never trip the
        # "epoch opened twice" guard — the seq order is the only thing under test.
        recs = [
            opening_epoch_record(seq=s, epoch_id=f"epoch-{i}")
            for i, s in enumerate(seqs)
        ]
        assert not classify(valid_purse_payload(CANON_ID, records=recs)).is_valid

    @pytest.mark.parametrize("seq", [True, 1.0, "1", None, [1]])
    def test_non_int_seq_rejected(self, seq):
        recs = [opening_epoch_record(seq=seq, epoch_id="epoch-1")]
        assert not classify(valid_purse_payload(CANON_ID, records=recs, sequence=1)).is_valid

    def test_record_not_a_dict_rejected(self):
        # Injected post-build for the same reason as the non-list records case.
        payload = valid_purse_payload(CANON_ID)
        payload["records"] = ["not a record"]
        assert not classify(payload).is_valid

    @pytest.mark.parametrize(
        "ts",
        [
            None, "1700000000", float("nan"), float("inf"), -1, True,
            # An overflow-sized integer ts: parseable JSON (arbitrary-precision int),
            # but float() raises OverflowError. The classifier must convert it to a
            # verdict, never raise — without the _require_ts try/except this case
            # would ERROR here rather than assert-fail (overflow-ts audit finding).
            pytest.param(10 ** 400, id="overflow_int"),
        ],
    )
    def test_bad_ts_rejected(self, ts):
        rec = opening_epoch_record(seq=1, epoch_id="epoch-1", ts=ts)
        assert not classify(valid_purse_payload(CANON_ID, records=[rec])).is_valid

    @pytest.mark.parametrize("kind", ["totally_unknown", "", 42, ["opening_epoch"], None])
    def test_unknown_or_non_str_kind_rejected(self, kind):
        # A non-str kind is rejected without a frozenset TypeError (the API's
        # deliberate stricter-safe guard over the engine's bare membership).
        rec = dict(opening_epoch_record(seq=1, epoch_id="epoch-1"), kind=kind)
        assert not classify(valid_purse_payload(CANON_ID, records=[rec])).is_valid


# ===========================================================================
# 6.  Per-kind required fields — drop each one, expect rejection
# ===========================================================================

class TestPerKindRequiredFields:
    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_each_money_field_is_required(self, kind):
        for field in ENGINE_MONEY_FIELDS[kind]:
            payload = kind_journal(kind)
            payload["records"][-1].pop(field)
            verdict = classify(payload)
            assert not verdict.is_valid, f"{kind}.{field} missing must reject"

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_each_non_money_field_is_required(self, kind):
        for field in ENGINE_NON_MONEY_REQUIRED[kind]:
            payload = kind_journal(kind)
            payload["records"][-1].pop(field)
            verdict = classify(payload)
            assert not verdict.is_valid, f"{kind}.{field} missing must reject"

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_non_finite_money_field_rejected(self, kind):
        # A NaN in any money field must reject (JSON parses bare NaN by default).
        field = next(iter(ENGINE_MONEY_FIELDS[kind]))
        payload = kind_journal(kind, **{field: float("nan")})
        assert not classify(payload).is_valid

    def test_non_negative_money_field_rejects_negative(self):
        # owned_quote is pinned non-negative (:101).
        assert not classify(kind_journal("opening_epoch", owned_quote="-1")).is_valid
        # fees_quote_cum is pinned non-negative (:114).
        assert not classify(kind_journal("fills_rollup", fees_quote_cum="-1")).is_valid


# ===========================================================================
# 7.  Enums and typed non-money fields
# ===========================================================================

class TestEnumsAndTypedFields:
    @pytest.mark.parametrize("value", list(OPENING_BASIS_QUALITIES))
    def test_opening_basis_quality_allowed_values(self, value):
        assert classify(kind_journal("opening_epoch", opening_basis_quality=value)).is_valid

    def test_opening_basis_quality_rejects_unknown(self):
        assert not classify(kind_journal("opening_epoch", opening_basis_quality="made_up")).is_valid

    @pytest.mark.parametrize("value", list(FLOW_KINDS))
    def test_flow_kind_allowed_values(self, value):
        assert classify(kind_journal("flow", flow_kind=value)).is_valid

    def test_flow_kind_rejects_unknown(self):
        assert not classify(kind_journal("flow", flow_kind="transfer")).is_valid

    def test_flow_confirmation_rejects_unknown(self):
        assert not classify(kind_journal("flow", confirmation="probably")).is_valid

    def test_reanchor_classification_rejects_unknown(self):
        assert not classify(kind_journal("reanchor", classification="mystery")).is_valid

    @pytest.mark.parametrize("bad", [None, "", 1, ["USDT"]])
    def test_flow_asset_must_be_non_empty_string(self, bad):
        assert not classify(kind_journal("flow", asset=bad)).is_valid

    @pytest.mark.parametrize("bad", [True, -1, 1.5, "3", None])
    def test_fills_seen_must_be_non_negative_int(self, bad):
        assert not classify(kind_journal("fills_rollup", fills_seen=bad)).is_valid

    def test_opening_predecessor_wrong_type_rejected(self):
        # null is allowed, a non-string non-null is not (:640-644).
        assert not classify(kind_journal("opening_epoch", predecessor=123)).is_valid

    def test_reseed_prev_epoch_id_null_allowed(self):
        # prev_epoch_id may be null (the key must exist) — a genuine value need not
        # reference an opened epoch (it is provenance, not a live reference).
        assert classify(kind_journal("reseed_epoch", prev_epoch_id=None)).is_valid


# ===========================================================================
# 8.  Epoch references and per-epoch uniqueness
# ===========================================================================

class TestEpochReferences:
    @pytest.mark.parametrize("kind", ["fills_rollup", "reanchor", "checkpoint"])
    def test_reference_to_unopened_epoch_rejected(self, kind):
        # No opening_epoch/reseed_epoch ever opened "epoch-404".
        verdict = classify(kind_journal(kind, epoch_id="epoch-404"))
        assert not verdict.is_valid
        assert "epoch-404" in verdict.reason

    def test_rollup_after_reseed_references_the_new_epoch(self):
        # reseed_epoch opens epoch-2; a later rollup on epoch-2 is valid.
        recs = [
            opening_epoch_record(seq=1, epoch_id="epoch-1"),
            reseed_epoch_record(seq=2, epoch_id="epoch-2", prev_epoch_id="epoch-1"),
            fills_rollup_record(seq=3, epoch_id="epoch-2"),
        ]
        assert classify(valid_purse_payload(CANON_ID, records=recs)).is_valid

    def test_rollup_before_its_opening_is_rejected(self):
        # Ordering matters: opened_epochs accumulates as records are walked, so a
        # rollup that precedes the epoch's opener references an unopened epoch.
        recs = [
            opening_epoch_record(seq=1, epoch_id="epoch-1"),
            fills_rollup_record(seq=2, epoch_id="epoch-2"),
            reseed_epoch_record(seq=3, epoch_id="epoch-2", prev_epoch_id="epoch-1"),
        ]
        assert not classify(valid_purse_payload(CANON_ID, records=recs)).is_valid

    def test_two_rollups_one_epoch_rejected(self):
        recs = [
            opening_epoch_record(seq=1, epoch_id="epoch-1"),
            fills_rollup_record(seq=2, epoch_id="epoch-1"),
            fills_rollup_record(seq=3, epoch_id="epoch-1"),
        ]
        verdict = classify(valid_purse_payload(CANON_ID, records=recs))
        assert not verdict.is_valid
        assert "fills_rollup" in verdict.reason

    def test_two_rollups_distinct_epochs_valid(self):
        recs = [
            opening_epoch_record(seq=1, epoch_id="epoch-1"),
            reseed_epoch_record(seq=2, epoch_id="epoch-2", prev_epoch_id="epoch-1"),
            fills_rollup_record(seq=3, epoch_id="epoch-1"),
            fills_rollup_record(seq=4, epoch_id="epoch-2"),
        ]
        assert classify(valid_purse_payload(CANON_ID, records=recs)).is_valid

    def test_epoch_opened_twice_rejected(self):
        recs = [
            opening_epoch_record(seq=1, epoch_id="epoch-1"),
            opening_epoch_record(seq=2, epoch_id="epoch-1"),
        ]
        verdict = classify(valid_purse_payload(CANON_ID, records=recs))
        assert not verdict.is_valid
        assert "twice" in verdict.reason

    def test_two_distinct_opening_epochs_valid(self):
        # A post-quarantine re-init appends a second opening_epoch — legal so long as
        # the epoch ids differ (inception continuity, :443-445).
        recs = [
            opening_epoch_record(seq=1, epoch_id="epoch-1"),
            opening_epoch_record(seq=2, epoch_id="epoch-2"),
        ]
        assert classify(valid_purse_payload(CANON_ID, records=recs)).is_valid
