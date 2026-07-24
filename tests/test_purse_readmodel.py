"""hbpurseapi Phase 3 — retirement-harvest read-model + purse endpoints.

Covers the DERIVED, NON-AUTHORITATIVE purse read-model (design Phase E, required
changes #9-#10, ADDENDUM A5):

  * ``services.purse_read_model.compute_derived_metrics`` — the contract-v1 formulas,
    a line-for-line mirror of the engine's ``PurseLedger.derived_metrics``. Every
    expected value is HAND-COMPUTED from the PINNED "Purse journal contract v1"
    (independent Decimal arithmetic in the test), NEVER captured by running the
    implementation — so a dropped formula term (an earned base-mark, the fees-netted
    quote delta, the withdrawn term, the reanchor drift cut, the owned-vs-records
    reconciliation) makes the arithmetic disagree and the test fail.
  * ``services.purse_harvest.harvest_instance_purses`` — the retirement-time harvest:
    a valid purse inserts one snapshot with the hand-computed metrics; re-harvesting
    the identical journal inserts NO duplicate; an INVALID purse logs + skips WITHOUT
    a row and WITHOUT raising (observation, not control); a journal that GREW (new
    sha/sequence) inserts a second row; an absent purse harvests nothing.
  * The endpoints — ``GET /purse/{id}`` returns the newest snapshot with provenance and
    404s an unknown controller; ``GET /purse/{id}/history`` is newest-first and EXCLUDES
    ``records_json``.
  * ORDERING (the load-bearing safety property, required change #9): driven through the
    REAL ``BotsOrchestrator.stop_and_archive_bot``, the harvest runs BEFORE the archive
    step moves/deletes the instance dir — an archiver that ``rmtree``s the dir still
    leaves a harvested snapshot, because the harvest already ran. Reordering the harvest
    after cleanup makes this fail.

Test authenticity: persistence runs the REAL repositories against a REAL sqlite DB
(the sync-session adapter from test_retirement_fsm); the harvest reads REAL purse
files under tmp_path; only Docker/MQTT/the archiver are stubbed. Expected numbers are
spec-derived. The end-to-end ORDERING test reuses the proven retirement-FSM harness
(``make_orchestrator``/``run_fsm``/stubs) rather than re-mocking the state machine.
"""
import hashlib
import json
import os
import shutil
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
import yaml
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, BotRun, PurseSnapshot
from database.repositories.purse_snapshot_repository import PurseSnapshotRepository
from ledger_fixtures import (
    checkpoint_record,
    fills_rollup_record,
    flow_record,
    opening_epoch_record,
    reanchor_record,
    valid_purse_payload,
)
from models.purse import AUTHORITY_NOTE
from routers.purse import get_purse, get_purse_history
from services.purse_harvest import harvest_instance_purses
from services.purse_read_model import compute_derived_metrics, resolve_current_state
from services.resume_service import _expected_ledger_name, _expected_purse_name

# The proven retirement-FSM harness — reused verbatim for the ordering test so the
# state machine is driven for real, not re-mocked (drift-free).
from database.repositories.bot_run_repository import (
    RETIREMENT_UNVERIFIED,
    RETIREMENT_VERIFIED,
)
from test_retirement_fsm import (
    FAST,
    StubMQTT,
    fetch_run,
    make_orchestrator,
    run_fsm,
    seed_bot_run,
    seed_order,
)


# ---------------------------------------------------------------------------
# Real-DB plumbing (sync sqlite adapted to the async session surface)
# ---------------------------------------------------------------------------

class SyncSessionAdapter:
    """Awaitable facade over a synchronous Session — real SQL, real sqlite."""

    def __init__(self, session):
        self._session = session

    def add(self, obj):
        self._session.add(obj)

    async def execute(self, *args, **kwargs):
        return self._session.execute(*args, **kwargs)

    async def flush(self):
        self._session.flush()

    async def refresh(self, obj):
        self._session.refresh(obj)

    async def delete(self, obj):
        self._session.delete(obj)


class RealSqliteDBManager:
    """get_session_context() against one shared in-memory sqlite DB."""

    def __init__(self):
        self.engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def get_session_context(self):
        session = self._sessionmaker()
        try:
            yield SyncSessionAdapter(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@pytest.fixture
def db():
    return RealSqliteDBManager()


# ---------------------------------------------------------------------------
# THE SPEC — a comprehensive journal whose every term is non-zero and distinct,
# with expected metrics computed by INDEPENDENT Decimal arithmetic (the PINNED
# contract formulas), never by running compute_derived_metrics.
# ---------------------------------------------------------------------------

# opening owned (300 quote, 1 base) + one buy (-45 quote, +0.3 base) => the checkpoint's
# owned (255, 1.3) EXACTLY equals opening+rollup, so the owned-vs-records reconciliation
# residual is zero and drift is clean (drift is exercised separately below).
COMPREHENSIVE_RECORDS = [
    opening_epoch_record(
        seq=1, ts=1700000000.0, epoch_id="epoch-1",
        owned_quote="300", owned_base="1", reference_price="150",
        contributed_opening_quote="400", earned_opening_quote="25",
        opening_basis_quality="reconstructed",
    ),
    flow_record(seq=2, ts=1700000200.0, flow_kind="deposit", quote_valuation="116.03"),
    flow_record(seq=3, ts=1700000300.0, flow_kind="withdrawal", quote_valuation="20"),
    fills_rollup_record(
        seq=4, ts=1700000400.0, epoch_id="epoch-1",
        quote_delta_cum="-45", base_delta_cum="0.3", fees_quote_cum="0.5",
    ),
    checkpoint_record(
        seq=5, ts=1700000500.0, epoch_id="epoch-1",
        owned_quote="255", owned_base="1.3", reference_price="160", equity_quote="463",
    ),
]


def comprehensive_purse(controller_id="ctrl_a"):
    return valid_purse_payload(controller_id=controller_id, records=list(COMPREHENSIVE_RECORDS))


def expected_comprehensive_metrics():
    """Contract-v1 metrics for COMPREHENSIVE_RECORDS, computed independently.

    ref is the newest checkpoint/epoch reference_price = 160 (the seq-5 checkpoint);
    owned_* is the newest owned-bearing record = the checkpoint's (255, 1.3).
    """
    ref = Decimal("160")
    contributed = Decimal("400") + Decimal("116.03")            # opening + deposit
    withdrawn = Decimal("20")                                    # withdrawal
    earned_realized = Decimal("25") + (Decimal("-45") + Decimal("0.3") * ref)  # opening + (quote + base*ref)
    equity = Decimal("255") + Decimal("1.3") * ref              # owned_quote + owned_base*ref
    earned_total = equity - contributed + withdrawn
    unrealized = earned_total - earned_realized
    return {
        "contributed": contributed,
        "withdrawn": withdrawn,
        "earned_realized": earned_realized,
        "earned_total": earned_total,
        "unrealized": unrealized,
        "drift": Decimal("0"),
        "equity_quote": equity,
        "reference_price_used": ref,
    }


# ===========================================================================
# 1. compute_derived_metrics — hand-computed contract arithmetic
# ===========================================================================

class TestDerivedMetrics:
    def test_every_term_matches_hand_computed(self):
        m = compute_derived_metrics(comprehensive_purse())
        exp = expected_comprehensive_metrics()
        # Decimal == is numeric (48 == 48.0), so trailing-zero representation is irrelevant.
        assert m.contributed == exp["contributed"]
        assert m.withdrawn == exp["withdrawn"]
        assert m.earned_realized == exp["earned_realized"]
        assert m.earned_total == exp["earned_total"]
        assert m.unrealized == exp["unrealized"]
        assert m.drift == exp["drift"]
        assert m.equity_quote == exp["equity_quote"]
        assert m.reference_price_used == exp["reference_price_used"]
        assert m.owned_quote == Decimal("255")
        assert m.owned_base == Decimal("1.3")
        assert m.opening_basis_quality == "reconstructed"
        assert m.reference_source == "checkpoint@seq5"

    def test_earned_realized_needs_the_base_mark_term(self):
        """The base_delta_cum * ref term is load-bearing: -45 + 0.3*160 = 3, so
        earned_realized = 25 + 3 = 28. Dropping base*ref would give 25 + (-45) = -20."""
        m = compute_derived_metrics(comprehensive_purse())
        assert m.earned_realized == Decimal("28")
        assert m.earned_realized != Decimal("-20")

    def test_reference_price_is_the_newest_checkpoint_epoch(self):
        """Two checkpoints with different reference_price → the NEWEST wins, and it is
        applied uniformly (not the opening's 150)."""
        records = [
            opening_epoch_record(seq=1, epoch_id="epoch-1", reference_price="150"),
            checkpoint_record(seq=2, epoch_id="epoch-1", reference_price="150"),
            checkpoint_record(seq=3, epoch_id="epoch-1", reference_price="175"),
        ]
        state = resolve_current_state(records)
        assert state.reference_price == Decimal("175")
        assert state.reference_source == "checkpoint@seq3"

    def test_reanchor_cut_surfaces_as_drift(self):
        """The CLA-M01 collapse trace: owned 600 → 170 (a 400 reserve withdrawal + 30
        over-claim). With no checkpoint the drift is exactly the reanchor cut valued at
        ref: max(0, 600-170) + 0 = 430. Dropping the drift cut term would give 0."""
        records = [
            opening_epoch_record(
                seq=1, epoch_id="epoch-1", owned_quote="600", owned_base="0",
                reference_price="150", contributed_opening_quote="600", earned_opening_quote="0",
            ),
            reanchor_record(
                seq=2, epoch_id="epoch-1", old_owned_quote="600", new_owned_quote="170",
                old_owned_base="0", new_owned_base="0", overclaim_quote="430",
                classification="undeclared_outflow", wallet_quote_total="170", wallet_base_total="0",
            ),
        ]
        m = compute_derived_metrics(valid_purse_payload(records=records))
        assert m.drift == Decimal("430")
        assert m.owned_quote == Decimal("170")          # reanchor moved owned
        assert m.reference_price_used == Decimal("150")  # reanchor carries no ref

    def test_checkpoint_reconciliation_surfaces_lost_delta_as_drift(self):
        """CDX-R02: once checkpointing, an owned that the records under-explain is
        surfaced as drift (a crash advanced owned without a rollup record). Opening 100,
        rollup -10 → records imply 90; checkpoint owned 95 → drift = 95 - 90 = 5. Never
        silently absorbed."""
        records = [
            opening_epoch_record(
                seq=1, epoch_id="epoch-1", owned_quote="100", owned_base="0",
                reference_price="150", contributed_opening_quote="100", earned_opening_quote="0",
            ),
            fills_rollup_record(
                seq=2, epoch_id="epoch-1", quote_delta_cum="-10", base_delta_cum="0", fees_quote_cum="0",
            ),
            checkpoint_record(
                seq=3, epoch_id="epoch-1", owned_quote="95", owned_base="0", reference_price="150",
            ),
        ]
        m = compute_derived_metrics(valid_purse_payload(records=records))
        assert m.drift == Decimal("5")

    def test_drift_reanchor_before_checkpoint_surfaces_magnitude_not_ledger_cut(self):
        """CDX-R03: an OBSERVATION-only ``classification='drift'`` reanchor (the engine's
        carried-prune / uncommitted-fill record: owned NOT resized, new_owned_*=0 encodes
        the surfaced magnitude — range_inventory_ladder.py:4708-4800) must surface its
        magnitude as drift WITHOUT being double-counted through the ledger reconciliation.

        Opening owned 100; a drift reanchor 30->0 (magnitude 30); checkpoint owned 100.
        Because a drift reanchor is NOT an owned-ledger cut, the implied owned stays 100
        (= checkpoint), the reconciliation residual is 0, and drift is exactly the 30
        magnitude. Treating the drift cut as an ``undeclared_outflow`` ledger cut (moving
        the imp_cut accumulator) would make implied 70, add a spurious +30 residual, and
        report drift 60 — the bug this guards.
        """
        records = [
            opening_epoch_record(
                seq=1, epoch_id="epoch-1", owned_quote="100", owned_base="0",
                reference_price="150", contributed_opening_quote="100", earned_opening_quote="0",
            ),
            reanchor_record(
                seq=2, epoch_id="epoch-1", old_owned_quote="30", new_owned_quote="0",
                old_owned_base="0", new_owned_base="0", overclaim_quote="30",
                classification="drift", wallet_quote_total="0", wallet_base_total="0",
            ),
            checkpoint_record(
                seq=3, epoch_id="epoch-1", owned_quote="100", owned_base="0", reference_price="150",
            ),
        ]
        m = compute_derived_metrics(valid_purse_payload(records=records))
        assert m.owned_ambiguous is False        # a superseding checkpoint follows
        assert m.owned_quote == Decimal("100")   # owned from the checkpoint, NOT the drift's 0
        assert m.equity_quote == Decimal("100")
        assert m.drift == Decimal("30")          # exactly the magnitude, not 60
        assert m.earned_total == Decimal("0")

    def test_resolve_flags_terminal_drift_reanchor_ambiguous(self):
        """CDX-R03: current owned resolution must reflect the engine's observation-only
        drift semantics. A ``drift`` reanchor is ambiguous (observation vs sub-dust cut),
        so it must NOT overwrite owned to its new_owned_*, and a TERMINAL one leaves the
        state flagged ambiguous. An ``undeclared_outflow`` reanchor IS a committed cut and
        DOES establish owned; a later checkpoint clears the ambiguity."""
        opening = opening_epoch_record(
            seq=1, epoch_id="epoch-1", owned_quote="100", owned_base="0", reference_price="150",
        )
        drift = reanchor_record(
            seq=2, epoch_id="epoch-1", old_owned_quote="10", new_owned_quote="0",
            old_owned_base="0", new_owned_base="0", classification="drift",
            overclaim_quote="10", wallet_quote_total="0", wallet_base_total="0",
        )
        # Terminal drift reanchor → owned unchanged (NOT 0) + flagged ambiguous.
        s_terminal = resolve_current_state([opening, drift])
        assert s_terminal.owned_ambiguous is True
        assert s_terminal.owned_quote == Decimal("100")

        # Drift then checkpoint → checkpoint supersedes, flag cleared.
        ckpt = checkpoint_record(seq=3, epoch_id="epoch-1", owned_quote="100", reference_price="150")
        s_superseded = resolve_current_state([opening, drift, ckpt])
        assert s_superseded.owned_ambiguous is False
        assert s_superseded.owned_quote == Decimal("100")

        # A real undeclared_outflow cut IS authoritative: owned = new_owned, not ambiguous.
        outflow = reanchor_record(
            seq=2, epoch_id="epoch-1", old_owned_quote="100", new_owned_quote="70",
            old_owned_base="0", new_owned_base="0", classification="undeclared_outflow",
            overclaim_quote="30", wallet_quote_total="70", wallet_base_total="0",
        )
        s_outflow = resolve_current_state([opening, outflow])
        assert s_outflow.owned_ambiguous is False
        assert s_outflow.owned_quote == Decimal("70")


# ===========================================================================
# 2. Instance-dir scaffolding for the harvest
# ===========================================================================

def build_instance(tmp_path, *, name="inst1", controller_id="ctrl_a",
                   controller_name="range_inventory_ladder", purse_payload=None,
                   state_file_name=None):
    """Create bots/instances/<name>/{conf/controllers, data} with one controller yaml
    and (optionally) its purse journal, named via the copy-forward's OWN helpers so the
    harvest looks exactly where the hook would have written."""
    inst = tmp_path / "bots" / "instances" / name
    cdir = inst / "conf" / "controllers"
    ddir = inst / "data"
    cdir.mkdir(parents=True, exist_ok=True)
    ddir.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": controller_id,
        "controller_name": controller_name,
        "controller_type": "market_making",
        "connector_name": "nonkyc",
        "trading_pair": "XMR-USDT",
    }
    if state_file_name is not None:
        doc["state_file_name"] = state_file_name
    (cdir / f"{controller_id}.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    purse_name = None
    if purse_payload is not None:
        ledger_name = _expected_ledger_name(controller_id, state_file_name)
        purse_name = _expected_purse_name(ledger_name)
        (ddir / purse_name).write_bytes(json.dumps(purse_payload).encode("utf-8"))
    return inst, purse_name


async def history(db, controller_id="ctrl_a"):
    async with db.get_session_context() as session:
        return await PurseSnapshotRepository(session).get_history_for_controller(controller_id)


# ===========================================================================
# 3. harvest_instance_purses — observation-only, idempotent, fail-closed-to-skip
# ===========================================================================

class TestHarvest:
    @pytest.mark.asyncio
    async def test_valid_purse_inserts_snapshot_with_hand_computed_metrics(self, tmp_path, db):
        payload = comprehensive_purse()
        inst, _ = build_instance(tmp_path, purse_payload=payload)

        outcomes = await harvest_instance_purses(
            str(inst), db_manager=db, source_instance_name="inst1", bot_name=None
        )

        assert [o["decision"] for o in outcomes] == ["harvested"]
        rows = await history(db)
        assert len(rows) == 1
        row = rows[0]
        assert row.controller_id == "ctrl_a"
        assert row.source_instance_name == "inst1"
        assert row.sequence == payload["sequence"] == 5
        # The stored derived metrics match the hand-computed contract arithmetic.
        exp = expected_comprehensive_metrics()
        assert Decimal(row.derived_contributed) == exp["contributed"]
        assert Decimal(row.derived_withdrawn) == exp["withdrawn"]
        assert Decimal(row.derived_earned_realized) == exp["earned_realized"]
        assert Decimal(row.derived_earned_total) == exp["earned_total"]
        assert Decimal(row.derived_unrealized) == exp["unrealized"]
        assert Decimal(row.derived_drift) == exp["drift"]
        assert Decimal(row.reference_price_used) == exp["reference_price_used"]
        assert row.opening_basis_quality == "reconstructed"
        # The raw journal is preserved BYTE-faithfully (survives the archive rmtree).
        # CDX-R06: assert byte identity, not just semantic (json.loads ==) equivalence —
        # a reserialization (reordered/reformatted JSON) would keep json.loads equal while
        # breaking the hash provenance. The load-bearing invariant is that the stored text
        # hashes to the stored sha256 (which was taken over the exact source bytes): this
        # fails if records_json is anything but the original bytes.
        source_bytes = json.dumps(payload).encode("utf-8")   # exactly what build_instance wrote
        assert row.records_json.encode("utf-8") == source_bytes
        assert hashlib.sha256(row.records_json.encode("utf-8")).hexdigest() == row.purse_sha256
        # ...and still semantically the same document.
        assert json.loads(row.records_json) == payload

    @pytest.mark.asyncio
    async def test_reharvest_identical_purse_is_idempotent(self, tmp_path, db):
        inst, _ = build_instance(tmp_path, purse_payload=comprehensive_purse())
        first = await harvest_instance_purses(str(inst), db_manager=db, source_instance_name="inst1")
        second = await harvest_instance_purses(str(inst), db_manager=db, source_instance_name="inst1")

        assert [o["decision"] for o in first] == ["harvested"]
        assert [o["decision"] for o in second] == ["skipped_duplicate"]
        rows = await history(db)
        assert len(rows) == 1  # exactly one row, not two

    @pytest.mark.asyncio
    async def test_invalid_purse_skips_without_row_and_without_raising(self, tmp_path, db):
        """A doubtful money journal is NOT mirrored (structured skip), and — unlike the
        copy-forward — harvest never raises: observation, not control. Here the journal's
        controller_id is foreign to the staged config, so the P2 envelope rejects it."""
        bad = comprehensive_purse(controller_id="ctrl_FOREIGN")  # config id is ctrl_a
        inst, _ = build_instance(tmp_path, controller_id="ctrl_a", purse_payload=bad)

        outcomes = await harvest_instance_purses(
            str(inst), db_manager=db, source_instance_name="inst1"
        )

        assert [o["decision"] for o in outcomes] == ["invalid"]
        assert await history(db) == []  # nothing harvested

    @pytest.mark.asyncio
    async def test_terminal_drift_reanchor_snapshot_is_skipped_as_ambiguous(self, tmp_path, db):
        """CDX-R03: a contract-VALID journal whose newest owned-bearing record is a
        ``drift``-classified reanchor (the engine's observation-only carried-prune record:
        owned unchanged, new_owned_*=0) cannot have its current owned authoritatively
        derived from the journal alone. Harvesting it with the naive ``owned = new_owned``
        rule would report zero equity and a large negative earned_total. The harvest must
        SKIP it (structured, observation-only) rather than mirror a misleading balance —
        and must NOT insert a row. Reverting resolve_current_state to overwrite owned from
        every reanchor makes this insert a (wrong) row instead of skipping."""
        records = [
            opening_epoch_record(
                seq=1, epoch_id="epoch-1", owned_quote="100", owned_base="0",
                reference_price="150", contributed_opening_quote="100", earned_opening_quote="0",
            ),
            checkpoint_record(
                seq=2, epoch_id="epoch-1", owned_quote="100", owned_base="0", reference_price="150",
            ),
            reanchor_record(
                seq=3, epoch_id="epoch-1", old_owned_quote="10", new_owned_quote="0",
                old_owned_base="0", new_owned_base="0", classification="drift",
                overclaim_quote="10", wallet_quote_total="0", wallet_base_total="0",
            ),
        ]
        payload = valid_purse_payload(controller_id="ctrl_a", records=records)
        inst, _ = build_instance(tmp_path, controller_id="ctrl_a", purse_payload=payload)

        outcomes = await harvest_instance_purses(
            str(inst), db_manager=db, source_instance_name="inst1"
        )

        assert [o["decision"] for o in outcomes] == ["skipped_ambiguous_owned"]
        assert await history(db) == []  # nothing mirrored — no misleading row

    @pytest.mark.asyncio
    async def test_grown_purse_inserts_a_second_row(self, tmp_path, db):
        """A journal that grew (a new record → new sha + higher sequence) is a NEW
        snapshot, not a duplicate: idempotency is keyed on content, not controller."""
        inst, purse_name = build_instance(tmp_path, purse_payload=comprehensive_purse())
        await harvest_instance_purses(str(inst), db_manager=db, source_instance_name="inst1")

        # Append one more record → new content, sequence 6.
        grown = comprehensive_purse()
        grown["records"].append(
            checkpoint_record(seq=6, ts=1700000600.0, epoch_id="epoch-1",
                              owned_quote="255", owned_base="1.3", reference_price="165")
        )
        grown["sequence"] = 6
        (inst / "data" / purse_name).write_bytes(json.dumps(grown).encode("utf-8"))

        outcomes = await harvest_instance_purses(str(inst), db_manager=db, source_instance_name="inst1")
        assert [o["decision"] for o in outcomes] == ["harvested"]
        rows = await history(db)
        assert len(rows) == 2
        assert {r.sequence for r in rows} == {5, 6}

    @pytest.mark.asyncio
    async def test_absent_purse_harvests_nothing(self, tmp_path, db):
        """A controller with no purse journal (pre-purse bot) harvests nothing and does
        not fail — regression guard for the common case."""
        inst, _ = build_instance(tmp_path, purse_payload=None)
        outcomes = await harvest_instance_purses(str(inst), db_manager=db, source_instance_name="inst1")
        assert outcomes == []
        assert await history(db) == []

    @pytest.mark.asyncio
    async def test_missing_instance_dir_is_a_noop(self, tmp_path, db):
        """A nonexistent instance dir (the harvest ran against a bot with no on-disk
        tree) is a clean no-op, never a crash."""
        outcomes = await harvest_instance_purses(
            str(tmp_path / "bots" / "instances" / "gone"),
            db_manager=db, source_instance_name="gone",
        )
        assert outcomes == []


# ===========================================================================
# 4. Endpoints
# ===========================================================================

async def insert_snapshot(db, *, controller_id="ctrl_a", sha, sequence, contributed="1",
                          source_instance_name="inst1"):
    async with db.get_session_context() as session:
        return await PurseSnapshotRepository(session).insert_snapshot_if_absent(
            controller_id=controller_id, source_instance_name=source_instance_name,
            source_bot_run_id=None, purse_sha256=sha, sequence=sequence, records_json="{}",
            derived_contributed=contributed, derived_withdrawn="0", derived_earned_realized="0",
            derived_earned_total="0", derived_unrealized="0", derived_drift="0",
            reference_price_used="150", opening_basis_quality="reconstructed",
        )


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_get_purse_returns_newest_with_provenance(self, db):
        await insert_snapshot(db, sha="sha-old", sequence=1, contributed="100")
        await insert_snapshot(db, sha="sha-new", sequence=2, contributed="200")

        resp = await get_purse("ctrl_a", db_manager=db)

        assert resp.controller_id == "ctrl_a"
        assert resp.provenance.sequence == 2            # newest
        assert resp.provenance.purse_sha256 == "sha-new"
        assert resp.provenance.harvested_at is not None
        assert resp.derived.contributed == "200"
        assert resp.authority_note == AUTHORITY_NOTE     # labeled non-authoritative

    @pytest.mark.asyncio
    async def test_get_purse_404_for_unknown_controller(self, db):
        with pytest.raises(HTTPException) as exc:
            await get_purse("nobody", db_manager=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_history_is_newest_first_and_excludes_records_json(self, db):
        await insert_snapshot(db, sha="sha-1", sequence=1)
        await insert_snapshot(db, sha="sha-2", sequence=2)

        resp = await get_purse_history("ctrl_a", limit=100, offset=0, db_manager=db)

        assert [e.provenance.sequence for e in resp.snapshots] == [2, 1]  # newest first
        # records_json must not leak anywhere in the serialized response.
        assert "records_json" not in json.dumps(resp.model_dump(), default=str)
        assert resp.authority_note == AUTHORITY_NOTE

    @pytest.mark.asyncio
    async def test_history_unknown_controller_is_empty_not_404(self, db):
        resp = await get_purse_history("nobody", limit=100, offset=0, db_manager=db)
        assert resp.snapshots == []


# ===========================================================================
# 4b. Lineage FK — deleting a bot_run must not break on / cascade the snapshot (CDX-R04)
# ===========================================================================

def _fk_enforced_sqlite_sessionmaker():
    """A real sqlite engine with FK enforcement ON (SQLite needs PRAGMA per connection).
    Production is PostgreSQL, which ALWAYS enforces FKs; this reproduces that so the ON
    DELETE behavior is actually exercised — the shared RealSqliteDBManager does NOT enable
    FK enforcement, which is exactly why the other tests here cannot surface this."""
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


class TestBotRunLineageFK:
    def test_deleting_bot_run_nulls_snapshot_lineage_and_is_not_blocked(self):
        """CDX-R04: DELETE /bot-runs/{id} (and archived-bot cleanup) delete bot_runs; on
        PostgreSQL a default RESTRICT/NO ACTION FK would raise ForeignKeyViolation when a
        harvested snapshot references the row (a 500 to the caller, and the row left
        behind). ON DELETE SET NULL makes the delete succeed and the DERIVED snapshot
        survive with a null lineage link — honoring the field's documented 'the run row
        may be gone' contract, and never cascade-deleting the purse history the harvest
        exists to preserve. Reverting the model FK to omit ondelete raises IntegrityError
        on the delete below."""
        Session = _fk_enforced_sqlite_sessionmaker()
        with Session() as s:
            run = BotRun(bot_name="b1", instance_name="b1", strategy_type="controller",
                         strategy_name="range_inventory_ladder", account_name="acct",
                         run_status="STOPPED", deployment_status="ARCHIVED")
            s.add(run)
            s.flush()
            run_id = run.id
            s.add(PurseSnapshot(
                controller_id="ctrl_a", source_instance_name="b1", source_bot_run_id=run_id,
                purse_sha256="sha-1", sequence=1, records_json="{}",
                derived_contributed="0", derived_withdrawn="0", derived_earned_realized="0",
                derived_earned_total="0", derived_unrealized="0", derived_drift="0",
                reference_price_used="150", opening_basis_quality="reconstructed",
            ))
            s.commit()

        # Guard: FK enforcement really IS on (else the assertion below would be vacuous).
        with Session() as s:
            assert s.execute(text("PRAGMA foreign_keys")).scalar() == 1

        # Delete the referenced bot_run — must NOT raise under SET NULL.
        with Session() as s:
            s.delete(s.get(BotRun, run_id))
            s.commit()

        # The derived snapshot survived, with its lineage link nulled out.
        with Session() as s:
            snap = s.execute(
                select(PurseSnapshot).where(PurseSnapshot.controller_id == "ctrl_a")
            ).scalars().one()
            assert snap.source_bot_run_id is None


# ===========================================================================
# 4c. Endpoints THROUGH the real ASGI app — routing + auth wiring (CDX-R05)
# ===========================================================================

@pytest.fixture
def asgi_main():
    """The REAL main app (logfire stubbed, per test_migrations) so the purse router's
    REGISTRATION and AUTH wiring in main.py are actually exercised — the direct-handler
    tests in TestEndpoints cannot see an unregistered route, a wrong prefix, or a dropped
    ``Depends(auth_user)``. dependency_overrides are cleared after each test."""
    import sys
    from unittest.mock import MagicMock
    sys.modules.setdefault("logfire", MagicMock())
    import main
    yield main
    main.app.dependency_overrides.clear()


async def _asgi_get(app, path, *, headers=None):
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


class TestEndpointsASGI:
    @pytest.mark.asyncio
    async def test_registered_authed_path_returns_200_with_provenance(self, db, asgi_main):
        """A real GET /purse/{id} through the app returns 200 with provenance and derived
        metrics, and never leaks records_json. Fails if the router is unregistered or the
        path prefix is wrong (route-level 404), catching the main.py:473 / purse.py:53
        mutations the direct-handler tests cannot."""
        from deps import get_database_manager
        await insert_snapshot(db, sha="sha-1", sequence=7, contributed="123")
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a")
        assert resp.status_code == 200
        body = resp.json()
        assert body["controller_id"] == "ctrl_a"
        assert body["provenance"]["sequence"] == 7
        assert body["provenance"]["purse_sha256"] == "sha-1"
        assert body["derived"]["contributed"] == "123"
        assert body["authority_note"] == AUTHORITY_NOTE
        assert "records_json" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_unauthenticated_request_is_rejected(self, db, asgi_main):
        """The financial read-model MUST be behind auth. With the DB overridden but auth
        NOT, an unauthenticated request is rejected by the real ``Depends(auth_user)`` the
        router is mounted with. Removing that dependency at main.py:473 makes this 401
        become a 404/200 — the mutation this catches."""
        from deps import get_database_manager
        await insert_snapshot(db, sha="sha-1", sequence=1)
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_history_through_asgi_excludes_records_json(self, db, asgi_main):
        from deps import get_database_manager
        await insert_snapshot(db, sha="sha-1", sequence=1)
        await insert_snapshot(db, sha="sha-2", sequence=2)
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a/history")
        assert resp.status_code == 200
        body = resp.json()
        assert [e["provenance"]["sequence"] for e in body["snapshots"]] == [2, 1]
        assert "records_json" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_unknown_controller_404_through_router(self, db, asgi_main):
        from deps import get_database_manager
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/nobody")
        assert resp.status_code == 404


# ===========================================================================
# 5. ORDERING — harvest BEFORE the archive destroys the instance dir (req change #9)
# ===========================================================================

class RmtreeArchiver:
    """An archiver that MOVES/DELETES the instance dir, like the real
    ``archive_locally`` (shutil.move) / ``archive_and_upload`` (rmtree). Records whether
    the dir was still present when archive ran — proving the harvest did not consume it."""

    def __init__(self):
        self.archived = []
        self.dir_present_at_archive = None

    def archive_locally(self, name, instance_dir):
        self.dir_present_at_archive = os.path.isdir(instance_dir)
        self.archived.append(name)
        shutil.rmtree(instance_dir)  # the destructive step the harvest must precede

    def archive_and_upload(self, name, instance_dir, bucket_name=None):
        self.archive_locally(name, instance_dir)


class TestHarvestOrdering:
    @pytest.mark.asyncio
    async def test_harvest_runs_before_archive_deletes_the_dir(self, tmp_path, db, monkeypatch):
        """Drive the REAL retirement state machine. The archiver rmtrees the instance
        dir; a snapshot still lands because the harvest ran first. If the harvest were
        reordered after the archive step, the dir would be gone and no snapshot would
        exist — this test would fail."""
        monkeypatch.chdir(tmp_path)  # instance_dir is 'bots/instances/bot1' (cwd-relative)
        build_instance(tmp_path, name="bot1", controller_id="ctrl_a",
                       purse_payload=comprehensive_purse())
        await seed_bot_run(db, bot_name="bot1", account_name="acct")

        orch = make_orchestrator(db, StubMQTT())
        archiver = RmtreeArchiver()
        # StubDockerManager default exits the container so the FSM reaches the archive.
        from test_retirement_fsm import StubDockerManager
        await run_fsm(orch, StubDockerManager(exit_code=0), archiver)

        # The archive DID run, and the dir was still there when it ran (harvest is
        # read-only, and ran BEFORE archive).
        assert archiver.archived == ["bot1"]
        assert archiver.dir_present_at_archive is True
        assert not os.path.isdir("bots/instances/bot1")  # archive then removed it
        # The inception history was harvested before the rmtree.
        rows = await history(db)
        assert len(rows) == 1
        assert rows[0].sequence == 5
        assert rows[0].source_bot_run_id is not None  # lineage link resolved

        # CDX-R01/R02: this fixture seeds NO order history, so the retirement finalizes
        # UNVERIFIED — yet the purse was STILL harvested. That is by design (required
        # change #9): the archive destroys the data/ dir whether or not the retirement
        # verifies, so the mirror must be captured regardless; a dirty/unverified
        # retirement is precisely when preserving the inception journal matters most.
        run = await fetch_run(db, bot_name="bot1")
        assert run.retirement_status == RETIREMENT_UNVERIFIED

    @pytest.mark.asyncio
    async def test_harvest_runs_on_verified_retirement_too(self, tmp_path, db, monkeypatch):
        """The mirror image of the above: a fully-evidenced VERIFIED retirement also
        harvests (seeding a FILLED order lets the zero-open-orders + fill-drain stages
        confirm). Together the two tests pin harvesting as verification-AGNOSTIC — it
        happens on the verified path AND the unverified path, never gated on the verdict
        (which, being computed only at finalization AFTER the archive, cannot even be
        known at the pre-archive harvest point)."""
        monkeypatch.chdir(tmp_path)
        build_instance(tmp_path, name="bot1", controller_id="ctrl_a",
                       purse_payload=comprehensive_purse())
        await seed_bot_run(db, bot_name="bot1", account_name="acct")
        await seed_order(db, account_name="acct", status="FILLED")  # history + zero active

        orch = make_orchestrator(db, StubMQTT())
        archiver = RmtreeArchiver()
        from test_retirement_fsm import StubDockerManager
        await run_fsm(orch, StubDockerManager(exit_code=0), archiver)

        run = await fetch_run(db, bot_name="bot1")
        assert run.retirement_status == RETIREMENT_VERIFIED
        assert archiver.dir_present_at_archive is True
        rows = await history(db)
        assert len(rows) == 1
        assert rows[0].sequence == 5
