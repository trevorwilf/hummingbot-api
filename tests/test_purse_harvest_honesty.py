"""hbdash_api Phase 3 — harvest honesty: additive epoch/degraded/incarnation fields.

Covers the DERIVED, NON-AUTHORITATIVE harvest-honesty additions (CLA-2A-07/CLA-008,
CLA-007, CDX-009, CDX-005/CLA-309; safety invariants 1, 3 & 5). Every expected value is
SPEC-DERIVED — hand-built journals with a known epoch structure, independent
``hashlib``/``Decimal`` arithmetic — never captured by running the implementation, so a
dropped term makes the arithmetic disagree and the test fail.

  * ``services.purse_read_model.compute_harvest_markers`` — the additive epoch markers
    (``latest_epoch_id`` = the NEWEST epoch opened, NOT the inception epoch;
    ``reanchor_count`` = the number of reanchor records), single-sourced with the record
    walk and never raising.
  * ``services.purse_harvest`` — the observation-only harvest now attaches those markers
    and a best-effort ``source_degraded`` flag from the retirement ``final_status``, and
    MUST still insert the snapshot (never block the retirement — invariant 5) when a new
    field's source is missing or raises.
  * ``BotsOrchestrator.get_latest_controller_performance_result`` + the
    ``controller-performance-latest`` route — CDX-009: a snapshot-store read failure
    surfaces an additive ``degraded: true`` marker instead of an empty success a panel
    cannot tell apart from 'no data'; the healthy success shape stays byte-identical.
  * ``GET /purse/{id}/history`` — CDX-005/CLA-309: rows carry an ``incarnation_id`` and are
    ordered by lineage chronology (``source_bot_run_id``), so a late-archived stale
    instance does NOT masquerade as the newest incarnation.

The real-sqlite DB plumbing, the ASGI harness, the instance-dir scaffolding and the
retirement-FSM harness are REUSED from the proven Phase-3 purse tests rather than
re-mocked, so the harvest runs against real repositories, the real app, and the real
retirement state machine.
"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from database.models import PurseSnapshot
from database.repositories.controller_performance_repository import (
    ControllerPerformanceRepository,
)
from ledger_fixtures import (
    checkpoint_record,
    opening_epoch_record,
    reanchor_record,
    reseed_epoch_record,
    valid_purse_payload,
)
from routers.bot_orchestration import get_latest_controller_performance
from routers.purse import (
    get_purse,
    get_purse_activity,
    get_purse_history,
    get_purse_timeseries,
)
from database.repositories.purse_snapshot_repository import PurseSnapshotRepository
from services.purse_harvest import _extract_source_degraded, harvest_instance_purses
from services.purse_read_model import PurseHarvestMarkers, compute_harvest_markers

# The proven real-DB + ASGI + instance-dir + retirement-FSM harnesses (fixtures resolved
# by name from these namespaces; plain helpers imported directly).
from test_purse_readmodel import (  # noqa: F401
    RmtreeArchiver,
    _asgi_get,
    asgi_main,
    build_instance,
    comprehensive_purse,
    db,
    history,
)
from test_retirement_fsm import (
    StubDockerManager,
    StubMQTT,
    make_orchestrator,
    run_fsm,
    seed_bot_run,
)


T0 = 1700000000.0  # inception ts


# ---------------------------------------------------------------------------
# THE SPEC — hand-built journals whose epoch structure is known by construction.
# ---------------------------------------------------------------------------


def two_reanchor_journal(controller_id="ctrl_a"):
    """opening(epoch-1) → reanchor → reanchor → reseed(epoch-2) → checkpoint(epoch-2).

    The newest epoch OPENED is ``epoch-2`` (the reseed), NOT the inception ``epoch-1``;
    exactly two reanchor records. The reanchors are no-op cuts (owned unchanged) and the
    journal ends on a checkpoint, so the harvest can authoritatively derive owned and
    inserts a row (it is not owned-ambiguous)."""
    records = [
        opening_epoch_record(seq=1, ts=T0, epoch_id="epoch-1"),
        reanchor_record(
            seq=2, ts=T0 + 10, epoch_id="epoch-1",
            old_owned_quote="100", new_owned_quote="100",
            old_owned_base="0", new_owned_base="0", overclaim_quote="0",
            classification="undeclared_outflow", wallet_quote_total="100", wallet_base_total="0",
        ),
        reanchor_record(
            seq=3, ts=T0 + 20, epoch_id="epoch-1",
            old_owned_quote="100", new_owned_quote="100",
            old_owned_base="0", new_owned_base="0", overclaim_quote="0",
            classification="undeclared_outflow", wallet_quote_total="100", wallet_base_total="0",
        ),
        reseed_epoch_record(
            seq=4, ts=T0 + 30, epoch_id="epoch-2", prev_epoch_id="epoch-1",
            new_owned_quote="120", new_owned_base="0",
        ),
        checkpoint_record(
            seq=5, ts=T0 + 40, epoch_id="epoch-2",
            owned_quote="120", owned_base="0", reference_price="150", equity_quote="120",
        ),
    ]
    return valid_purse_payload(controller_id=controller_id, records=records)


def _journal_json(controller_id="ctrl_a", epoch_id="epoch-1"):
    """The minimal parseable journal bytes the history incarnation derivation reads."""
    payload = valid_purse_payload(
        controller_id=controller_id,
        records=[opening_epoch_record(seq=1, ts=T0, epoch_id=epoch_id)],
    )
    return json.dumps(payload)


def _spec_incarnation_id(controller_id, epoch_id):
    """The DOCUMENTED derivation, reimplemented independently (spec-derived): ``inc_`` +
    first 16 hex of sha256 of the tagged ``epoch\\0<cid>\\0<epoch_id>`` basis."""
    return "inc_" + hashlib.sha256(
        f"epoch\x00{controller_id}\x00{epoch_id}".encode("utf-8")
    ).hexdigest()[:16]


def _final_status(cid="ctrl_a", *, purse_block=None, include_perf=True):
    """A retirement final_status shaped like ``get_bot_status`` output: ``performance``
    maps controller_id -> {custom_info: {purse: {...}}}. ``purse_block=None`` yields a
    custom_info WITHOUT a purse block."""
    perf = {}
    if include_perf:
        custom_info = {"purse": purse_block} if purse_block is not None else {}
        perf = {cid: {"status": "idle", "performance": {}, "custom_info": custom_info}}
    return {"status": "idle", "performance": perf}


class _BoomDict(dict):
    """A dict whose ``.get`` RAISES — passes ``isinstance(_, dict)`` so it reaches (and
    exercises) the extractor's defensive ``except`` (invariant 5)."""

    def get(self, *args, **kwargs):
        raise RuntimeError("hostile final_status")


class _FailingDB:
    """A db_manager whose ``get_session_context`` raises — the snapshot store is
    UNAVAILABLE (CDX-009), the case that must be distinguishable from empty."""

    def get_session_context(self):
        raise RuntimeError("snapshot store unavailable")


class _StubManager:
    """Returns a pre-set ``(data, degraded)`` from the degraded-aware method so the ROUTE
    can be exercised for both branches without a DB."""

    def __init__(self, result):
        self._result = result

    async def get_latest_controller_performance_result(self, bot_name=None):
        return self._result


async def _insert_row(db, *, controller_id, run_id, sha, sequence, records_json, harvested_at):
    """Insert one snapshot with an EXPLICIT ``harvested_at`` and ``source_bot_run_id`` so
    lineage-vs-harvest-time ordering can be pinned deterministically."""
    async with db.get_session_context() as session:
        session.add(PurseSnapshot(
            controller_id=controller_id, harvested_at=harvested_at,
            source_instance_name="inst", source_bot_run_id=run_id,
            purse_sha256=sha, sequence=sequence, records_json=records_json,
            derived_contributed="0", derived_withdrawn="0", derived_earned_realized="0",
            derived_earned_total="0", derived_unrealized="0", derived_drift="0",
            reference_price_used="150", opening_basis_quality=None,
        ))


# ===========================================================================
# 1. compute_harvest_markers — epoch markers (CLA-2A-07 / CLA-008)
# ===========================================================================


class TestHarvestMarkers:
    def test_latest_epoch_is_newest_opened_not_inception(self):
        """latest_epoch_id = the epoch opened by the NEWEST opening_epoch/reseed_epoch =
        'epoch-2', never the inception 'epoch-1'. MUTATION GUARD: returning records[0]'s
        epoch (a 'first opening' impl) yields 'epoch-1' and fails the != below."""
        m = compute_harvest_markers(two_reanchor_journal())
        assert m.latest_epoch_id == "epoch-2"
        assert m.latest_epoch_id != "epoch-1"

    def test_reanchor_count_is_the_number_of_reanchors(self):
        """Exactly two reanchor records. MUTATION GUARD: a constant, 0/1, or 'count every
        record' (would be 5) all disagree with the hand-counted 2."""
        m = compute_harvest_markers(two_reanchor_journal())
        assert m.reanchor_count == 2

    def test_degenerate_payloads_never_raise_and_yield_nulls(self):
        """Observation-only: a recordless/degenerate payload is (None, 0), never a raise
        (invariant 5) — so the harvest attaches nulls and proceeds."""
        assert compute_harvest_markers({}) == PurseHarvestMarkers(None, 0)
        assert compute_harvest_markers({"records": "not-a-list"}) == PurseHarvestMarkers(None, 0)
        assert compute_harvest_markers({"records": [1, "x", None]}) == PurseHarvestMarkers(None, 0)

    @pytest.mark.asyncio
    async def test_markers_persisted_by_harvest(self, tmp_path, db):
        """The harvest attaches the markers to the stored row. MUTATION GUARD: dropping the
        markers wiring (passing None) leaves reanchor_count/latest_epoch_id NULL and the
        asserts below fail. Also proves the harvest still inserts with NO final_status
        (source unavailable → source_degraded null) — the retirement is never blocked."""
        inst, _ = build_instance(tmp_path, controller_id="ctrl_a", purse_payload=two_reanchor_journal())
        outcomes = await harvest_instance_purses(str(inst), db_manager=db, source_instance_name="inst1")
        assert [o["decision"] for o in outcomes] == ["harvested"]
        rows = await history(db)
        assert len(rows) == 1
        assert rows[0].reanchor_count == 2
        assert rows[0].latest_epoch_id == "epoch-2"
        assert rows[0].source_degraded is None  # no final_status supplied -> unknowable


# ===========================================================================
# 2. source_degraded extraction (CLA-007)
# ===========================================================================


class TestSourceDegradedExtraction:
    def test_true_from_purse_degraded(self):
        fs = _final_status(purse_block={"purse_degraded": True})
        assert _extract_source_degraded(fs, "ctrl_a", "ctrl_a") is True

    def test_true_from_accounting_degraded(self):
        """MUTATION GUARD: checking only purse_degraded (not accounting_degraded) returns
        None here and fails — both engine flags must be honored."""
        fs = _final_status(purse_block={"accounting_degraded": True})
        assert _extract_source_degraded(fs, "ctrl_a", "ctrl_a") is True

    def test_false_when_flags_present_but_false(self):
        """A clean pre-stop status is a DEFINED False, not null. MUTATION GUARD: an
        'always True' impl fails here."""
        fs = _final_status(purse_block={"purse_degraded": False, "accounting_degraded": False})
        assert _extract_source_degraded(fs, "ctrl_a", "ctrl_a") is False

    def test_none_when_no_flag_present(self):
        """Absence is NEVER fabricated healthy. MUTATION GUARD: defaulting to False when no
        flag is present (fabricating healthy) returns False here and fails the `is None`."""
        fs = _final_status(purse_block={"other": 1})
        assert _extract_source_degraded(fs, "ctrl_a", "ctrl_a") is None

    def test_none_when_no_purse_block(self):
        fs = _final_status(purse_block=None)  # custom_info carries no purse block
        assert _extract_source_degraded(fs, "ctrl_a", "ctrl_a") is None

    def test_none_when_controller_absent_from_performance(self):
        fs = _final_status(cid="other_ctrl", purse_block={"purse_degraded": True})
        assert _extract_source_degraded(fs, "ctrl_a", "ctrl_a") is None

    def test_none_when_final_status_absent(self):
        assert _extract_source_degraded(None, "ctrl_a", "ctrl_a") is None

    def test_none_and_never_raises_on_hostile_final_status(self):
        """Invariant 5: a hostile final_status that raises on access is a None, never a
        raise. MUTATION GUARD: removing the extractor's try/except lets the RuntimeError
        escape here."""
        assert _extract_source_degraded(_BoomDict(), "ctrl_a", "ctrl_a") is None

    def test_matches_on_raw_controller_id_when_canonical_differs(self):
        """The extractor tries the canonical id then the raw config id, since final_status
        is keyed by the report's controller id."""
        fs = {"performance": {"ctrl_raw": {"custom_info": {"purse": {"purse_degraded": True}}}}}
        assert _extract_source_degraded(fs, "ctrl_raw", "ctrl_canonical") is True


class TestSourceDegradedThroughHarvest:
    @pytest.mark.asyncio
    async def test_degraded_true_persisted(self, tmp_path, db):
        inst, _ = build_instance(tmp_path, controller_id="ctrl_a", purse_payload=comprehensive_purse())
        fs = _final_status(purse_block={"purse_degraded": True})
        outcomes = await harvest_instance_purses(
            str(inst), db_manager=db, source_instance_name="inst1", final_status=fs
        )
        assert [o["decision"] for o in outcomes] == ["harvested"]
        rows = await history(db)
        assert rows[0].source_degraded is True

    @pytest.mark.asyncio
    async def test_clean_status_persists_false(self, tmp_path, db):
        inst, _ = build_instance(tmp_path, controller_id="ctrl_a", purse_payload=comprehensive_purse())
        fs = _final_status(purse_block={"purse_degraded": False})
        await harvest_instance_purses(str(inst), db_manager=db, source_instance_name="inst1", final_status=fs)
        rows = await history(db)
        assert rows[0].source_degraded is False

    @pytest.mark.asyncio
    async def test_harvest_not_blocked_when_final_status_raises(self, tmp_path, db):
        """Invariant 5 through the harvest: a hostile final_status that RAISES on access
        must NOT block the harvest — the snapshot is STILL inserted, with source_degraded
        null. MUTATION GUARD: dropping the extractor's try/except lets the raise propagate
        into the per-controller handler, the controller is skipped, and NO row lands — the
        len==1 and 'harvested' asserts then fail."""
        inst, _ = build_instance(tmp_path, controller_id="ctrl_a", purse_payload=comprehensive_purse())
        outcomes = await harvest_instance_purses(
            str(inst), db_manager=db, source_instance_name="inst1", final_status=_BoomDict(),
        )
        assert [o["decision"] for o in outcomes] == ["harvested"]  # not blocked / not skipped
        rows = await history(db)
        assert len(rows) == 1
        assert rows[0].source_degraded is None


# ===========================================================================
# 3. Provenance surfacing — markers appear on the responses (P1/P2 + get_purse)
# ===========================================================================


class TestProvenanceSurfacing:
    @pytest.mark.asyncio
    async def test_get_purse_surfaces_all_three_markers(self, tmp_path, db):
        """GET /purse/{id} provenance carries the epoch markers + source_degraded (the
        same PurseProvenance block P1/P2 reuse). MUTATION GUARD: not reading the new
        columns in _provenance leaves them at their None defaults and the asserts fail."""
        inst, _ = build_instance(tmp_path, controller_id="ctrl_a", purse_payload=two_reanchor_journal())
        fs = _final_status(purse_block={"purse_degraded": True})
        await harvest_instance_purses(str(inst), db_manager=db, source_instance_name="inst1", final_status=fs)

        resp = await get_purse("ctrl_a", db_manager=db)
        assert resp.provenance.reanchor_count == 2
        assert resp.provenance.latest_epoch_id == "epoch-2"
        assert resp.provenance.source_degraded is True


# ===========================================================================
# 4. Perf-latest degraded flag (CDX-009)
# ===========================================================================


class TestPerfLatestDegradedOrchestrator:
    @pytest.mark.asyncio
    async def test_repo_failure_flags_degraded(self, db):
        """A snapshot-store read failure returns ([], True) — the UNAVAILABLE signal.
        MUTATION GUARD: collapsing to ([], False) (the old silent-empty behavior) makes the
        degraded assert fail; the empty list alone can't be told apart from 'no data'."""
        orch = make_orchestrator(db, StubMQTT())
        orch.db_manager = _FailingDB()
        data, degraded = await orch.get_latest_controller_performance_result()
        assert data == []
        assert degraded is True

    @pytest.mark.asyncio
    async def test_healthy_read_is_not_degraded(self, db):
        """A healthy read of real rows is (rows, False). MUTATION GUARD: an 'always True'
        degraded impl fails here (the healthy path must be distinguishable)."""
        async with db.get_session_context() as session:
            await ControllerPerformanceRepository(session).save_controller_performance(
                bot_name="b1", controller_id="c1", status="running",
                performance={"net_pnl_quote": 1}, custom_info={"purse": {"contributed": "1"}},
            )
        orch = make_orchestrator(db, StubMQTT())
        data, degraded = await orch.get_latest_controller_performance_result()
        assert degraded is False
        assert len(data) == 1
        assert data[0]["controller_id"] == "c1"


class TestPerfLatestDegradedRoute:
    @pytest.mark.asyncio
    async def test_route_adds_degraded_only_on_failure(self):
        """CDX-009: the failure path adds an additive degraded:true. MUTATION GUARD:
        dropping the `if degraded: response['degraded'] = True` line makes the response
        `{"status":"success","data":[]}` — indistinguishable from empty — and this fails."""
        resp = await get_latest_controller_performance(bot_name=None, bots_manager=_StubManager(([], True)))
        assert resp == {"status": "success", "data": [], "degraded": True}

    @pytest.mark.asyncio
    async def test_route_success_shape_is_byte_identical_with_data(self):
        """Regression guard: when there IS data the response is byte-identical to before —
        no `degraded` key. MUTATION GUARD: unconditionally adding degraded (even on
        success) makes `"degraded" not in resp` fail."""
        rows = [{"controller_id": "c1", "status": "running"}]
        resp = await get_latest_controller_performance(bot_name=None, bots_manager=_StubManager((rows, False)))
        assert resp == {"status": "success", "data": rows}
        assert "degraded" not in resp


# ===========================================================================
# 5. Incarnation-aware history ordering (CDX-005 / CLA-309)
# ===========================================================================


class TestHistoryLineageOrdering:
    @pytest.mark.asyncio
    async def test_stale_late_harvested_instance_does_not_sort_newest(self, db):
        """The load-bearing CLA-309 property. Two incarnations of one controller:

          * B — the GENUINELY newer incarnation: higher source_bot_run_id (2), but
            harvested EARLY (2026-01) with a LOWER sequence and a lower row id.
          * A — the STALE, late-archived older incarnation: lower run id (1), harvested
            LATE (2026-06) with a HIGHER sequence and a higher row id.

        Lineage-correct ordering (source_bot_run_id) sorts B first; the stale A does NOT
        masquerade as newest. MUTATION GUARDS (each puts A wrongly first, failing the
        asserts): order by harvested_at (A is later); by sequence (A's is 99); by id (A's
        is higher). Each row also carries its own spec-derived incarnation_id (distinct).
        """
        await _insert_row(
            db, controller_id="ctrl_a", run_id=2, sha="sha-B", sequence=1,
            records_json=_journal_json(epoch_id="epoch-beta"),
            harvested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _insert_row(
            db, controller_id="ctrl_a", run_id=1, sha="sha-A", sequence=99,
            records_json=_journal_json(epoch_id="epoch-alpha"),
            harvested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

        resp = await get_purse_history("ctrl_a", limit=100, offset=0, db_manager=db)
        entries = resp.snapshots
        assert len(entries) == 2
        # B (run id 2) first despite its earlier harvest / lower seq / lower id.
        assert entries[0].provenance.source_bot_run_id == 2
        assert entries[1].provenance.source_bot_run_id == 1
        assert entries[0].provenance.source_bot_run_id != 1  # the stale A is NOT newest
        # incarnation_id: present, distinct, derived from each row's OWN journal.
        assert entries[0].incarnation_id == _spec_incarnation_id("ctrl_a", "epoch-beta")
        assert entries[1].incarnation_id == _spec_incarnation_id("ctrl_a", "epoch-alpha")
        assert entries[0].incarnation_id != entries[1].incarnation_id

    @pytest.mark.asyncio
    async def test_all_null_run_ids_fall_back_to_harvested_at(self, db):
        """Backward-compat: a fleet of all-NULL source_bot_run_id rows degrades to exactly
        the previous newest-by-harvest order (nullslast keeps them together, then
        harvested_at DESC decides — NOT the journal sequence, which is invalid across
        incarnations). The fixture deliberately ANTI-CORRELATES sequence with harvest time:
        the newest-harvested row carries the LOWER sequence and the stale older row the
        HIGHER sequence. So only harvested_at ordering yields [seq 1, seq 99]; the forbidden
        desc(sequence) ordering would yield [99, 1] and fail. MUTATION GUARD (CDX-R03):
        replacing desc(harvested_at) with desc(sequence) in get_history_for_controller puts
        the stale higher-sequence row first and fails both asserts below."""
        await _insert_row(
            db, controller_id="ctrl_a", run_id=None, sha="sha-stale", sequence=99,
            records_json=_journal_json(epoch_id="epoch-1"),
            harvested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),  # OLDER harvest, HIGHER seq
        )
        await _insert_row(
            db, controller_id="ctrl_a", run_id=None, sha="sha-recent", sequence=1,
            records_json=_journal_json(epoch_id="epoch-1"),
            harvested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),  # NEWER harvest, LOWER seq
        )
        resp = await get_purse_history("ctrl_a", limit=100, offset=0, db_manager=db)
        # Newest HARVEST leads, regardless of the (anti-correlated) sequence:
        assert [e.provenance.sequence for e in resp.snapshots] == [1, 99]
        assert [e.provenance.purse_sha256 for e in resp.snapshots] == ["sha-recent", "sha-stale"]

    @pytest.mark.asyncio
    async def test_latest_surfaces_select_lineage_newest_not_late_stale(self, db):
        """CDX-R01: the CURRENT-STATE surfaces (GET /purse/{id}, /activity, /timeseries) all
        resolve the newest snapshot via the SAME lineage-correct ordering as history — so a
        late-archived STALE incarnation (higher harvested_at, LOWER run id) cannot win on the
        primary card while history shows the genuinely-newer one first. All four surfaces
        must agree on run id 2. MUTATION GUARD: ordering get_latest_for_controller by
        harvested_at alone (the pre-fix behavior) selects the stale run-1 row and every
        `== 2` assert flips to 1.

        Both rows carry the same (valid, computable) journal bytes so /activity and
        /timeseries actually derive over a real journal rather than 500-ing."""
        # B — the GENUINELY newer incarnation: run id 2, harvested EARLY (2026-01).
        await _insert_row(
            db, controller_id="ctrl_a", run_id=2, sha="sha-B", sequence=1,
            records_json=json.dumps(two_reanchor_journal()),
            harvested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        # A — the STALE, late-archived older incarnation: run id 1, harvested LATE (2026-06).
        await _insert_row(
            db, controller_id="ctrl_a", run_id=1, sha="sha-A", sequence=99,
            records_json=json.dumps(two_reanchor_journal()),
            harvested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

        purse = await get_purse("ctrl_a", db_manager=db)
        activity = await get_purse_activity("ctrl_a", db_manager=db)
        timeseries = await get_purse_timeseries("ctrl_a", db_manager=db)
        hist = await get_purse_history("ctrl_a", limit=100, offset=0, db_manager=db)

        # Every current-state surface selects the lineage-newest incarnation (run 2)...
        assert purse.provenance.source_bot_run_id == 2
        assert activity.provenance.source_bot_run_id == 2
        assert timeseries.provenance.source_bot_run_id == 2
        # ...and history agrees — the surfaces can never disagree about "newest" now.
        assert hist.snapshots[0].provenance.source_bot_run_id == 2

    @pytest.mark.asyncio
    async def test_history_incarnation_id_through_asgi_no_records_leak(self, db, asgi_main):
        """Through the real app: history entries carry incarnation_id, ordered
        lineage-correct, and NEVER leak records_json (it is consumed only to derive the
        id). MUTATION GUARD: leaking records_json onto the entry would put it in the body."""
        from deps import get_database_manager

        await _insert_row(
            db, controller_id="ctrl_a", run_id=2, sha="sha-B", sequence=1,
            records_json=_journal_json(epoch_id="epoch-beta"),
            harvested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _insert_row(
            db, controller_id="ctrl_a", run_id=1, sha="sha-A", sequence=99,
            records_json=_journal_json(epoch_id="epoch-alpha"),
            harvested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["snapshots"][0]["provenance"]["source_bot_run_id"] == 2
        assert body["snapshots"][0]["incarnation_id"] == _spec_incarnation_id("ctrl_a", "epoch-beta")
        assert body["snapshots"][1]["incarnation_id"] == _spec_incarnation_id("ctrl_a", "epoch-alpha")
        assert "records_json" not in json.dumps(body)


# ===========================================================================
# 5b. Content-dedup provenance refresh (CDX-R02) — a byte-identical re-harvest by a
#     lineage-newer retirement must not lose its source_degraded observation, and a
#     stale retirement must never clobber a newer one.
# ===========================================================================


async def _dedup_insert(db, *, run_id, degraded, sha="sha-same", inst="inst"):
    """Insert-if-absent one snapshot for fixed content ``sha`` with an explicit run id and
    source_degraded — the knobs the CDX-R02 refresh keys on. records_json is irrelevant to
    the dedup (keyed on controller_id + sha), so a stub is fine."""
    async with db.get_session_context() as session:
        return await PurseSnapshotRepository(session).insert_snapshot_if_absent(
            controller_id="ctrl_a", source_instance_name=inst, source_bot_run_id=run_id,
            purse_sha256=sha, sequence=1, records_json="{}",
            derived_contributed="0", derived_withdrawn="0", derived_earned_realized="0",
            derived_earned_total="0", derived_unrealized="0", derived_drift="0",
            reference_price_used="150", opening_basis_quality=None, source_degraded=degraded,
        )


class TestContentDedupProvenanceRefresh:
    @pytest.mark.asyncio
    async def test_newer_retirement_refreshes_degraded_on_identical_content(self, db):
        """The reviewer's exact CDX-R02 scenario: content harvested clean (False) under run 1,
        then re-harvested BYTE-IDENTICAL but DEGRADED (True) under a lineage-newer run 2. The
        later degraded observation must survive — refreshed onto the single content row — and
        NO second row is created (dedup preserved). MUTATION GUARD: dropping the
        `_is_lineage_newer` refresh block leaves source_degraded stuck at the first False."""
        first = await _dedup_insert(db, run_id=1, degraded=False, inst="inst1")
        assert first is not None                        # inserted the content row
        dup = await _dedup_insert(db, run_id=2, degraded=True, inst="inst2")
        assert dup is None                              # still a content-dedup: no 2nd row
        rows = await history(db)
        assert len(rows) == 1                           # exactly one content row
        assert rows[0].source_degraded is True          # REFRESHED from the newer retirement
        assert rows[0].source_bot_run_id == 2           # lineage advanced to the newer run
        assert rows[0].source_instance_name == "inst2"

    @pytest.mark.asyncio
    async def test_stale_retirement_does_not_clobber_newer_observation(self, db):
        """CLA-309 safety: a LATE stale retirement (LOWER run id) re-harvesting identical
        content must NOT overwrite the newer observation — the refresh is lineage-GUARDED.
        MUTATION GUARD: an unguarded update (refresh on any duplicate) would let run 1 clobber
        run 2's True back to False and fail the `is True` below."""
        await _dedup_insert(db, run_id=2, degraded=True, inst="inst2")   # newer first
        await _dedup_insert(db, run_id=1, degraded=False, inst="inst1")  # stale, lower run id
        rows = await history(db)
        assert len(rows) == 1
        assert rows[0].source_degraded is True          # newer observation preserved
        assert rows[0].source_bot_run_id == 2
        assert rows[0].source_instance_name == "inst2"

    @pytest.mark.asyncio
    async def test_same_run_reharvest_is_a_plain_noop(self, db):
        """Equal run id is NOT lineage-newer: an identical-content, identical-lineage
        re-harvest is a pure dedup no-op (never an over-eager refresh). MUTATION GUARD:
        loosening `_is_lineage_newer` to `>=` would let this refresh False->True and fail."""
        await _dedup_insert(db, run_id=5, degraded=False)
        await _dedup_insert(db, run_id=5, degraded=True)   # same run id -> no refresh
        rows = await history(db)
        assert len(rows) == 1
        assert rows[0].source_degraded is False            # unchanged

    @pytest.mark.asyncio
    async def test_null_lineage_never_refreshes(self, db):
        """Ambiguous lineage (either run id NULL) never touches the stored observation —
        conservative: we neither clobber on doubt nor fabricate an ordering. A first
        resolved (run 3, True) is preserved when a later NULL-run re-harvest arrives."""
        await _dedup_insert(db, run_id=3, degraded=True, inst="inst3")
        await _dedup_insert(db, run_id=None, degraded=False, inst="instX")  # NULL -> not newer
        rows = await history(db)
        assert len(rows) == 1
        assert rows[0].source_degraded is True
        assert rows[0].source_bot_run_id == 3


# ===========================================================================
# 6. End-to-end: degraded source captured through the REAL retirement, not blocking
# ===========================================================================


class ReportingMQTT(StubMQTT):
    """A StubMQTT whose pre-stop controller report carries a degraded purse block, so the
    retirement's Step-1 ``get_bot_status`` captures a source_degraded=True final_status —
    exercising the full thread final_status -> _harvest_purses -> harvest -> persisted."""

    def get_bot_controller_reports(self, bot_id):
        return {"ctrl_a": {"performance": {}, "custom_info": {"purse": {"purse_degraded": True}}}}


class TestSourceDegradedThroughRetirement:
    @pytest.mark.asyncio
    async def test_degraded_source_captured_and_retirement_completes(self, tmp_path, db, monkeypatch):
        """Drive the REAL retirement state machine. The pre-stop status is degraded; the
        harvest captures source_degraded=True AND the markers, and the retirement STILL
        runs to completion (the archiver rmtrees the dir, the run finalizes) — the honesty
        additions never block the retirement (invariant 5). MUTATION GUARD: not threading
        final_status from stop_and_archive_bot -> _harvest_purses leaves source_degraded
        null and the True assert fails."""
        monkeypatch.chdir(tmp_path)
        build_instance(tmp_path, name="bot1", controller_id="ctrl_a",
                       purse_payload=two_reanchor_journal())
        await seed_bot_run(db, bot_name="bot1", account_name="acct")

        orch = make_orchestrator(db, ReportingMQTT())
        archiver = RmtreeArchiver()
        await run_fsm(orch, StubDockerManager(exit_code=0), archiver)

        # The retirement ran to completion (archived, then the dir removed).
        assert archiver.archived == ["bot1"]
        rows = await history(db)
        assert len(rows) == 1
        assert rows[0].source_degraded is True      # captured from the pre-stop final_status
        assert rows[0].reanchor_count == 2          # epoch markers persisted too
        assert rows[0].latest_epoch_id == "epoch-2"
        assert rows[0].source_bot_run_id is not None  # lineage link resolved through the FSM

    @pytest.mark.asyncio
    async def test_harvest_exception_does_not_block_retirement(self, tmp_path, db, monkeypatch):
        """Invariant 5 at the ORCHESTRATION BOUNDARY (CDX-R04). The prior test proves a
        SUCCESSFUL harvest does not block retirement; this proves the load-bearing part: if
        the harvest delegate itself RAISES an unexpected error, the broad barrier in
        BotsOrchestrator._harvest_purses must swallow it so the retirement still finishes —
        the archiver runs and the run finalizes, an observation-only concern never becoming
        load-bearing on the retirement path.

        The harvest is driven through the REAL retirement FSM with a delegate monkeypatched
        to raise; only the outer barrier stands between that raise and an aborted archive.
        MUTATION GUARD: narrowing that barrier from `except Exception` to `except KeyError`
        (services/bots_orchestrator.py) lets the injected RuntimeError escape _harvest_purses,
        propagate to the retirement's outer handler, and SKIP Step 6 (archive) — so
        `archiver.archived` stays empty and the `== ['bot1']` assert fails."""
        monkeypatch.chdir(tmp_path)
        build_instance(tmp_path, name="bot1", controller_id="ctrl_a",
                       purse_payload=two_reanchor_journal())
        await seed_bot_run(db, bot_name="bot1", account_name="acct")

        async def _boom(*args, **kwargs):
            raise RuntimeError("injected harvest failure")

        # Patch the name _harvest_purses actually calls (imported into the orchestrator module).
        monkeypatch.setattr("services.bots_orchestrator.harvest_instance_purses", _boom)

        orch = make_orchestrator(db, ReportingMQTT())
        archiver = RmtreeArchiver()
        await run_fsm(orch, StubDockerManager(exit_code=0), archiver)

        # The harvest raised, but the retirement still ran to completion (archive happened).
        assert archiver.archived == ["bot1"]
        # No snapshot landed (harvest aborted before any insert) — yet retirement was unaffected.
        assert await history(db) == []
