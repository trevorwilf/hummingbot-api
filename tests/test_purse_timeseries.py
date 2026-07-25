"""hbdash_api Phase 2 — GET /purse/{id}/timeseries (journal-time checkpoint series).

Covers the DERIVED, NON-AUTHORITATIVE equity/flows timeseries (CLA-2A-05, CLA-M02;
safety invariants 1 & 6):

  * ``services.purse_read_model.compute_timeseries`` — the seq-ordered checkpoint/
    epoch/reanchor series, single-sourced with the SAME record walk + ``_dec`` +
    accumulator logic as ``compute_derived_metrics``. Every EXPECTED value here is
    HAND-COMPUTED from the pinned contract formulas (independent ``Decimal``
    arithmetic), NEVER captured by running the implementation — so a dropped
    accumulator term, a boundary that vanishes, an x-axis on harvest time, or a
    per-point reference price collapsed to the final one makes the arithmetic
    disagree and the test fail.
  * The x-axis is JOURNAL time (each point's record ``ts``), the CLA-M02 fix — never
    the DB ``harvested_at``. The series RECONCILES with ``compute_derived_metrics`` at
    its final point (the two computations agree by construction).
  * Incarnation segmentation — one stable ``incarnation_id`` on every point of a
    journal (P1's derivation, shared); a reseed re-baselines (a ``boundary`` marker)
    but does NOT open a new incarnation; a separate fresh seed does.
  * The endpoint — ``GET /purse/{id}/timeseries`` returns points + provenance + a
    non-live ``note``, 404s an unknown controller, is behind auth, and never leaks
    ``records_json`` — exercised both as a direct handler and THROUGH the real
    ``main.app`` ASGI stack. Money is decimal STRINGS end-to-end (a float would lose
    precision).

The real-sqlite DB plumbing and the ASGI harness are REUSED from
``test_purse_readmodel`` (the proven Phase-3 fixtures), so the timeseries route runs
against real repositories and the real app.
"""
import hashlib
import json
from decimal import Decimal

import pytest
from fastapi import HTTPException

from database.repositories.purse_snapshot_repository import PurseSnapshotRepository
from ledger_fixtures import (
    checkpoint_record,
    fills_rollup_record,
    flow_record,
    opening_epoch_record,
    reanchor_record,
    reseed_epoch_record,
    valid_purse_payload,
)
from models.purse import AUTHORITY_NOTE, TIMESERIES_NOTE, PurseTimeseriesResponse
from routers.purse import get_purse_timeseries
from services.purse_read_model import compute_derived_metrics, compute_timeseries

# The proven real-DB + ASGI harness (fixtures resolved by name from this namespace).
from test_purse_readmodel import _asgi_get, asgi_main, db  # noqa: F401


# ---------------------------------------------------------------------------
# THE SPEC — hand-built journals with a KNOWN sequence of opening + checkpoints +
# reseed/reanchor, so every point's ts / owned / ref / equity / contributed is
# derivable from the contract, never captured from the code.
# ---------------------------------------------------------------------------

T0 = 1700000000.0        # inception ts (journal time)
WEEK = 604800            # seconds in a calendar week


def multi_epoch_series_journal(controller_id="ctrl_a", *, inception_epoch="epoch-alpha"):
    """opening(inception) → rollup(-30q,+0.2b) → checkpoint(ref 150) → reseed(epoch-beta,
    owned→100) → rollup(+10q) → checkpoint(ref 160). Emits at opening/checkpoint/reseed/
    checkpoint = 4 points (rollups do NOT emit). The two checkpoints carry DIFFERENT
    reference prices (150 then 160) so a per-point ref is distinguishable from the final
    one. All money below is transcribed from the contract, not the code."""
    records = [
        opening_epoch_record(
            seq=1, ts=T0, epoch_id=inception_epoch,
            owned_quote="100", owned_base="0", reference_price="150",
            contributed_opening_quote="100", earned_opening_quote="0",
        ),
        fills_rollup_record(
            seq=2, ts=T0 + 100, epoch_id=inception_epoch,
            quote_delta_cum="-30", base_delta_cum="0.2", fees_quote_cum="0",
            last_update_ts=T0 + 100,
        ),
        checkpoint_record(
            seq=3, ts=T0 + 1 * WEEK, epoch_id=inception_epoch,
            owned_quote="70", owned_base="0.2", reference_price="150", equity_quote="100",
        ),
        reseed_epoch_record(
            seq=4, ts=T0 + 1 * WEEK + 100, epoch_id="epoch-beta", prev_epoch_id=inception_epoch,
            old_owned_quote="70", old_owned_base="0.2",
            new_owned_quote="100", new_owned_base="0", reference_price="150",
        ),
        fills_rollup_record(
            seq=5, ts=T0 + 1 * WEEK + 200, epoch_id="epoch-beta",
            quote_delta_cum="10", base_delta_cum="0", fees_quote_cum="0",
            last_update_ts=T0 + 1 * WEEK + 200,
        ),
        checkpoint_record(
            seq=6, ts=T0 + 2 * WEEK, epoch_id="epoch-beta",
            owned_quote="110", owned_base="0", reference_price="160", equity_quote="110",
        ),
    ]
    return valid_purse_payload(controller_id=controller_id, records=records)


# The hand-computed expected points for multi_epoch_series_journal (each a dict of the
# fields the test pins). Reference price is the CURRENT one at that point; earned_realized
# = earned_opening + Σquote_delta + Σbase_delta*ref; earned_total = equity - contributed +
# withdrawn. Derived here by hand, NOT by running compute_timeseries.
EXPECTED_MULTI_EPOCH_POINTS = [
    # seq1 opening: owned (100,0) @150 → equity 100; earned_realized 0; earned_total 0.
    dict(seq=1, ts=T0, epoch_id="epoch-alpha", boundary="opening",
         owned_quote="100", owned_base="0", reference_price="150",
         equity_quote="100", contributed="100", withdrawn="0",
         earned_total="0", earned_realized="0"),
    # seq3 checkpoint @150: owned (70,0.2) → equity 70+30=100; realized -30+0.2*150=0.
    dict(seq=3, ts=T0 + 1 * WEEK, epoch_id="epoch-alpha", boundary=None,
         owned_quote="70", owned_base="0.2", reference_price="150",
         equity_quote="100", contributed="100", withdrawn="0",
         earned_total="0", earned_realized="0"),
    # seq4 reseed: owned re-anchored to (100,0) @150 → equity 100; deltas unchanged.
    dict(seq=4, ts=T0 + 1 * WEEK + 100, epoch_id="epoch-beta", boundary="reseed",
         owned_quote="100", owned_base="0", reference_price="150",
         equity_quote="100", contributed="100", withdrawn="0",
         earned_total="0", earned_realized="0"),
    # seq6 checkpoint @160: owned (110,0) → equity 110; Σq=-20, Σb=0.2 →
    # realized -20+0.2*160=12; earned_total 110-100=10.
    dict(seq=6, ts=T0 + 2 * WEEK, epoch_id="epoch-beta", boundary=None,
         owned_quote="110", owned_base="0", reference_price="160",
         equity_quote="110", contributed="100", withdrawn="0",
         earned_total="10", earned_realized="12"),
]


def flows_journal(controller_id="ctrl_a"):
    """opening(contributed 100) → deposit 50 → withdrawal 20 → checkpoint. Only two points
    (flows do NOT emit), but the checkpoint point's running contributed/withdrawn MUST
    reflect the flows: contributed 100+50=150, withdrawn 20."""
    records = [
        opening_epoch_record(
            seq=1, ts=T0, epoch_id="epoch-alpha",
            owned_quote="100", owned_base="0", reference_price="150",
            contributed_opening_quote="100", earned_opening_quote="0",
        ),
        flow_record(seq=2, ts=T0 + 100, flow_kind="deposit", quote_valuation="50"),
        flow_record(seq=3, ts=T0 + 200, flow_kind="withdrawal", quote_valuation="20"),
        checkpoint_record(
            seq=4, ts=T0 + 300, epoch_id="epoch-alpha",
            owned_quote="130", owned_base="0", reference_price="150", equity_quote="130",
        ),
    ]
    return valid_purse_payload(controller_id=controller_id, records=records)


def outflow_reanchor_journal(controller_id="ctrl_a"):
    """opening(owned 200) → undeclared_outflow reanchor(owned→120) → checkpoint. The
    reanchor is a committed owned CUT, so its point shows owned re-anchored to 120 and a
    ``reanchor`` boundary — the reset a chart must mark, not draw as a continuous line."""
    records = [
        opening_epoch_record(
            seq=1, ts=T0, epoch_id="epoch-alpha",
            owned_quote="200", owned_base="0", reference_price="150",
            contributed_opening_quote="200", earned_opening_quote="0",
        ),
        reanchor_record(
            seq=2, ts=T0 + 100, epoch_id="epoch-alpha",
            old_owned_quote="200", old_owned_base="0",
            new_owned_quote="120", new_owned_base="0", overclaim_quote="80",
            classification="undeclared_outflow", wallet_quote_total="120", wallet_base_total="0",
        ),
        checkpoint_record(
            seq=3, ts=T0 + 1 * WEEK, epoch_id="epoch-alpha",
            owned_quote="120", owned_base="0", reference_price="150", equity_quote="120",
        ),
    ]
    return valid_purse_payload(controller_id=controller_id, records=records)


def drift_reanchor_journal(controller_id="ctrl_a"):
    """opening(owned 200) → DRIFT reanchor(new_owned=0 — a magnitude, not a cut) →
    checkpoint(owned 200). A ``drift`` reanchor is the indistinguishable observation case,
    so owned is left UNCHANGED at 200 (the SAME safe default resolve_current_state applies,
    CDX-R03), NOT overwritten to the record's new_owned_*=0."""
    records = [
        opening_epoch_record(
            seq=1, ts=T0, epoch_id="epoch-alpha",
            owned_quote="200", owned_base="0", reference_price="150",
            contributed_opening_quote="200", earned_opening_quote="0",
        ),
        reanchor_record(
            seq=2, ts=T0 + 100, epoch_id="epoch-alpha",
            old_owned_quote="30", old_owned_base="0",
            new_owned_quote="0", new_owned_base="0", overclaim_quote="30",
            classification="drift", wallet_quote_total="0", wallet_base_total="0",
        ),
        checkpoint_record(
            seq=3, ts=T0 + 1 * WEEK, epoch_id="epoch-alpha",
            owned_quote="200", owned_base="0", reference_price="150", equity_quote="200",
        ),
    ]
    return valid_purse_payload(controller_id=controller_id, records=records)


def precision_journal(controller_id="ctrl_a"):
    """A journal whose money would LOSE precision as a float: contributed 0.1 + 0.2 = 0.3
    exactly as Decimal, but 0.30000000000000004 as a float. The response strings must be
    the exact Decimal representation."""
    records = [
        opening_epoch_record(
            seq=1, ts=T0, epoch_id="epoch-p",
            owned_quote="0.1", owned_base="0", reference_price="1",
            contributed_opening_quote="0.1", earned_opening_quote="0",
        ),
        flow_record(seq=2, ts=T0 + 100, flow_kind="deposit", quote_valuation="0.2"),
        checkpoint_record(
            seq=3, ts=T0 + 200, epoch_id="epoch-p",
            owned_quote="0.3", owned_base="0", reference_price="1", equity_quote="0.3",
        ),
    ]
    return valid_purse_payload(controller_id=controller_id, records=records)


def _spec_incarnation_id(controller_id, epoch_id):
    """Reimplement the DOCUMENTED P1 derivation independently (spec-derived, NOT captured
    from the implementation): ``inc_`` + first 16 hex of sha256 of the tagged basis."""
    return "inc_" + hashlib.sha256(
        f"epoch\x00{controller_id}\x00{epoch_id}".encode("utf-8")
    ).hexdigest()[:16]


async def insert_journal(db, payload, *, source_instance_name="inst1", source_bot_run_id=None):
    """Insert a snapshot whose ``records_json`` is a REAL journal (the timeseries route
    re-parses + re-validates it). ``derived_*`` are placeholders — the route recomputes
    from ``records_json`` and never reads them."""
    body = json.dumps(payload)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    async with db.get_session_context() as session:
        return await PurseSnapshotRepository(session).insert_snapshot_if_absent(
            controller_id=payload["controller_id"],
            source_instance_name=source_instance_name,
            source_bot_run_id=source_bot_run_id,
            purse_sha256=sha,
            sequence=payload["sequence"],
            records_json=body,
            derived_contributed="0", derived_withdrawn="0", derived_earned_realized="0",
            derived_earned_total="0", derived_unrealized="0", derived_drift="0",
            reference_price_used="0", opening_basis_quality=None,
        )


# ===========================================================================
# 1. compute_timeseries — point emission, hand-computed journal-time values
# ===========================================================================

class TestTimeseriesComputation:
    def test_emits_at_checkpoint_opening_reseed_only_with_hand_computed_values(self):
        """The point set + every field matches the hand-derived EXPECTED_MULTI_EPOCH_POINTS.
        Emission is keyed on record KIND: opening/checkpoint/reseed emit, the two rollups do
        NOT. MUTATION GUARDS: emitting at rollups → 6 points ≠ 4; skipping the reseed point →
        3 points; a running-accumulator term dropped → an equity/earned mismatch below."""
        points = compute_timeseries(multi_epoch_series_journal())
        assert len(points) == 4      # opening + checkpoint + reseed + checkpoint (rollups skipped)
        for point, exp in zip(points, EXPECTED_MULTI_EPOCH_POINTS):
            assert point.seq == exp["seq"]
            assert point.ts == exp["ts"]
            assert point.epoch_id == exp["epoch_id"]
            assert point.boundary == exp["boundary"]
            assert point.owned_quote == Decimal(exp["owned_quote"])
            assert point.owned_base == Decimal(exp["owned_base"])
            assert point.reference_price == Decimal(exp["reference_price"])
            assert point.equity_quote == Decimal(exp["equity_quote"])
            assert point.contributed == Decimal(exp["contributed"])
            assert point.withdrawn == Decimal(exp["withdrawn"])
            assert point.earned_total == Decimal(exp["earned_total"])
            assert point.earned_realized == Decimal(exp["earned_realized"])

    def test_x_axis_is_journal_record_ts_not_harvest_time(self):
        """CLA-M02: every point's ``ts`` is the RECORD's journal ts, not a DB harvest time.
        The values are the fixed epoch seconds T0.. no ``harvested_at`` (a datetime.now())
        could equal. MUTATION GUARD: emitting harvested_at as the point ts would replace
        these with wall-clock-derived values and fail this exact-equality check."""
        points = compute_timeseries(multi_epoch_series_journal())
        assert [p.ts for p in points] == [T0, T0 + 1 * WEEK, T0 + 1 * WEEK + 100, T0 + 2 * WEEK]

    def test_points_are_in_strictly_increasing_seq_order(self):
        """The series is emitted in journal seq order. MUTATION GUARD: reordering (e.g. by
        harvested_at or reversed) breaks the strictly-increasing invariant."""
        seqs = [p.seq for p in compute_timeseries(multi_epoch_series_journal())]
        assert seqs == [1, 3, 4, 6]
        assert all(a < b for a, b in zip(seqs, seqs[1:]))

    def test_intermediate_point_uses_its_own_reference_price_not_the_final_one(self):
        """The seq-3 checkpoint carries ref 150 while the FINAL checkpoint carries 160. A
        chart must mark equity as-of that point (owned*own-ref), not retro-applied. MUTATION
        GUARD: applying the final ref (160) to the intermediate point gives 70+0.2*160=102 ≠
        the hand-computed 100."""
        points = compute_timeseries(multi_epoch_series_journal())
        mid = points[1]                                     # the seq-3 checkpoint
        assert mid.reference_price == Decimal("150")
        assert mid.equity_quote == Decimal("100")
        assert mid.equity_quote != Decimal("102")           # not the final-ref value

    def test_running_contributed_and_withdrawn_track_the_flows(self):
        """A deposit and a withdrawal move the RUNNING contributed/withdrawn by the checkpoint
        that follows them: contributed 100+50=150, withdrawn 20. MUTATION GUARD: skipping the
        flow accumulation leaves contributed 100 / withdrawn 0 — both assertions fail."""
        points = compute_timeseries(flows_journal())
        assert len(points) == 2                             # opening + checkpoint (flows skipped)
        final = points[-1]
        assert final.contributed == Decimal("150")
        assert final.withdrawn == Decimal("20")
        assert final.earned_total == Decimal("0")           # 130 - 150 + 20

    def test_outflow_reanchor_emits_a_boundary_point_with_the_cut_owned(self):
        """An undeclared_outflow reanchor is a committed cut: its point carries
        boundary='reanchor' and owned re-anchored to 120 (from 200). MUTATION GUARDS: not
        emitting at reanchor → 2 points ≠ 3; dropping the boundary → None ≠ 'reanchor'."""
        points = compute_timeseries(outflow_reanchor_journal())
        assert len(points) == 3                             # opening + reanchor + checkpoint
        seqs = [p.seq for p in points]
        assert seqs == [1, 2, 3]
        boundaries = [p.boundary for p in points]
        assert boundaries == ["opening", "reanchor", None]
        reanchor_point = points[1]
        assert reanchor_point.owned_quote == Decimal("120")  # the committed cut, from 200
        assert reanchor_point.equity_quote == Decimal("120")
        assert reanchor_point.earned_total == Decimal("-80")  # 120 - 200 + 0

    def test_drift_reanchor_leaves_owned_unchanged(self):
        """A DRIFT reanchor is observation-only (new_owned_*=0 is a surfaced magnitude, not a
        cut): owned stays 200, mirroring resolve_current_state's safe default (CDX-R03).
        MUTATION GUARD: overwriting owned from a drift reanchor's new_owned_* would zero the
        equity here (200 → 0)."""
        points = compute_timeseries(drift_reanchor_journal())
        drift_point = points[1]                             # the drift reanchor
        assert drift_point.boundary == "reanchor"
        assert drift_point.owned_quote == Decimal("200")    # UNCHANGED, not the record's 0
        assert drift_point.equity_quote == Decimal("200")


# ===========================================================================
# 2. Reconciliation — the final point agrees with compute_derived_metrics
# ===========================================================================

class TestReconciliation:
    @pytest.mark.parametrize(
        "journal", [multi_epoch_series_journal, flows_journal, outflow_reanchor_journal]
    )
    def test_final_point_equals_compute_derived_metrics(self, journal):
        """Single-sourcing proof: the LAST timeseries point equals the whole-journal
        ``compute_derived_metrics`` for contributed / withdrawn / earned_realized /
        earned_total / equity / owned / reference price. Any drift between the two walks
        (a different accumulator, a different owned/ref resolution) fails this. MUTATION
        GUARD: skipping the running-contributed update, or using a different final ref,
        makes the two disagree."""
        payload = journal()
        final = compute_timeseries(payload)[-1]
        m = compute_derived_metrics(payload)
        assert final.contributed == m.contributed
        assert final.withdrawn == m.withdrawn
        assert final.earned_realized == m.earned_realized
        assert final.earned_total == m.earned_total
        assert final.equity_quote == m.equity_quote
        assert final.owned_quote == m.owned_quote
        assert final.owned_base == m.owned_base
        assert final.reference_price == m.reference_price_used


# ===========================================================================
# 3. Incarnation segmentation — one stable id per journal (P1's derivation)
# ===========================================================================

class TestIncarnationSegmentation:
    def test_one_stable_incarnation_id_on_every_point_matching_the_spec(self):
        """Every point of a journal carries the SAME incarnation_id, derived from the first
        opening_epoch (P1). A reseed re-baselines but does NOT open a new incarnation, so the
        reseed point shares the id. MUTATION GUARD: deriving the id per-epoch (from the reseed
        epoch) would make the reseed point differ from the opening point."""
        points = compute_timeseries(multi_epoch_series_journal(controller_id="ctrl_a"))
        expected = _spec_incarnation_id("ctrl_a", "epoch-alpha")
        assert {p.incarnation_id for p in points} == {expected}
        reseed_point = next(p for p in points if p.boundary == "reseed")
        opening_point = next(p for p in points if p.boundary == "opening")
        assert reseed_point.incarnation_id == opening_point.incarnation_id

    def test_a_fresh_seed_yields_a_different_incarnation_id(self):
        """A separate no-resume journal (a NEW inception epoch) is a distinct incarnation, so
        its points never share the id — the CDX-005/CLA-309 segmentation hook. MUTATION GUARD:
        an id independent of the inception epoch would collide across the two journals."""
        a = compute_timeseries(multi_epoch_series_journal(inception_epoch="epoch-alpha"))
        b = compute_timeseries(multi_epoch_series_journal(inception_epoch="epoch-GAMMA"))
        assert a[0].incarnation_id != b[0].incarnation_id
        assert a[0].incarnation_id == _spec_incarnation_id("ctrl_a", "epoch-alpha")
        assert b[0].incarnation_id == _spec_incarnation_id("ctrl_a", "epoch-GAMMA")

    def test_incarnation_id_ignores_source_bot_run_id(self):
        """A copy-forward RESUME keeps the inception epoch but is a NEW bot_run: the
        incarnation must NOT split. MUTATION GUARD: folding source_bot_run_id (or any
        harvest-time value) into the id would make these three differ."""
        payload = multi_epoch_series_journal()
        id1 = compute_timeseries(payload, source_bot_run_id=1)[0].incarnation_id
        id2 = compute_timeseries(payload, source_bot_run_id=2)[0].incarnation_id
        id_none = compute_timeseries(payload, source_bot_run_id=None)[0].incarnation_id
        assert id1 == id2 == id_none


# ===========================================================================
# 4. Response contract — non-live labeling, decimal strings (safety invariants)
# ===========================================================================

class TestResponseContract:
    def test_note_disclaims_live_freshness_and_names_journal_time(self):
        """The response MUST NOT imply a live tail and MUST name the journal-time x-axis and
        the segmentation keys (CLA-2A-05 / CLA-M02)."""
        assert "note" in PurseTimeseriesResponse.model_fields
        assert "not a live tail" in TIMESERIES_NOTE
        assert "journal time" in TIMESERIES_NOTE
        assert "incarnation_id" in TIMESERIES_NOTE and "boundary" in TIMESERIES_NOTE

    def test_point_money_fields_are_strings_not_floats(self):
        """Money on every point is a decimal STRING (JSON float coercion must never corrupt a
        balance). The model types these ``str``; a value that a float would mangle (0.1+0.2)
        stays exact. MUTATION GUARD: typing a money field as float serializes 0.3 as
        0.30000000000000004."""
        from models.purse import PurseTimeseriesPoint
        for money_field in ("owned_quote", "owned_base", "reference_price", "equity_quote",
                            "contributed", "withdrawn", "earned_total", "earned_realized"):
            assert PurseTimeseriesPoint.model_fields[money_field].annotation is str


# ===========================================================================
# 5. Endpoint — direct handler + through the real ASGI app
# ===========================================================================

class TestTimeseriesEndpoint:
    @pytest.mark.asyncio
    async def test_returns_points_provenance_and_note(self, db):
        payload = multi_epoch_series_journal(controller_id="ctrl_a")
        await insert_journal(db, payload, source_instance_name="inst1", source_bot_run_id=7)

        resp = await get_purse_timeseries("ctrl_a", db_manager=db)

        assert resp.controller_id == "ctrl_a"
        assert len(resp.points) == 4
        assert [p.seq for p in resp.points] == [1, 3, 4, 6]
        assert [p.boundary for p in resp.points] == ["opening", None, "reseed", None]
        # journal time survives the API boundary (CLA-M02), and money is strings.
        assert [p.ts for p in resp.points] == [T0, T0 + 1 * WEEK, T0 + 1 * WEEK + 100, T0 + 2 * WEEK]
        final = resp.points[-1]
        assert final.equity_quote == "110"
        assert Decimal(final.earned_total) == Decimal("10")
        assert Decimal(final.earned_realized) == Decimal("12")
        assert final.incarnation_id == _spec_incarnation_id("ctrl_a", "epoch-alpha")
        assert resp.provenance.sequence == payload["sequence"] == 6
        assert resp.provenance.source_instance_name == "inst1"
        assert resp.authority_note == AUTHORITY_NOTE
        assert resp.note == TIMESERIES_NOTE

    @pytest.mark.asyncio
    async def test_404_for_unknown_controller(self, db):
        with pytest.raises(HTTPException) as exc:
            await get_purse_timeseries("nobody", db_manager=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_contract_invalid_stored_journal_is_500(self, db):
        """A row indexed under ctrl_a whose stored journal's OWN controller_id is ctrl_b is a
        corrupt/tampered mirror. The route re-validates the stored envelope against the URL
        id and returns 500 — it never silently attributes ctrl_b's series to ctrl_a. MUTATION
        GUARD: skipping the re-validation would 200 with ctrl_b's points under ctrl_a."""
        mismatched = multi_epoch_series_journal(controller_id="ctrl_b")
        body = json.dumps(mismatched)
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        async with db.get_session_context() as session:
            await PurseSnapshotRepository(session).insert_snapshot_if_absent(
                controller_id="ctrl_a", source_instance_name="inst1", source_bot_run_id=None,
                purse_sha256=sha, sequence=mismatched["sequence"], records_json=body,
                derived_contributed="0", derived_withdrawn="0", derived_earned_realized="0",
                derived_earned_total="0", derived_unrealized="0", derived_drift="0",
                reference_price_used="0", opening_basis_quality=None,
            )
        with pytest.raises(HTTPException) as exc:
            await get_purse_timeseries("ctrl_a", db_manager=db)
        assert exc.value.status_code == 500


class TestTimeseriesEndpointASGI:
    @pytest.mark.asyncio
    async def test_registered_authed_200_no_records_leak_journal_time_strings(self, db, asgi_main):
        """A real GET /purse/{id}/timeseries through the app: 200 with journal-time points,
        decimal-string money, the note, provenance — and NO ``records_json`` leak. Fails if
        the route is unregistered or the prefix wrong."""
        from deps import get_database_manager
        payload = multi_epoch_series_journal(controller_id="ctrl_a")
        await insert_journal(db, payload, source_instance_name="inst1", source_bot_run_id=7)
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a/timeseries")
        assert resp.status_code == 200
        body = resp.json()
        assert body["controller_id"] == "ctrl_a"
        assert [p["seq"] for p in body["points"]] == [1, 3, 4, 6]
        assert [p["boundary"] for p in body["points"]] == ["opening", None, "reseed", None]
        # x-axis is journal time through the whole ASGI stack (CLA-M02).
        assert [p["ts"] for p in body["points"]] == [T0, T0 + 1 * WEEK, T0 + 1 * WEEK + 100, T0 + 2 * WEEK]
        final = body["points"][-1]
        assert final["equity_quote"] == "110"
        assert isinstance(final["contributed"], str)         # money is a JSON string, not a float
        assert final["incarnation_id"] == _spec_incarnation_id("ctrl_a", "epoch-alpha")
        assert body["note"] == TIMESERIES_NOTE
        assert body["authority_note"] == AUTHORITY_NOTE
        assert body["provenance"]["sequence"] == 6
        assert "records_json" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_money_strings_keep_exact_precision(self, db, asgi_main):
        """0.1 + 0.2 = 0.3 exactly as decimal strings; a float would serialize
        0.30000000000000004. MUTATION GUARD: a float-typed money field fails this."""
        from deps import get_database_manager
        await insert_journal(db, precision_journal(controller_id="ctrl_a"))
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a/timeseries")
        assert resp.status_code == 200
        final = resp.json()["points"][-1]
        assert final["contributed"] == "0.3"                 # exact, not 0.30000000000000004
        assert final["equity_quote"] == "0.3"

    @pytest.mark.asyncio
    async def test_unauthenticated_request_is_rejected(self, db, asgi_main):
        """The financial read-model MUST be behind auth. DB overridden, auth NOT → the real
        ``Depends(auth_user)`` rejects with 401."""
        from deps import get_database_manager
        await insert_journal(db, multi_epoch_series_journal(controller_id="ctrl_a"))
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a/timeseries")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_controller_404_through_router(self, db, asgi_main):
        from deps import get_database_manager
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/nobody/timeseries")
        assert resp.status_code == 404
