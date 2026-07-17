"""Phase 6 tests: router plumbing + resume-preview endpoint (design §5 R6, §9).

Coverage:
  * deploy_v2_controllers: resume fields thread through to create_hummingbot_instance
  * deploy_v2_controllers: ResumeError -> HTTP 409 {reason, detail}
  * deploy_v2_controllers without resume fields -> unchanged behavior
  * deploy_v2_script: ResumeError -> HTTP 409
  * preview happy path (mocked service)
  * preview performs zero writes to the bot tree (integration)
  * 409 + reason on each ResumeAbortReason (parametrized)
  * 422 on bad models (FastAPI / Pydantic validation)

NOTE: routers.bot_orchestration imports deps.py which imports
``unified_connector_service`` which imports ``hummingbot.connector.gateway.gateway``
— a module absent on this host. A MagicMock stub is installed in sys.modules
at collection time (below) so that the router can be imported in test bodies.
The stub is a no-op if the real module is already loaded.

No real Docker, no DB, no network, no containers started.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Stub hummingbot.connector.gateway.gateway so that deps.py (which imports
# UnifiedConnectorService which imports Gateway) can be imported in test
# bodies.  The stub is installed once at module-level, before any test runs.
# It is a no-op if the module is already present (i.e. if some other test has
# already successfully loaded it).
# ---------------------------------------------------------------------------
_GATEWAY_MOD = "hummingbot.connector.gateway.gateway"
if _GATEWAY_MOD not in sys.modules:
    sys.modules[_GATEWAY_MOD] = MagicMock()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Module-level imports that are SAFE (no hummingbot chain)
# ---------------------------------------------------------------------------

from database.repositories.bot_run_repository import REQUIRED_RETIREMENT_EVIDENCE
from ledger_fixtures import valid_ledger_payload
from services.resume_service import (
    CopyPlan,
    GuardReport,
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
)

_RETIREMENT_TS = "2026-07-10T12:00:00+00:00"
# CDX-005/CDX-R04: the graceful-source guard validates the FULL retirement
# evidence, not just the VERIFIED marker.
_VERIFIED_EVIDENCE_JSON = json.dumps({
    **{k: _RETIREMENT_TS for k in REQUIRED_RETIREMENT_EVIDENCE},
    "skip_order_cancellation": False,
    "cancellation_requested_at": _RETIREMENT_TS,
})


# ---------------------------------------------------------------------------
# Lazy app factory (avoids module-level import of routers/deps)
# ---------------------------------------------------------------------------

def _make_app(mock_docker_manager, mock_bots_manager=None):
    """Create a minimal FastAPI app with bot-orchestration router and mocked deps.
    Import of router/deps is deferred to here so pytest collection succeeds.
    """
    from fastapi import FastAPI
    from deps import get_bots_orchestrator, get_docker_service
    from routers.bot_orchestration import router

    if mock_bots_manager is None:
        mock_bots_manager = _default_bots_manager()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_docker_service] = lambda: mock_docker_manager
    app.dependency_overrides[get_bots_orchestrator] = lambda: mock_bots_manager
    return app


def _default_docker_manager():
    mgr = MagicMock()
    mgr.db_manager = None
    mgr.client = MagicMock()
    mgr.create_hummingbot_instance = AsyncMock(
        return_value={"success": True, "message": "ok"}
    )
    return mgr


def _default_bots_manager():
    mgr = MagicMock()
    mgr.create_bot_run = AsyncMock(return_value=None)
    return mgr


def _test_client(mock_docker_manager, mock_bots_manager=None):
    from fastapi.testclient import TestClient
    return TestClient(_make_app(mock_docker_manager, mock_bots_manager))


# ---------------------------------------------------------------------------
# Shared request payloads
# ---------------------------------------------------------------------------

CTRL_BASE = {
    "instance_name": "TEST_BOT",
    "credentials_profile": "master_account",
    "controllers_config": ["ctrl_xmr"],
    "image": "hummingbot/hummingbot:latest",
}
CTRL_RESUME = {
    **CTRL_BASE,
    "resume_mode": "explicit",
    "resume_from": "TEST_BOT-20260710-101010",
}

SCRIPT_BASE = {
    "instance_name": "SCRIPT_BOT",
    "credentials_profile": "master_account",
    "image": "hummingbot/hummingbot:latest",
}
SCRIPT_RESUME = {
    **SCRIPT_BASE,
    "resume_mode": "explicit",
    "resume_from": "SCRIPT_BOT-20260710-101010",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_resolved_source(tmp_path: Path, name="SRC-20260710-101010") -> ResolvedSource:
    inst = tmp_path / "instances" / name
    data = inst / "data"
    data.mkdir(parents=True, exist_ok=True)
    return ResolvedSource(
        instance_name=name, data_dir=data, instance_dir=inst, origin="instances"
    )


def _empty_guard_report() -> GuardReport:
    rep = GuardReport()
    rep._record("source_container", True, "ok")
    rep._record("ledger_validity", True, "ok")
    rep._record("destination_empty", True, "ok")
    rep._record("graceful_source", True, "ok")
    return rep


def _preview_result_stub(source: ResolvedSource, guard_report: GuardReport) -> dict:
    return {
        "resolved_source": {
            "instance_name": source.instance_name,
            "data_dir": str(source.data_dir),
            "origin": source.origin,
        },
        "files": [],
        "decisions": {},
        "guard_report": guard_report.to_dict(),
        "would_succeed": True,
    }


# ===========================================================================
# 1.  deploy_v2_controllers: resume fields reach create_hummingbot_instance
# ===========================================================================

class TestDeployResumePlumbing:
    """Resume fields propagate from the request model to create_hummingbot_instance."""

    def test_resume_fields_forwarded(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bots" / "conf" / "scripts").mkdir(parents=True)

        docker_mgr = _default_docker_manager()
        client = _test_client(docker_mgr)

        with patch("utils.file_system.fs_util.dump_dict_to_yaml"):
            resp = client.post("/bot-orchestration/deploy-v2-controllers", json=CTRL_RESUME)

        assert resp.status_code == 200
        docker_mgr.create_hummingbot_instance.assert_called_once()
        config = docker_mgr.create_hummingbot_instance.call_args.args[0]
        assert config.resume_mode == "explicit"
        assert config.resume_from == "TEST_BOT-20260710-101010"
        assert config.resume_from_archive is False
        assert config.resume_accept_ungraceful is False

    def test_all_resume_fields_forwarded(self, tmp_path, monkeypatch):
        """All five resume fields (mode, from, archive, extra_paths, ungraceful) thread through."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bots" / "conf" / "scripts").mkdir(parents=True)

        docker_mgr = _default_docker_manager()
        client = _test_client(docker_mgr)
        payload = {
            **CTRL_BASE,
            "resume_mode": "explicit",
            "resume_from": "TEST_BOT-20260710-101010",
            "resume_from_archive": True,
            "resume_extra_paths": ["custom/data.bin"],
            "resume_accept_ungraceful": True,
        }

        with patch("utils.file_system.fs_util.dump_dict_to_yaml"):
            resp = client.post("/bot-orchestration/deploy-v2-controllers", json=payload)

        assert resp.status_code == 200
        config = docker_mgr.create_hummingbot_instance.call_args.args[0]
        assert config.resume_from_archive is True
        assert config.resume_extra_paths == ["custom/data.bin"]
        assert config.resume_accept_ungraceful is True

    def test_no_resume_fields_defaults(self, tmp_path, monkeypatch):
        """Without resume fields the deployment has resume_mode='off'; unchanged behavior."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bots" / "conf" / "scripts").mkdir(parents=True)

        docker_mgr = _default_docker_manager()
        client = _test_client(docker_mgr)

        with patch("utils.file_system.fs_util.dump_dict_to_yaml"):
            resp = client.post("/bot-orchestration/deploy-v2-controllers", json=CTRL_BASE)

        assert resp.status_code == 200
        config = docker_mgr.create_hummingbot_instance.call_args.args[0]
        assert config.resume_mode == "off"
        assert config.resume_from is None
        assert config.resume_from_archive is False
        assert config.resume_extra_paths is None
        assert config.resume_accept_ungraceful is False


# ===========================================================================
# 2.  deploy_v2_controllers: ResumeError -> HTTP 409
# ===========================================================================

class TestDeployResumeError:
    """ResumeError raised inside create_hummingbot_instance is mapped to 409."""

    @pytest.mark.parametrize("reason", list(ResumeAbortReason))
    def test_resume_error_each_reason_409(self, reason, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bots" / "conf" / "scripts").mkdir(parents=True)

        docker_mgr = _default_docker_manager()
        docker_mgr.create_hummingbot_instance = AsyncMock(
            side_effect=ResumeError(reason, f"test: {reason.value}")
        )
        client = _test_client(docker_mgr)

        with patch("utils.file_system.fs_util.dump_dict_to_yaml"):
            resp = client.post("/bot-orchestration/deploy-v2-controllers", json=CTRL_RESUME)

        assert resp.status_code == 409, f"expected 409 for reason {reason}"
        body = resp.json()
        assert body["detail"]["reason"] == reason.value
        assert reason.value in body["detail"]["detail"] or "test:" in body["detail"]["detail"]

    def test_resume_error_response_shape(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bots" / "conf" / "scripts").mkdir(parents=True)

        docker_mgr = _default_docker_manager()
        docker_mgr.create_hummingbot_instance = AsyncMock(
            side_effect=ResumeError(ResumeAbortReason.SOURCE_RUNNING, "stop the container first")
        )
        client = _test_client(docker_mgr)

        with patch("utils.file_system.fs_util.dump_dict_to_yaml"):
            resp = client.post("/bot-orchestration/deploy-v2-controllers", json=CTRL_RESUME)

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert set(detail.keys()) >= {"reason", "detail"}
        assert detail["reason"] == "SOURCE_RUNNING"
        assert "stop the container first" in detail["detail"]


# ===========================================================================
# 3.  deploy_v2_script: ResumeError -> HTTP 409
# ===========================================================================

class TestDeployScriptResumeError:
    def test_script_resume_error_409(self):
        docker_mgr = _default_docker_manager()
        docker_mgr.create_hummingbot_instance = AsyncMock(
            side_effect=ResumeError(ResumeAbortReason.DEST_NOT_EMPTY, "data/ not empty")
        )
        resp = _test_client(docker_mgr).post(
            "/bot-orchestration/deploy-v2-script", json=SCRIPT_RESUME
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "DEST_NOT_EMPTY"

    def test_script_resume_error_all_reasons(self):
        for reason in ResumeAbortReason:
            docker_mgr = _default_docker_manager()
            docker_mgr.create_hummingbot_instance = AsyncMock(
                side_effect=ResumeError(reason, f"test: {reason.value}")
            )
            resp = _test_client(docker_mgr).post(
                "/bot-orchestration/deploy-v2-script", json=SCRIPT_RESUME
            )
            assert resp.status_code == 409, f"expected 409 for {reason}"
            assert resp.json()["detail"]["reason"] == reason.value


# ===========================================================================
# 4.  Model validation: HTTP 422
# ===========================================================================

class TestModelValidation:
    """Bad request bodies produce 422 before reaching any handler."""

    def test_explicit_without_resume_from_422(self):
        resp = _test_client(_default_docker_manager()).post(
            "/bot-orchestration/deploy-v2-controllers",
            json={**CTRL_BASE, "resume_mode": "explicit"},
        )
        assert resp.status_code == 422

    def test_resume_from_while_off_422(self):
        resp = _test_client(_default_docker_manager()).post(
            "/bot-orchestration/deploy-v2-controllers",
            json={**CTRL_BASE, "resume_mode": "off", "resume_from": "some_bot"},
        )
        assert resp.status_code == 422

    def test_missing_required_fields_422(self):
        resp = _test_client(_default_docker_manager()).post(
            "/bot-orchestration/deploy-v2-controllers",
            json={"instance_name": "X"},
        )
        assert resp.status_code == 422

    def test_preview_explicit_without_resume_from_422(self):
        resp = _test_client(_default_docker_manager()).post(
            "/bot-orchestration/deploy-v2-controllers/resume-preview",
            json={**CTRL_BASE, "resume_mode": "explicit"},
        )
        assert resp.status_code == 422

    def test_preview_resume_from_while_off_422(self):
        resp = _test_client(_default_docker_manager()).post(
            "/bot-orchestration/deploy-v2-controllers/resume-preview",
            json={**CTRL_BASE, "resume_from": "some_bot"},
        )
        assert resp.status_code == 422


# ===========================================================================
# 5.  Preview endpoint: happy path (mocked service)
# ===========================================================================

class TestPreviewHappyPath:
    """preview_v2_controllers_resume returns the expected structure on success."""

    def test_preview_returns_expected_keys(self, tmp_path):
        source = _make_resolved_source(tmp_path, "SRC-20260710-101010")
        guard_report = _empty_guard_report()
        stub = _preview_result_stub(source, guard_report)
        stub["files"] = [
            {"src": "/a", "dst": "/b", "kind": "ledger", "size": 42, "sha256": "abc"}
        ]
        stub["decisions"] = {"ctrl_xmr": "copied"}

        with patch("routers.bot_orchestration.preview_resume", AsyncMock(return_value=stub)):
            resp = _test_client(_default_docker_manager()).post(
                "/bot-orchestration/deploy-v2-controllers/resume-preview",
                json=CTRL_RESUME,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["would_succeed"] is True
        assert body["resolved_source"]["instance_name"] == "SRC-20260710-101010"
        assert body["decisions"]["ctrl_xmr"] == "copied"
        assert len(body["files"]) == 1
        assert body["files"][0]["sha256"] == "abc"
        assert "guard_report" in body

    def test_preview_passes_correct_bots_path(self, tmp_path):
        """preview_resume is called with bots_path == Path('bots')."""
        stub = {
            "resolved_source": {"instance_name": "x", "data_dir": "/x", "origin": "instances"},
            "files": [],
            "decisions": {},
            "guard_report": _empty_guard_report().to_dict(),
            "would_succeed": True,
        }
        mock_preview = AsyncMock(return_value=stub)

        with patch("routers.bot_orchestration.preview_resume", mock_preview):
            _test_client(_default_docker_manager()).post(
                "/bot-orchestration/deploy-v2-controllers/resume-preview",
                json=CTRL_RESUME,
            )

        mock_preview.assert_called_once()
        kw = mock_preview.call_args.kwargs
        assert str(kw["bots_path"]) == "bots"
        dep = kw["deployment"]
        assert dep.resume_mode == "explicit"
        assert dep.resume_from == "TEST_BOT-20260710-101010"

    def test_preview_response_keys_complete(self, tmp_path):
        """Response includes all five expected top-level keys."""
        source = _make_resolved_source(tmp_path)
        stub = _preview_result_stub(source, _empty_guard_report())

        with patch("routers.bot_orchestration.preview_resume", AsyncMock(return_value=stub)):
            resp = _test_client(_default_docker_manager()).post(
                "/bot-orchestration/deploy-v2-controllers/resume-preview",
                json=CTRL_RESUME,
            )

        body = resp.json()
        for key in ("resolved_source", "files", "decisions", "guard_report", "would_succeed"):
            assert key in body, f"missing key {key!r} in preview response"


# ===========================================================================
# 6.  Preview endpoint: 409 on guard failures
# ===========================================================================

class TestPreview409:
    """Each ResumeAbortReason raised by preview_resume produces HTTP 409."""

    @pytest.mark.parametrize("reason", list(ResumeAbortReason))
    def test_preview_409_each_reason(self, reason):
        err = ResumeError(reason, f"test: {reason.value}")

        with patch("routers.bot_orchestration.preview_resume", AsyncMock(side_effect=err)):
            resp = _test_client(_default_docker_manager()).post(
                "/bot-orchestration/deploy-v2-controllers/resume-preview",
                json=CTRL_RESUME,
            )

        assert resp.status_code == 409, f"expected 409 for {reason}"
        body = resp.json()
        assert body["detail"]["reason"] == reason.value
        assert "test:" in body["detail"]["detail"]


# ===========================================================================
# 7.  Preview: zero writes to the bot tree (real preview_resume integration)
# ===========================================================================

class TestPreviewNoWrites:
    """preview_resume must not create or modify any file in the bots tree."""

    @pytest.mark.asyncio
    async def test_preview_does_not_write_to_bot_tree(self, tmp_path, monkeypatch):
        """Run real preview_resume on a fabricated bots tree; assert the tree
        is byte-for-byte identical before and after the call."""
        monkeypatch.chdir(tmp_path)
        bots = tmp_path / "bots"

        # Template controller
        ctrl_dir = bots / "conf" / "controllers"
        ctrl_dir.mkdir(parents=True)
        ctrl_cfg = {"controller_name": "range_inventory_ladder", "id": "ctrl_xmr",
                     "connector_name": "nonkyc", "trading_pair": "XMR-USDT"}
        (ctrl_dir / "ctrl_xmr.yml").write_text(yaml.safe_dump(ctrl_cfg), encoding="utf-8")

        # Credentials (for sqlite-mode check)
        creds = bots / "credentials" / "master_account"
        creds.mkdir(parents=True)
        (creds / "conf_client.yml").write_text(
            yaml.safe_dump({"db_mode": {"db_engine": "postgres+asyncpg"}}), encoding="utf-8"
        )

        # Source instance with valid ledger + .owner
        src_dir = bots / "instances" / "SRC-20260710-101010"
        src_data = src_dir / "data"
        src_data.mkdir(parents=True)
        ledger = json.dumps(valid_ledger_payload("ctrl_xmr")).encode()
        (src_data / "range_inventory_ladder_ctrl_xmr.json").write_bytes(ledger)
        (src_data / "range_inventory_ladder_ctrl_xmr.json.owner").write_text(
            json.dumps({"controller_id": "ctrl_xmr"}), encoding="utf-8"
        )
        src_ctrl_dir = src_dir / "conf" / "controllers"
        src_ctrl_dir.mkdir(parents=True)
        (src_ctrl_dir / "ctrl_xmr.yml").write_text(yaml.safe_dump(ctrl_cfg), encoding="utf-8")

        # Docker: container not found -> source guard passes
        from docker.errors import NotFound as DockerNotFound
        docker_client = MagicMock()
        docker_client.containers.get.side_effect = DockerNotFound("x")

        # Graceful bot run (CDX-005: requires VERIFIED retirement)
        fake_run = SimpleNamespace(
            instance_name="SRC-20260710-101010",
            run_status="STOPPED",
            stopped_at="2026-07-10",
            retirement_status="VERIFIED",
            retirement_evidence=_VERIFIED_EVIDENCE_JSON,
        )
        fake_repo = AsyncMock()
        fake_repo.get_bot_runs = AsyncMock(return_value=[fake_run])

        from models import V2ControllerDeployment
        dep = V2ControllerDeployment(
            instance_name="TEST_BOT",
            credentials_profile="master_account",
            controllers_config=["ctrl_xmr"],
            resume_mode="explicit",
            resume_from="SRC-20260710-101010",
        )

        # Snapshot the bots tree before the call
        def _snapshot(root: Path):
            return {
                str(p.relative_to(root)): p.read_bytes()
                for p in sorted(root.rglob("*"))
                if p.is_file()
            }

        before = _snapshot(bots)

        from services.resume_service import preview_resume
        result = await preview_resume(
            deployment=dep,
            bots_path=bots,
            docker_client=docker_client,
            bot_run_repo=fake_repo,
        )

        after = _snapshot(bots)

        assert result["would_succeed"] is True
        assert before == after, (
            "preview_resume modified the bot tree. "
            f"New files: {set(after) - set(before)}. "
            f"Removed files: {set(before) - set(after)}."
        )


# ===========================================================================
# 8.  preview_resume service unit tests (direct, no router)
# ===========================================================================

class TestPreviewResumeService:
    """Direct unit tests for services.resume_service.preview_resume."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_correct_structure(self, tmp_path, monkeypatch):
        """preview_resume stages templates and returns resolved_source / files /
        decisions / guard_report / would_succeed with sha256 on each file."""
        monkeypatch.chdir(tmp_path)
        bots = tmp_path / "bots"

        ctrl_dir = bots / "conf" / "controllers"
        ctrl_dir.mkdir(parents=True)
        ctrl_cfg = {"controller_name": "range_inventory_ladder", "id": "ctrl_xmr",
                     "connector_name": "nonkyc", "trading_pair": "XMR-USDT"}
        (ctrl_dir / "ctrl_xmr.yml").write_text(yaml.safe_dump(ctrl_cfg), encoding="utf-8")

        creds = bots / "credentials" / "master_account"
        creds.mkdir(parents=True)
        (creds / "conf_client.yml").write_text(
            yaml.safe_dump({"db_mode": {"db_engine": "postgres+asyncpg"}}), encoding="utf-8"
        )

        src_dir = bots / "instances" / "SRC-20260710-101010"
        src_data = src_dir / "data"
        src_data.mkdir(parents=True)
        ledger_bytes = json.dumps(valid_ledger_payload("ctrl_xmr")).encode()
        (src_data / "range_inventory_ladder_ctrl_xmr.json").write_bytes(ledger_bytes)
        (src_data / "range_inventory_ladder_ctrl_xmr.json.owner").write_text(
            json.dumps({"controller_id": "ctrl_xmr"}), encoding="utf-8"
        )
        src_ctrl_dir = src_dir / "conf" / "controllers"
        src_ctrl_dir.mkdir(parents=True)
        (src_ctrl_dir / "ctrl_xmr.yml").write_text(yaml.safe_dump(ctrl_cfg), encoding="utf-8")

        from docker.errors import NotFound as DockerNotFound
        docker_client = MagicMock()
        docker_client.containers.get.side_effect = DockerNotFound("x")

        fake_run = SimpleNamespace(
            instance_name="SRC-20260710-101010", run_status="STOPPED", stopped_at="2026-07-10",
            retirement_status="VERIFIED",  # CDX-005
            retirement_evidence=_VERIFIED_EVIDENCE_JSON,
        )
        fake_repo = AsyncMock()
        fake_repo.get_bot_runs = AsyncMock(return_value=[fake_run])

        from models import V2ControllerDeployment
        dep = V2ControllerDeployment(
            instance_name="TEST_BOT",
            credentials_profile="master_account",
            controllers_config=["ctrl_xmr"],
            resume_mode="explicit",
            resume_from="SRC-20260710-101010",
        )

        from services.resume_service import preview_resume
        result = await preview_resume(
            deployment=dep, bots_path=bots, docker_client=docker_client, bot_run_repo=fake_repo
        )

        assert result["would_succeed"] is True
        assert result["resolved_source"]["instance_name"] == "SRC-20260710-101010"
        assert result["resolved_source"]["origin"] == "instances"
        assert result["decisions"]["ctrl_xmr"] == "copied"
        kinds = {f["kind"] for f in result["files"]}
        assert "ledger" in kinds
        assert "owner" in kinds
        for f in result["files"]:
            assert "sha256" in f
            assert "size" in f

    @pytest.mark.asyncio
    async def test_raises_resume_error_on_running_container(self, tmp_path, monkeypatch):
        """A running source container causes preview_resume to raise ResumeError."""
        monkeypatch.chdir(tmp_path)
        bots = tmp_path / "bots"
        ctrl_dir = bots / "conf" / "controllers"
        ctrl_dir.mkdir(parents=True)
        (ctrl_dir / "ctrl_xmr.yml").write_text(
            yaml.safe_dump({"controller_name": "range_inventory_ladder", "id": "ctrl_xmr",
                     "connector_name": "nonkyc", "trading_pair": "XMR-USDT"}),
            encoding="utf-8",
        )

        src_data = bots / "instances" / "SRC-20260710-101010" / "data"
        src_data.mkdir(parents=True)
        (src_data / "range_inventory_ladder_ctrl_xmr.json").write_bytes(
            json.dumps({"seq": 1}).encode()
        )

        running_container = MagicMock()
        running_container.status = "running"
        docker_client = MagicMock()
        docker_client.containers.get.return_value = running_container

        from models import V2ControllerDeployment
        dep = V2ControllerDeployment(
            instance_name="TEST_BOT",
            credentials_profile="master_account",
            controllers_config=["ctrl_xmr"],
            resume_mode="explicit",
            resume_from="SRC-20260710-101010",
        )

        from services.resume_service import preview_resume
        with pytest.raises(ResumeError) as exc_info:
            await preview_resume(
                deployment=dep,
                bots_path=bots,
                docker_client=docker_client,
                bot_run_repo=AsyncMock(get_bot_runs=AsyncMock(return_value=[])),
            )

        assert exc_info.value.reason == ResumeAbortReason.SOURCE_RUNNING

    @pytest.mark.asyncio
    async def test_yml_extension_added_when_missing(self, tmp_path, monkeypatch):
        """controllers_config entries without .yml extension still find the template."""
        monkeypatch.chdir(tmp_path)
        bots = tmp_path / "bots"

        ctrl_dir = bots / "conf" / "controllers"
        ctrl_dir.mkdir(parents=True)
        ctrl_cfg = {"controller_name": "range_inventory_ladder", "id": "ctrl_no_ext",
                       "connector_name": "nonkyc", "trading_pair": "XMR-USDT"}
        (ctrl_dir / "ctrl_no_ext.yml").write_text(yaml.safe_dump(ctrl_cfg), encoding="utf-8")

        src_data = bots / "instances" / "SRC-20260710-101010" / "data"
        src_data.mkdir(parents=True)
        (src_data / "range_inventory_ladder_ctrl_no_ext.json").write_bytes(
            json.dumps(valid_ledger_payload("ctrl_no_ext")).encode()
        )
        (src_data / "range_inventory_ladder_ctrl_no_ext.json.owner").write_text(
            json.dumps({"controller_id": "ctrl_no_ext"}), encoding="utf-8"
        )
        src_ctrl_dir = bots / "instances" / "SRC-20260710-101010" / "conf" / "controllers"
        src_ctrl_dir.mkdir(parents=True)
        (src_ctrl_dir / "ctrl_no_ext.yml").write_text(yaml.safe_dump(ctrl_cfg), encoding="utf-8")

        from docker.errors import NotFound as DockerNotFound
        docker_client = MagicMock()
        docker_client.containers.get.side_effect = DockerNotFound("x")

        fake_run = SimpleNamespace(
            instance_name="SRC-20260710-101010", run_status="STOPPED", stopped_at="2026-07-10",
            retirement_status="VERIFIED",  # CDX-005
            retirement_evidence=_VERIFIED_EVIDENCE_JSON,
        )
        fake_repo = AsyncMock()
        fake_repo.get_bot_runs = AsyncMock(return_value=[fake_run])

        from models import V2ControllerDeployment
        dep = V2ControllerDeployment(
            instance_name="TEST_BOT",
            credentials_profile="master_account",
            controllers_config=["ctrl_no_ext"],  # no .yml — preview_resume must add it
            resume_mode="explicit",
            resume_from="SRC-20260710-101010",
        )

        from services.resume_service import preview_resume
        result = await preview_resume(
            deployment=dep, bots_path=bots, docker_client=docker_client, bot_run_repo=fake_repo
        )

        assert result["would_succeed"] is True
        assert result["decisions"].get("ctrl_no_ext") == "copied"
