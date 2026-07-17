"""Phase 4 tests: preconditions & guards, fail-closed (design §7).

Covers ``services.resume_service.run_guards``:

  * §7.1 source container running / paused / restarting -> SOURCE_RUNNING abort
  * §7.1 source container exited / NotFound -> pass (dir-on-disk is acceptable)
  * §7.4 destination data/ with a stray ``.json`` / ``.sqlite`` / ``.owner``
    -> DEST_NOT_EMPTY abort; clean (or absent) dest -> pass
  * §7.5 ungraceful source blocked; ``resume_accept_ungraceful=True`` passes with
    a loud warning recorded; missing ``bot_runs`` row treated as ungraceful
  * evaluation order: a running container aborts before the dest/ungraceful guards
  * the partial GuardReport is attached to the raised ResumeError

Docker is mocked via ``MagicMock`` (``containers.get`` returns a fake container or
raises ``docker.errors.NotFound``); the bot-run repo is mocked via ``AsyncMock``;
all filesystem via ``tmp_path``. No real Docker, no DB, no network.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from docker.errors import NotFound

from database.repositories.bot_run_repository import REQUIRED_RETIREMENT_EVIDENCE
from services.resume_service import (
    GuardCheck,
    GuardReport,
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    run_guards,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_source(tmp_path, name="SRC-20260712-230254"):
    inst = tmp_path / "instances" / name
    data = inst / "data"
    data.mkdir(parents=True, exist_ok=True)
    return ResolvedSource(
        instance_name=name, data_dir=data, instance_dir=inst, origin="instances"
    )


def make_dest(tmp_path, name="NEW-20260713-000000", *, create=True):
    """Return the new instance's ``data/`` path; create it empty by default."""
    data = tmp_path / "instances" / name / "data"
    if create:
        data.mkdir(parents=True, exist_ok=True)
    return data


def make_docker(*, status=None, not_found=False):
    """A mock Docker client.

    ``not_found=True`` -> ``containers.get`` raises ``docker.errors.NotFound``;
    otherwise it returns a fake container whose ``.status`` is ``status``.
    """
    client = MagicMock()
    if not_found:
        client.containers.get.side_effect = NotFound("no such container")
    else:
        container = MagicMock()
        container.status = status
        client.containers.get.return_value = container
    return client


def make_repo(rows=None, *, error=None):
    """A mock BotRunRepository. ``rows`` are the ``get_bot_runs`` results (newest
    first); ``error`` makes the call raise (DB unavailable)."""
    repo = MagicMock()
    if error is not None:
        repo.get_bot_runs = AsyncMock(side_effect=error)
    else:
        repo.get_bot_runs = AsyncMock(return_value=list(rows or []))
    return repo


_RETIREMENT_TS = "2026-07-12T23:05:00+00:00"
# Full retirement evidence as the state machine persists it — the guard now
# validates the evidence, not just the VERIFIED marker (CDX-005 / CDX-R04).
VERIFIED_EVIDENCE_JSON = json.dumps({
    **{k: _RETIREMENT_TS for k in REQUIRED_RETIREMENT_EVIDENCE},
    "skip_order_cancellation": False,
    "cancellation_requested_at": _RETIREMENT_TS,
})


def graceful_row(instance_name):
    """A bot_runs row for a clean, graceful stop: STOPPED + end marker +
    VERIFIED retirement with full evidence (CDX-005 — neither STOPPED nor
    the bare marker is trusted)."""
    return SimpleNamespace(
        instance_name=instance_name,
        run_status="STOPPED",
        stopped_at=datetime(2026, 7, 12, 23, 5, 0),
        retirement_status="VERIFIED",
        retirement_evidence=VERIFIED_EVIDENCE_JSON,
    )


def make_dep(accept_ungraceful=False):
    return SimpleNamespace(resume_accept_ungraceful=accept_ungraceful)


def graceful_repo(source):
    """A repo whose latest row for ``source`` is graceful — lets the other guards
    be tested in isolation without the §7.5 guard firing."""
    return make_repo([graceful_row(source.instance_name)])


# ===========================================================================
# §7.1 — source container state
# ===========================================================================

class TestSourceContainerGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["running", "restarting", "paused"])
    async def test_active_container_aborts(self, tmp_path, status):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, dest, make_dep(),
                make_docker(status=status), graceful_repo(source),
            )
        assert exc.value.reason is ResumeAbortReason.SOURCE_RUNNING
        assert "stop the container first" in exc.value.message.lower()

    @pytest.mark.asyncio
    async def test_exited_container_passes(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        report = await run_guards(
            source, dest, make_dep(),
            make_docker(status="exited"), graceful_repo(source),
        )
        assert report.passed
        check = next(c for c in report.checks if c.name == "source_container")
        assert check.passed

    @pytest.mark.asyncio
    async def test_notfound_container_passes(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        report = await run_guards(
            source, dest, make_dep(),
            make_docker(not_found=True), graceful_repo(source),
        )
        assert report.passed
        check = next(c for c in report.checks if c.name == "source_container")
        assert check.passed and "stopped/removed" in check.message

    @pytest.mark.asyncio
    async def test_container_looked_up_by_exact_instance_name(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        docker = make_docker(status="exited")
        await run_guards(source, dest, make_dep(), docker, graceful_repo(source))
        docker.containers.get.assert_called_once_with(source.instance_name)


# ===========================================================================
# §7.4 — destination data/ must be empty of state files
# ===========================================================================

class TestDestinationEmptyGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stray_name",
        [
            "range_inventory_ladder_x.json",
            "trades.sqlite",
            "range_inventory_ladder_x.json.owner",
        ],
    )
    async def test_stray_state_file_aborts(self, tmp_path, stray_name):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        (dest / stray_name).write_text("{}", encoding="utf-8")
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, dest, make_dep(),
                make_docker(status="exited"), graceful_repo(source),
            )
        assert exc.value.reason is ResumeAbortReason.DEST_NOT_EMPTY
        assert stray_name in exc.value.message

    @pytest.mark.asyncio
    async def test_clean_dest_passes(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        report = await run_guards(
            source, dest, make_dep(),
            make_docker(status="exited"), graceful_repo(source),
        )
        assert report.passed
        check = next(c for c in report.checks if c.name == "destination_empty")
        assert check.passed

    @pytest.mark.asyncio
    async def test_absent_dest_treated_as_empty(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path, create=False)
        report = await run_guards(
            source, dest, make_dep(),
            make_docker(status="exited"), graceful_repo(source),
        )
        assert report.passed

    @pytest.mark.asyncio
    async def test_non_state_file_in_dest_is_ignored(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        # A stray log / diagnostic is not a state file — must not trip the guard.
        (dest / "some.log").write_text("x", encoding="utf-8")
        report = await run_guards(
            source, dest, make_dep(),
            make_docker(status="exited"), graceful_repo(source),
        )
        assert report.passed


# ===========================================================================
# §7.5 — ungraceful source
# ===========================================================================

class TestUngracefulSourceGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["RUNNING", "ERROR", "CREATED"])
    async def test_non_stopped_status_blocked(self, tmp_path, status):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        row = SimpleNamespace(
            instance_name=source.instance_name,
            run_status=status,
            stopped_at=datetime(2026, 7, 12, 23, 5, 0),
        )
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, dest, make_dep(),
                make_docker(status="exited"), make_repo([row]),
            )
        assert exc.value.reason is ResumeAbortReason.UNGRACEFUL_SOURCE

    @pytest.mark.asyncio
    async def test_missing_end_marker_blocked(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        row = SimpleNamespace(
            instance_name=source.instance_name,
            run_status="STOPPED",
            stopped_at=None,  # no end marker
        )
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, dest, make_dep(),
                make_docker(status="exited"), make_repo([row]),
            )
        assert exc.value.reason is ResumeAbortReason.UNGRACEFUL_SOURCE

    @pytest.mark.asyncio
    async def test_missing_bot_runs_row_treated_as_ungraceful(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        # Repo has rows, but none for THIS source instance.
        other = SimpleNamespace(
            instance_name="OTHER-20260101-000000",
            run_status="STOPPED",
            stopped_at=datetime(2026, 1, 1),
        )
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, dest, make_dep(),
                make_docker(status="exited"), make_repo([other]),
            )
        assert exc.value.reason is ResumeAbortReason.UNGRACEFUL_SOURCE
        assert "unknown history" in exc.value.message.lower()

    @pytest.mark.asyncio
    async def test_db_error_treated_as_ungraceful(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, dest, make_dep(),
                make_docker(status="exited"),
                make_repo(error=RuntimeError("db down")),
            )
        assert exc.value.reason is ResumeAbortReason.UNGRACEFUL_SOURCE

    @pytest.mark.asyncio
    async def test_graceful_source_passes(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        report = await run_guards(
            source, dest, make_dep(),
            make_docker(status="exited"), graceful_repo(source),
        )
        assert report.passed
        check = next(c for c in report.checks if c.name == "graceful_source")
        assert check.passed and not report.warnings

    @pytest.mark.asyncio
    async def test_override_passes_with_warning_recorded(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        row = SimpleNamespace(
            instance_name=source.instance_name,
            run_status="ERROR",
            stopped_at=None,
        )
        report = await run_guards(
            source, dest, make_dep(accept_ungraceful=True),
            make_docker(status="exited"), make_repo([row]),
        )
        assert report.passed
        # A loud warning is recorded (both as a report warning and on the check).
        assert any("UNGRACEFUL SOURCE ACCEPTED" in w for w in report.warnings)
        check = next(c for c in report.checks if c.name == "graceful_source")
        assert check.passed

    @pytest.mark.asyncio
    async def test_override_on_missing_row_passes_with_warning(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        report = await run_guards(
            source, dest, make_dep(accept_ungraceful=True),
            make_docker(status="exited"), make_repo([]),
        )
        assert report.passed
        assert report.warnings


# ===========================================================================
# Ordering + report shape
# ===========================================================================

class TestGuardOrderingAndReport:
    @pytest.mark.asyncio
    async def test_running_container_aborts_before_dest_and_ungraceful(self, tmp_path):
        """A running container must abort first, even with a dirty dest and no
        bot_runs history — SOURCE_RUNNING wins the §7 ordering."""
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        (dest / "stray.json").write_text("{}", encoding="utf-8")  # would trip §7.4
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, dest, make_dep(),
                make_docker(status="running"), make_repo([]),  # would trip §7.5
            )
        assert exc.value.reason is ResumeAbortReason.SOURCE_RUNNING

    @pytest.mark.asyncio
    async def test_partial_report_attached_to_error(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        with pytest.raises(ResumeError) as exc:
            await run_guards(
                source, dest, make_dep(),
                make_docker(status="running"), graceful_repo(source),
            )
        report = getattr(exc.value, "guard_report", None)
        assert isinstance(report, GuardReport)
        assert not report.passed
        assert any(not c.passed for c in report.checks)

    @pytest.mark.asyncio
    async def test_passing_report_records_all_guards_and_serializes(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        report = await run_guards(
            source, dest, make_dep(),
            make_docker(status="exited"), graceful_repo(source),
        )
        names = {c.name for c in report.checks}
        assert names == {
            "source_container",
            "ledger_validity",
            "destination_empty",
            "graceful_source",
        }
        as_dict = report.to_dict()
        assert as_dict["passed"] is True
        assert len(as_dict["checks"]) == 4
        assert all(isinstance(c, GuardCheck) for c in report.checks)

    @pytest.mark.asyncio
    async def test_ledger_validity_recorded_as_deferred_pass(self, tmp_path):
        source = make_source(tmp_path)
        dest = make_dest(tmp_path)
        report = await run_guards(
            source, dest, make_dep(),
            make_docker(status="exited"), graceful_repo(source),
        )
        check = next(c for c in report.checks if c.name == "ledger_validity")
        assert check.passed and "copy-plan" in check.message
