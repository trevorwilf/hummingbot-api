"""Phase 7 (CDX-005 / CDX-M03): acknowledged-retirement state machine.

Covers:
  * BotRunRepository.finalize_bot_run_retirement — VERIFIED is computable ONLY
    from complete evidence; every postcondition gate is exercised (missing key
    K → UNVERIFIED with K recorded), including the ordering claim that a
    verified-STOPPED row cannot be persisted without the exchange-confirmed
    zero-open-orders evidence.
  * BotsOrchestrator.stop_and_archive_bot — the full state machine against a
    REAL in-memory sqlite DB: happy path (VERIFIED), no stop ack → UNVERIFIED,
    open orders at timeout → UNVERIFIED, dirty container exit → UNVERIFIED,
    no order history → fail-closed UNVERIFIED, archive failure → UNVERIFIED,
    publish failure → run row untouched (no STOPPED write, container untouched),
    skip_order_cancellation recorded + independently verifiable.
  * resume_service._guard_ungraceful_source (via run_guards) — legacy STOPPED
    rows and UNVERIFIED retirements refuse; VERIFIED passes; the pre-existing
    resume_accept_ungraceful human override still works, default refuse.
  * MQTTManager.publish_command_with_ack — publication and acknowledgement are
    reported separately (publish success is never an ack).
  * DockerService.get_container_status — exit_code is actually extracted from
    container attrs (the old getattr-on-a-dict always returned None).

Test authenticity: persistence tests run the REAL repositories against a REAL
sqlite database. The environment has no async sqlite driver (aiosqlite), so a
thin adapter awaits the same calls against a synchronous Session — every SQL
statement, column default and constraint executes for real; only the await
plumbing is adapted. Mocked things are the unavoidable externals: the docker
daemon, the MQTT broker, the archiver. Expected values are derived from the
phase spec (evidence keys, VERIFIED/UNVERIFIED semantics), never from running
the implementation.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, BotRun, Order
from database.repositories.bot_run_repository import (
    REQUIRED_RETIREMENT_EVIDENCE,
    RETIREMENT_UNVERIFIED,
    RETIREMENT_VERIFIED,
    BotRunRepository,
    missing_retirement_evidence,
)
from services.bots_orchestrator import BotsOrchestrator, RetirementTimeouts
from services.docker_service import DockerService
from services.resume_service import (
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    run_guards,
)
from utils.mqtt_manager import MQTTManager


# ---------------------------------------------------------------------------
# Real-DB plumbing (sync sqlite driver adapted to the async session surface)
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


async def seed_bot_run(db, bot_name="bot1", account_name="acct", run_status=None):
    async with db.get_session_context() as session:
        run = await BotRunRepository(session).create_bot_run(
            bot_name=bot_name,
            instance_name=bot_name,
            strategy_type="controller",
            strategy_name="range_inventory_ladder",
            account_name=account_name,
        )
        if run_status is not None:
            run.run_status = run_status
            await session.flush()
        return run.id


async def seed_order(db, account_name="acct", status="FILLED", client_order_id="ord-1"):
    async with db.get_session_context() as session:
        session.add(
            Order(
                client_order_id=client_order_id,
                account_name=account_name,
                connector_name="nonkyc",
                trading_pair="XMR-USDT",
                trade_type="BUY",
                order_type="LIMIT",
                amount=1,
                status=status,
            )
        )
        await session.flush()


async def fetch_run(db, bot_name="bot1"):
    async with db.get_session_context() as session:
        result = await session.execute(
            select(BotRun).where(BotRun.bot_name == bot_name)
        )
        return result.scalars().first()


def evidence_of(run):
    return json.loads(run.retirement_evidence) if run.retirement_evidence else {}


# ---------------------------------------------------------------------------
# Spec-derived evidence fixtures (timestamps are arbitrary ISO strings; what
# matters is presence/absence — the spec keys, not implementation output)
# ---------------------------------------------------------------------------

TS = "2026-07-16T12:00:00+00:00"


def full_evidence(skip_order_cancellation=False):
    ev = {k: TS for k in REQUIRED_RETIREMENT_EVIDENCE}
    ev["skip_order_cancellation"] = skip_order_cancellation
    if not skip_order_cancellation:
        ev["cancellation_requested_at"] = TS
    return ev


# ===========================================================================
# Repository: the VERIFIED gate
# ===========================================================================

class TestFinalizeRetirementGates:
    @pytest.mark.asyncio
    async def test_full_evidence_persists_verified_stopped(self, db):
        await seed_bot_run(db)
        async with db.get_session_context() as session:
            row = await BotRunRepository(session).finalize_bot_run_retirement(
                "bot1", full_evidence(), final_status={"status": "stopped"}
            )
        assert row.retirement_status == RETIREMENT_VERIFIED
        assert row.run_status == "STOPPED"
        assert row.stopped_at is not None
        assert json.loads(row.final_status) == {"status": "stopped"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing_key", REQUIRED_RETIREMENT_EVIDENCE)
    async def test_each_postcondition_gate(self, db, missing_key):
        """Missing evidence for ANY stage → the terminal state is UNVERIFIED,
        with the gap recorded for the operator."""
        await seed_bot_run(db)
        ev = full_evidence()
        ev[missing_key] = None
        async with db.get_session_context() as session:
            row = await BotRunRepository(session).finalize_bot_run_retirement("bot1", ev)
        assert row.retirement_status == RETIREMENT_UNVERIFIED
        assert row.run_status == "STOPPED"  # descriptive, but NOT verified
        assert missing_key in json.loads(row.retirement_evidence)["missing_evidence"]

    @pytest.mark.asyncio
    async def test_verified_cannot_precede_zero_open_orders(self, db):
        """Ordering (spec): verified-STOPPED cannot be persisted before the
        exchange-confirmed-zero-open-orders evidence exists."""
        await seed_bot_run(db)
        ev = full_evidence()
        del ev["zero_open_orders_confirmed_at"]
        async with db.get_session_context() as session:
            row = await BotRunRepository(session).finalize_bot_run_retirement("bot1", ev)
        assert row.retirement_status == RETIREMENT_UNVERIFIED
        assert "zero_open_orders_confirmed_at" in json.loads(row.retirement_evidence)["missing_evidence"]

    @pytest.mark.asyncio
    async def test_error_is_never_verified(self, db):
        await seed_bot_run(db)
        async with db.get_session_context() as session:
            row = await BotRunRepository(session).finalize_bot_run_retirement(
                "bot1", full_evidence(), error_message="container removal failed"
            )
        assert row.run_status == "ERROR"
        assert row.retirement_status == RETIREMENT_UNVERIFIED

    @pytest.mark.asyncio
    async def test_cancellation_marker_required_unless_skipped(self, db):
        """skip=False without a cancellation request marker cannot verify;
        skip=True can (its verification rides on the INDEPENDENT
        zero-open-orders confirmation, which stays required)."""
        await seed_bot_run(db)
        not_skipped = full_evidence(skip_order_cancellation=False)
        del not_skipped["cancellation_requested_at"]
        skipped = full_evidence(skip_order_cancellation=True)
        assert "cancellation_requested_at" not in skipped

        assert "cancellation_requested_at" in missing_retirement_evidence(not_skipped)
        assert missing_retirement_evidence(skipped) == []

        async with db.get_session_context() as session:
            row = await BotRunRepository(session).finalize_bot_run_retirement("bot1", not_skipped)
        assert row.retirement_status == RETIREMENT_UNVERIFIED

    @pytest.mark.asyncio
    async def test_new_rows_default_unverified(self, db):
        """Schema: rows never touched by the state machine are UNVERIFIED —
        the same default legacy rows receive when the column is added."""
        await seed_bot_run(db)
        run = await fetch_run(db)
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        assert run.retirement_evidence is None

    @pytest.mark.asyncio
    async def test_progressive_evidence_write_changes_no_status(self, db):
        await seed_bot_run(db, run_status="RUNNING")
        async with db.get_session_context() as session:
            await BotRunRepository(session).update_bot_run_retirement_evidence(
                "bot1", {"initiated_at": TS}
            )
        run = await fetch_run(db)
        assert run.run_status == "RUNNING"
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        assert evidence_of(run)["initiated_at"] == TS


# ===========================================================================
# Orchestrator: the state machine end-to-end (real DB, external stubs)
# ===========================================================================

FAST = RetirementTimeouts(
    stop_ack_timeout=0.05,
    zero_open_orders_timeout=0.1,
    fill_drain_seconds=0.01,
    poll_interval=0.01,
)


class StubMQTT:
    """The MQTT broker/bot boundary — the unavoidable external."""

    def __init__(self, published=True, response=None):
        self.published = published
        self.response = response
        self.commands = []

    async def publish_command_with_ack(self, bot_id, command, data, timeout=30.0, qos=1):
        self.commands.append((bot_id, command, dict(data)))
        return {"published": self.published, "response": self.response}

    def clear_bot_controller_reports(self, bot_id):
        pass

    def get_bot_controller_reports(self, bot_id):
        return {}

    def get_bot_logs(self, bot_id):
        return []

    def get_bot_error_logs(self, bot_id):
        return []

    def get_discovered_bots(self, timeout_seconds=30):
        return []

    def clear_bot_data(self, bot_id):
        pass


class StubDockerManager:
    """The docker daemon boundary — the unavoidable external."""

    def __init__(self, exit_code=0, remove_success=True):
        self.exit_code = exit_code
        self.remove_success = remove_success
        self.stop_calls = []
        self.remove_calls = []

    def stop_container(self, name):
        self.stop_calls.append(name)

    def get_container_status(self, name):
        return {
            "success": True,
            "state": {"status": "exited", "running": False, "exit_code": self.exit_code},
        }

    def remove_container(self, name, force=True):
        self.remove_calls.append((name, force))
        return {"success": self.remove_success, "message": ""}


class StubArchiver:
    def __init__(self, fail=False):
        self.fail = fail
        self.archived = []

    def archive_locally(self, name, instance_dir):
        if self.fail:
            raise RuntimeError("disk full")
        self.archived.append(name)

    def archive_and_upload(self, name, instance_dir, bucket_name=None):
        self.archive_locally(name, instance_dir)


def make_orchestrator(db, mqtt):
    with patch("docker.from_env", return_value=MagicMock()):
        orch = BotsOrchestrator(
            broker_host="localhost",
            broker_port=1883,
            broker_username="u",
            broker_password="p",
            db_manager=db,
        )
    orch.mqtt_manager = mqtt
    orch.active_bots["bot1"] = {"bot_name": "bot1", "status": "connected", "source": "docker"}
    return orch


async def run_fsm(orch, docker_mgr, archiver, skip_order_cancellation=False, timeouts=FAST):
    await orch.stop_and_archive_bot(
        bot_name="bot1",
        container_name="bot1",
        bot_name_for_orchestrator="bot1",
        skip_order_cancellation=skip_order_cancellation,
        archive_locally=True,
        s3_bucket=None,
        docker_manager=docker_mgr,
        bot_archiver=archiver,
        retirement_timeouts=timeouts,
    )


class TestStopAndArchiveStateMachine:
    @pytest.mark.asyncio
    async def test_happy_path_persists_verified(self, db):
        """All postconditions confirmable → STOPPED + ARCHIVED + VERIFIED with
        every evidence stage stamped."""
        await seed_bot_run(db)
        await seed_order(db, status="FILLED")  # history exists, zero active
        mqtt = StubMQTT(response={"status": 200, "msg": ""})
        orch = make_orchestrator(db, mqtt)
        dockermgr = StubDockerManager(exit_code=0)
        archiver = StubArchiver()

        await run_fsm(orch, dockermgr, archiver, skip_order_cancellation=False)

        run = await fetch_run(db)
        assert run.run_status == "STOPPED"
        assert run.deployment_status == "ARCHIVED"
        assert run.retirement_status == RETIREMENT_VERIFIED
        ev = evidence_of(run)
        for key in REQUIRED_RETIREMENT_EVIDENCE:
            assert ev.get(key) is not None, f"evidence {key} missing on the happy path"
        assert ev["cancellation_requested_at"] is not None
        assert ev["skip_order_cancellation"] is False
        assert ev["container_exit_code"] == 0
        # The stop command actually carried the cancellation flag.
        assert mqtt.commands == [("bot1", "stop", {"skip_order_cancellation": False, "async_backend": True})]
        assert archiver.archived == ["bot1"]

    @pytest.mark.asyncio
    async def test_no_stop_ack_is_unverified(self, db):
        """Published but never acknowledged: publish success is not evidence
        (CDX-005) — the run must finalize UNVERIFIED."""
        await seed_bot_run(db)
        await seed_order(db, status="FILLED")
        orch = make_orchestrator(db, StubMQTT(response=None))  # broker took it, bot silent

        await run_fsm(orch, StubDockerManager(), StubArchiver())

        run = await fetch_run(db)
        assert run.run_status == "STOPPED"
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        ev = evidence_of(run)
        assert ev.get("stop_requested_at") is not None
        assert ev.get("stop_ack_at") is None
        assert "stop_ack_at" in ev["missing_evidence"]

    @pytest.mark.asyncio
    async def test_open_orders_timeout_is_unverified(self, db):
        """Active orders that never clear within the bounded poll → the
        zero-open-orders stage stays unconfirmed → UNVERIFIED, with the
        remaining count recorded. Archival still proceeds (spec: allowed,
        just never verified)."""
        await seed_bot_run(db)
        await seed_order(db, status="OPEN")
        orch = make_orchestrator(db, StubMQTT(response={"status": 200}))

        await run_fsm(orch, StubDockerManager(), StubArchiver())

        run = await fetch_run(db)
        assert run.deployment_status == "ARCHIVED"
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        ev = evidence_of(run)
        assert ev.get("zero_open_orders_confirmed_at") is None
        assert ev["zero_open_orders_basis"] == "timeout"
        assert ev["open_orders_remaining"] == 1
        assert "zero_open_orders_confirmed_at" in ev["missing_evidence"]

    @pytest.mark.asyncio
    async def test_no_order_history_fails_closed(self, db):
        """An account with NO recorded orders proves nothing (a disconnected
        recorder looks identical) — silence is never read as zero."""
        await seed_bot_run(db)  # no orders seeded at all
        orch = make_orchestrator(db, StubMQTT(response={"status": 200}))

        await run_fsm(orch, StubDockerManager(), StubArchiver())

        run = await fetch_run(db)
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        ev = evidence_of(run)
        assert ev.get("zero_open_orders_confirmed_at") is None
        assert ev["zero_open_orders_basis"] == "no_order_history"

    @pytest.mark.asyncio
    async def test_dirty_container_exit_is_unverified(self, db):
        """Exit code != 0: the engine's graceful shutdown (which flushes
        state) did not complete — state_flush stays unconfirmed."""
        await seed_bot_run(db)
        await seed_order(db, status="FILLED")
        orch = make_orchestrator(db, StubMQTT(response={"status": 200}))

        await run_fsm(orch, StubDockerManager(exit_code=137), StubArchiver())

        run = await fetch_run(db)
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        ev = evidence_of(run)
        assert ev.get("process_exited_at") is not None
        assert ev["container_exit_code"] == 137
        assert ev.get("state_flushed_at") is None
        assert "state_flushed_at" in ev["missing_evidence"]

    @pytest.mark.asyncio
    async def test_publish_failure_leaves_row_untouched(self, db):
        """The stop request never reached the broker: the bot may still be
        trading. No STOPPED write, no container stop — fully fail-closed.
        (Mutation this catches: restoring the old write-STOPPED-before-stop
        at the top of stop_and_archive_bot.)"""
        await seed_bot_run(db, run_status="RUNNING")
        orch = make_orchestrator(db, StubMQTT(published=False))
        dockermgr = StubDockerManager()

        await run_fsm(orch, dockermgr, StubArchiver())

        run = await fetch_run(db)
        assert run.run_status == "RUNNING"  # untouched
        assert run.stopped_at is None
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        assert dockermgr.stop_calls == []  # container never touched
        assert evidence_of(run)["failure"].startswith("stop command could not be published")

    @pytest.mark.asyncio
    async def test_skip_order_cancellation_recorded_and_independently_verifiable(self, db):
        """skip=True is recorded in the evidence; verification is still
        possible because zero-open-orders is confirmed INDEPENDENTLY from the
        API's own order records (spec §3)."""
        await seed_bot_run(db)
        await seed_order(db, status="CANCELLED")
        mqtt = StubMQTT(response={"status": 200})
        orch = make_orchestrator(db, mqtt)

        await run_fsm(orch, StubDockerManager(), StubArchiver(), skip_order_cancellation=True)

        run = await fetch_run(db)
        assert run.retirement_status == RETIREMENT_VERIFIED
        ev = evidence_of(run)
        assert ev["skip_order_cancellation"] is True
        assert ev.get("cancellation_requested_at") is None  # never requested
        assert ev.get("zero_open_orders_confirmed_at") is not None
        assert mqtt.commands[0][2]["skip_order_cancellation"] is True

    @pytest.mark.asyncio
    async def test_archive_failure_is_unverified(self, db):
        await seed_bot_run(db)
        await seed_order(db, status="FILLED")
        orch = make_orchestrator(db, StubMQTT(response={"status": 200}))

        await run_fsm(orch, StubDockerManager(), StubArchiver(fail=True))

        run = await fetch_run(db)
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        ev = evidence_of(run)
        assert ev.get("archived_at") is None
        assert "archived_at" in ev["missing_evidence"]
        assert "disk full" in ev["archive_error"]

    @pytest.mark.asyncio
    async def test_remove_failure_finalizes_error_unverified(self, db):
        await seed_bot_run(db)
        await seed_order(db, status="FILLED")
        orch = make_orchestrator(db, StubMQTT(response={"status": 200}))

        await run_fsm(orch, StubDockerManager(remove_success=False), StubArchiver())

        run = await fetch_run(db)
        assert run.run_status == "ERROR"
        assert run.retirement_status == RETIREMENT_UNVERIFIED
        assert run.deployment_status == "DEPLOYED"  # never archived

    @pytest.mark.asyncio
    async def test_serialized_run_exposes_retirement_fields(self, db):
        await seed_bot_run(db)
        await seed_order(db, status="FILLED")
        orch = make_orchestrator(db, StubMQTT(response={"status": 200}))
        await run_fsm(orch, StubDockerManager(), StubArchiver())

        runs = await orch.get_bot_runs(bot_name="bot1")
        assert runs[0]["retirement_status"] == RETIREMENT_VERIFIED
        assert isinstance(runs[0]["retirement_evidence"], dict)
        assert runs[0]["retirement_evidence"]["initiated_at"]


# ===========================================================================
# CFH guard: STOPPED alone is no longer trusted
# ===========================================================================

def make_source(tmp_path, name="SRC-20260712-230254"):
    inst = tmp_path / "instances" / name
    data = inst / "data"
    data.mkdir(parents=True, exist_ok=True)
    return ResolvedSource(instance_name=name, data_dir=data, instance_dir=inst, origin="instances")


def make_dest(tmp_path, name="NEW-20260713-000000"):
    data = tmp_path / "instances" / name / "data"
    data.mkdir(parents=True, exist_ok=True)
    return data


def make_docker_exited():
    client = MagicMock()
    container = MagicMock()
    container.status = "exited"
    client.containers.get.return_value = container
    return client


def make_repo(rows):
    repo = MagicMock()
    repo.get_bot_runs = AsyncMock(return_value=list(rows))
    return repo


def make_dep(accept_ungraceful=False):
    return SimpleNamespace(resume_accept_ungraceful=accept_ungraceful)


class TestCFHRequiresVerifiedRetirement:
    @pytest.mark.asyncio
    async def test_legacy_stopped_row_refused(self, tmp_path):
        """A row predating the evidence schema (no retirement_status at all):
        STOPPED + stopped_at used to pass — it must now refuse (CDX-005)."""
        source = make_source(tmp_path)
        legacy = SimpleNamespace(
            instance_name=source.instance_name,
            run_status="STOPPED",
            stopped_at=datetime(2026, 7, 12, 23, 5, 0),
        )
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, make_dest(tmp_path), make_dep(),
                make_docker_exited(), make_repo([legacy]),
            )
        assert exc.value.reason is ResumeAbortReason.UNGRACEFUL_SOURCE
        assert "unverified" in exc.value.message.lower()

    @pytest.mark.asyncio
    async def test_unverified_retirement_refused(self, tmp_path):
        source = make_source(tmp_path)
        row = SimpleNamespace(
            instance_name=source.instance_name,
            run_status="STOPPED",
            stopped_at=datetime(2026, 7, 12, 23, 5, 0),
            retirement_status=RETIREMENT_UNVERIFIED,
        )
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, make_dest(tmp_path), make_dep(),
                make_docker_exited(), make_repo([row]),
            )
        assert exc.value.reason is ResumeAbortReason.UNGRACEFUL_SOURCE

    @pytest.mark.asyncio
    async def test_verified_retirement_passes(self, tmp_path):
        source = make_source(tmp_path)
        row = SimpleNamespace(
            instance_name=source.instance_name,
            run_status="STOPPED",
            stopped_at=datetime(2026, 7, 12, 23, 5, 0),
            retirement_status=RETIREMENT_VERIFIED,
        )
        report = await run_guards(
            source, make_dest(tmp_path), make_dep(),
            make_docker_exited(), make_repo([row]),
        )
        assert report.passed
        check = next(c for c in report.checks if c.name == "graceful_source")
        assert check.passed and not report.warnings

    @pytest.mark.asyncio
    async def test_human_override_still_works_with_loud_warning(self, tmp_path):
        """The pre-existing explicit override extends to the retirement
        refusal — default refuse, opt-in accept, loudly recorded."""
        source = make_source(tmp_path)
        legacy = SimpleNamespace(
            instance_name=source.instance_name,
            run_status="STOPPED",
            stopped_at=datetime(2026, 7, 12, 23, 5, 0),
        )
        report = await run_guards(
            source, make_dest(tmp_path), make_dep(accept_ungraceful=True),
            make_docker_exited(), make_repo([legacy]),
        )
        assert report.passed
        assert any("UNGRACEFUL SOURCE ACCEPTED" in w for w in report.warnings)


# ===========================================================================
# MQTT: publication vs acknowledgement
# ===========================================================================

class TestPublishCommandWithAck:
    def _manager(self, connected=True, publish_side_effect=None):
        m = MQTTManager(host="localhost", port=1883, username="u", password="p")
        m._connected = connected
        client = MagicMock()
        client.publish = AsyncMock(side_effect=publish_side_effect)
        m._client = client if connected else None
        return m

    @pytest.mark.asyncio
    async def test_not_connected_reports_unpublished(self):
        m = self._manager(connected=False)
        result = await m.publish_command_with_ack("bot1", "stop", {}, timeout=0.01)
        assert result == {"published": False, "response": None}

    @pytest.mark.asyncio
    async def test_publish_error_reports_unpublished(self):
        m = self._manager(publish_side_effect=Exception("broker down"))
        result = await m.publish_command_with_ack("bot1", "stop", {}, timeout=0.01)
        assert result == {"published": False, "response": None}
        assert m._pending_responses == {}

    @pytest.mark.asyncio
    async def test_published_without_ack_is_not_an_ack(self):
        """The broker accepted the publish but the bot never answered: the
        result must say so explicitly — this is the CDX-005 distinction."""
        m = self._manager()
        result = await m.publish_command_with_ack("bot1", "stop", {}, timeout=0.01)
        assert result["published"] is True
        assert result["response"] is None
        assert m._pending_responses == {}  # future cleaned up

    @pytest.mark.asyncio
    async def test_bot_response_returned(self):
        m = self._manager()

        async def answer():
            for _ in range(100):
                if m._pending_responses:
                    topic = next(iter(m._pending_responses))
                    m._pending_responses[topic].set_result({"status": 200})
                    return
                await asyncio.sleep(0.005)

        task = asyncio.ensure_future(answer())
        result = await m.publish_command_with_ack("bot1", "stop", {}, timeout=2.0)
        await task
        assert result == {"published": True, "response": {"status": 200}}


# ===========================================================================
# DockerService: exit codes must be observable
# ===========================================================================

class TestContainerExitCode:
    def _service(self, exit_code):
        service = DockerService.__new__(DockerService)
        container = MagicMock()
        container.status = "exited"
        container.attrs = {"State": {"ExitCode": exit_code}}
        service.client = MagicMock()
        service.client.containers.get.return_value = container
        return service

    def test_exit_code_extracted(self):
        """attrs["State"] is a dict; the old getattr() on it ALWAYS returned
        None, which would make state-flush evidence impossible to confirm."""
        status = self._service(0).get_container_status("bot1")
        assert status["state"]["exit_code"] == 0

    def test_nonzero_exit_code_extracted(self):
        status = self._service(137).get_container_status("bot1")
        assert status["state"]["exit_code"] == 137
