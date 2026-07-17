"""Phase 5 tests: hook wiring into ``create_hummingbot_instance`` (design §3/§4, §12, §13).

Covers ``services.docker_service.DockerService.create_hummingbot_instance`` +
``services.resume_service.seed_resume_state``:

  * call ordering — the copies land in the new ``data/`` BEFORE ``containers.run``
    (asserted from inside the ``containers.run`` mock)
  * abort in each stage (resolve / guards / copy plan / copy-I/O) ->
    ``containers.run`` is never called AND the just-created instance dir is removed
  * manifest contents, including sha256/size correctness and per-controller decisions
  * config-drift diff (§12) fires on a mutated template, records the fields,
    template wins (staged YAML untouched)
  * off-mode -> the hook function is NOT invoked (spy) and ``containers.run`` is
    called exactly as before
  * ``COPY_IO_ERROR`` path (``shutil.copy2`` patched to raise)
  * ``bot_resume_seeded`` / ``bot_resume_failed`` structured events + §13 one-liner
  * the ``db_manager`` session path builds a ``BotRunRepository`` and queries lineage

Docker is mocked (``MagicMock`` client, ``docker.errors.NotFound`` for absent
containers); the DB via mock repos / a fake session-context manager; all
filesystem via ``tmp_path`` (chdir'd). No real Docker, no DB, no network, no
containers ever started.
"""

import hashlib
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from docker.errors import NotFound

from models import V2ControllerDeployment
from ledger_fixtures import valid_ledger_payload
from services.docker_service import DockerService
from services.resume_service import ResumeAbortReason, ResumeError, seed_resume_state

SRC_NAME = "LADDER_BOT-20260710-101010"
NEW_NAME = "LADDER_BOT-20260714-121212"
CONTROLLER_ID = "ladder_xmr"
CONTROLLER_FILE = "ladder_xmr.yml"
LEDGER_NAME = f"range_inventory_ladder_{CONTROLLER_ID}.json"
# A VALID engine ledger envelope (CDX-M02, phase 4). This was a placeholder
# ({"levels": [1, 2, 3], ...}) back when the validator only checked length + UTF-8
# + json.loads: valid JSON, but never a ledger the engine would load — it would
# quarantine on load and re-seed from the wallet. The envelope validator rejects
# it, so the fixture is now built from the engine's own contract. connector_name
# must match TEMPLATE_CFG's, which the API compares the ledger against.
LEDGER_BYTES = json.dumps(
    valid_ledger_payload(CONTROLLER_ID, connector_name="kraken")
).encode("utf-8")
SCRIPT_CONFIG = f"{NEW_NAME}.yml"

TEMPLATE_CFG = {
    "controller_name": "range_inventory_ladder",
    "id": CONTROLLER_ID,
    "connector_name": "kraken",
    "buy_spread": 0.1,
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def bots_tree(tmp_path, monkeypatch):
    """A full fake ``bots/`` tree under a chdir'd tmp_path: credentials profile,
    shared script/controller templates, and a stopped source instance holding a
    valid ledger + ``.owner`` (plus never-copy junk)."""
    monkeypatch.chdir(tmp_path)
    bots = tmp_path / "bots"

    creds = bots / "credentials" / "master_account"
    creds.mkdir(parents=True)
    # Postgres db_mode: the copy plan must not add sqlite files.
    (creds / "conf_client.yml").write_text(
        yaml.safe_dump({"db_mode": {"db_engine": "postgres+asyncpg"}}), encoding="utf-8"
    )
    (creds / "connectors").mkdir()

    controllers = bots / "conf" / "controllers"
    controllers.mkdir(parents=True)
    (controllers / CONTROLLER_FILE).write_text(yaml.safe_dump(TEMPLATE_CFG), encoding="utf-8")

    scripts = bots / "conf" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / SCRIPT_CONFIG).write_text(
        yaml.safe_dump(
            {"script_file_name": "v2_with_controllers.py", "controllers_config": [CONTROLLER_FILE]}
        ),
        encoding="utf-8",
    )

    src = bots / "instances" / SRC_NAME
    (src / "data").mkdir(parents=True)
    (src / "data" / LEDGER_NAME).write_bytes(LEDGER_BYTES)
    (src / "data" / f"{LEDGER_NAME}.owner").write_text(
        json.dumps({"controller_id": CONTROLLER_ID, "pid": 7}), encoding="utf-8"
    )
    (src / "conf" / "controllers").mkdir(parents=True)
    (src / "conf" / "controllers" / CONTROLLER_FILE).write_text(
        yaml.safe_dump(TEMPLATE_CFG), encoding="utf-8"
    )
    # Never-copied junk (§6 exclusion list).
    (src / "data" / "ladder.diagnostic_20260710.jsonl").write_text("junk", encoding="utf-8")
    (src / "data" / "partial.tmp").write_text("junk", encoding="utf-8")
    return bots


@pytest.fixture
def patched_security(monkeypatch, tmp_path):
    """A config password (SCRIPT_CONFIG requires one) with gateway cert
    generation stubbed out so nothing real is created."""
    from config import settings

    monkeypatch.setattr(settings.security, "config_password", "test-password")
    monkeypatch.setattr("services.docker_service.ensure_gateway_certs", MagicMock())
    monkeypatch.setattr(
        "services.docker_service.gateway_certs_dir",
        MagicMock(return_value=str(tmp_path / "certs")),
    )


def make_docker_client(*, src_status="exited", src_not_found=False, on_run=None):
    client = MagicMock()
    if src_not_found:
        client.containers.get.side_effect = NotFound("no such container")
    else:
        container = MagicMock()
        container.status = src_status
        client.containers.get.return_value = container
    if on_run is not None:
        client.containers.run.side_effect = on_run
    return client


def make_service(client, db_manager=None):
    """A DockerService without touching a real Docker daemon or spawning the
    cleanup thread (bypasses __init__)."""
    service = DockerService.__new__(DockerService)
    service.SOURCE_PATH = os.getcwd()
    service.db_manager = db_manager
    service._pull_status = {}
    service._cleanup_thread = None
    service.client = client
    return service


def make_deployment(**overrides):
    kwargs = dict(
        instance_name=NEW_NAME,
        credentials_profile="master_account",
        controllers_config=[CONTROLLER_FILE],
        script_config=SCRIPT_CONFIG,
        image="hummingbot/hummingbot:v2.9",
        resume_mode="explicit",
        resume_from=SRC_NAME,
        # No DB is wired in most tests -> unknown history counts as ungraceful,
        # so the deploys opt in explicitly (the guard itself is P4-tested).
        resume_accept_ungraceful=True,
    )
    kwargs.update(overrides)
    return V2ControllerDeployment(**kwargs)


def graceful_repo():
    repo = MagicMock()
    repo.get_bot_runs = AsyncMock(
        return_value=[
            SimpleNamespace(
                instance_name=SRC_NAME,
                run_status="STOPPED",
                stopped_at=datetime(2026, 7, 10, 12, 0, 0),
            )
        ]
    )
    return repo


def new_instance_dir(bots_tree):
    return bots_tree / "instances" / NEW_NAME


# ===========================================================================
# Happy path: ordering, byte-identical copy, exclusions
# ===========================================================================

class TestHookHappyPath:
    @pytest.mark.asyncio
    async def test_copies_land_before_containers_run(self, bots_tree, patched_security):
        """The ledger + .owner must already be in the new data/ at the moment
        containers.run is invoked (§3 ordering), byte-identical."""
        new_data = new_instance_dir(bots_tree) / "data"
        seen_at_run = {}

        def on_run(*args, **kwargs):
            seen_at_run["ledger"] = (new_data / LEDGER_NAME).read_bytes() \
                if (new_data / LEDGER_NAME).exists() else None
            seen_at_run["owner"] = (new_data / f"{LEDGER_NAME}.owner").exists()
            seen_at_run["manifest"] = (new_data / "resume.manifest.json").exists()
            return MagicMock()

        client = make_docker_client(on_run=on_run)
        service = make_service(client)

        response = await service.create_hummingbot_instance(make_deployment())

        assert response["success"] is True
        client.containers.run.assert_called_once()
        assert seen_at_run["ledger"] == LEDGER_BYTES, "ledger not seeded before containers.run"
        assert seen_at_run["owner"] is True
        assert seen_at_run["manifest"] is True

    @pytest.mark.asyncio
    async def test_junk_files_not_copied(self, bots_tree, patched_security):
        service = make_service(make_docker_client())
        await service.create_hummingbot_instance(make_deployment())

        new_data = new_instance_dir(bots_tree) / "data"
        assert not (new_data / "ladder.diagnostic_20260710.jsonl").exists()
        assert not (new_data / "partial.tmp").exists()
        # And no sqlite either (Postgres deployment).
        assert list(new_data.glob("*.sqlite")) == []

    @pytest.mark.asyncio
    async def test_seeded_event_and_one_liner_logged(self, bots_tree, patched_security, caplog):
        service = make_service(make_docker_client())
        with caplog.at_level(logging.INFO, logger="services.resume_service"):
            await service.create_hummingbot_instance(make_deployment())

        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("bot_resume_seeded:") for m in messages)
        assert any(m.startswith(f"Resumed {NEW_NAME} from {SRC_NAME}:") for m in messages)


# ===========================================================================
# Abort in each stage -> containers.run NOT called, instance dir cleaned
# ===========================================================================

class TestHookAborts:
    async def _assert_aborts(self, bots_tree, deployment, client, reason):
        service = make_service(client)
        with pytest.raises(ResumeError) as exc:
            await service.create_hummingbot_instance(deployment)
        assert exc.value.reason is reason
        client.containers.run.assert_not_called()
        assert not new_instance_dir(bots_tree).exists(), (
            "half-created instance dir was not cleaned up"
        )

    @pytest.mark.asyncio
    async def test_resolve_abort(self, bots_tree, patched_security):
        deployment = make_deployment(resume_from="GHOST-20260101-000000")
        await self._assert_aborts(
            bots_tree, deployment, make_docker_client(), ResumeAbortReason.SOURCE_NOT_FOUND
        )

    @pytest.mark.asyncio
    async def test_guard_abort_source_running(self, bots_tree, patched_security):
        await self._assert_aborts(
            bots_tree,
            make_deployment(),
            make_docker_client(src_status="running"),
            ResumeAbortReason.SOURCE_RUNNING,
        )

    @pytest.mark.asyncio
    async def test_plan_abort_invalid_ledger(self, bots_tree, patched_security):
        (bots_tree / "instances" / SRC_NAME / "data" / LEDGER_NAME).write_bytes(b"{not json")
        await self._assert_aborts(
            bots_tree, make_deployment(), make_docker_client(), ResumeAbortReason.LEDGER_INVALID
        )

    @pytest.mark.asyncio
    async def test_copy_io_error(self, bots_tree, patched_security):
        # docker_service's config staging shares the same shutil module, so the
        # failure must be scoped to the hook's copies into data/.
        real_copy2 = shutil.copy2

        def failing_copy2(src, dst, *args, **kwargs):
            if "data" in Path(dst).parts:
                raise OSError("disk full")
            return real_copy2(src, dst, *args, **kwargs)

        with patch("services.resume_service.shutil.copy2", side_effect=failing_copy2):
            await self._assert_aborts(
                bots_tree, make_deployment(), make_docker_client(), ResumeAbortReason.COPY_IO_ERROR
            )

    @pytest.mark.asyncio
    async def test_failed_event_logged_with_reason(self, bots_tree, patched_security, caplog):
        service = make_service(make_docker_client(src_status="running"))
        with caplog.at_level(logging.ERROR, logger="services.resume_service"):
            with pytest.raises(ResumeError):
                await service.create_hummingbot_instance(make_deployment())
        failed = [r for r in caplog.records if r.getMessage().startswith("bot_resume_failed:")]
        assert failed, "bot_resume_failed event not logged"
        assert "SOURCE_RUNNING" in failed[0].getMessage()


# ===========================================================================
# Manifest contents (incl. sha256 correctness)
# ===========================================================================

class TestManifest:
    @pytest.mark.asyncio
    async def test_manifest_contents(self, bots_tree, patched_security):
        service = make_service(make_docker_client())
        await service.create_hummingbot_instance(make_deployment())

        manifest_path = new_instance_dir(bots_tree) / "data" / "resume.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert manifest["source_instance"] == SRC_NAME
        assert Path(manifest["source_path"]).name == "data"
        assert SRC_NAME in manifest["source_path"]
        assert manifest["mode"] == "explicit"
        assert manifest["decisions"] == {CONTROLLER_ID: "copied"}
        assert manifest["drift"] == []
        assert manifest["guard_report"]["passed"] is True
        assert manifest["guard_report"]["warnings"], "accepted-ungraceful warning missing"
        assert manifest["created_at"]

        by_name = {f["name"]: f for f in manifest["files"]}
        assert set(by_name) == {LEDGER_NAME, f"{LEDGER_NAME}.owner"}
        assert by_name[LEDGER_NAME]["size"] == len(LEDGER_BYTES)
        assert by_name[LEDGER_NAME]["sha256"] == hashlib.sha256(LEDGER_BYTES).hexdigest()
        owner_bytes = (
            bots_tree / "instances" / SRC_NAME / "data" / f"{LEDGER_NAME}.owner"
        ).read_bytes()
        assert by_name[f"{LEDGER_NAME}.owner"]["sha256"] == hashlib.sha256(owner_bytes).hexdigest()


# ===========================================================================
# Config-drift diff (§12)
# ===========================================================================

class TestConfigDrift:
    @pytest.mark.asyncio
    async def test_drift_recorded_and_template_wins(self, bots_tree, patched_security, caplog):
        # The source instance ran with a live-edited config: spread changed and
        # an extra field added vs. the shared template.
        drifted = dict(TEMPLATE_CFG, buy_spread=0.5, live_edited_field=True)
        (bots_tree / "instances" / SRC_NAME / "conf" / "controllers" / CONTROLLER_FILE).write_text(
            yaml.safe_dump(drifted), encoding="utf-8"
        )
        service = make_service(make_docker_client())
        with caplog.at_level(logging.WARNING, logger="services.resume_service"):
            await service.create_hummingbot_instance(make_deployment())

        manifest = json.loads(
            (new_instance_dir(bots_tree) / "data" / "resume.manifest.json").read_text(encoding="utf-8")
        )
        assert len(manifest["drift"]) == 1
        entry = manifest["drift"][0]
        assert entry["file"] == CONTROLLER_FILE
        assert entry["controller_id"] == CONTROLLER_ID
        fields = {f["field"]: f for f in entry["fields"]}
        assert fields["buy_spread"]["source"] == 0.5
        assert fields["buy_spread"]["template"] == 0.1
        assert fields["live_edited_field"]["source"] is True
        assert fields["live_edited_field"]["template"] == "<absent>"
        assert any("Config drift" in r.getMessage() for r in caplog.records)

        # Template wins: the staged YAML is the template, not the source's copy.
        staged = yaml.safe_load(
            (new_instance_dir(bots_tree) / "conf" / "controllers" / CONTROLLER_FILE).read_text(
                encoding="utf-8"
            )
        )
        assert staged == TEMPLATE_CFG

    @pytest.mark.asyncio
    async def test_no_drift_when_configs_match(self, bots_tree, patched_security):
        service = make_service(make_docker_client())
        await service.create_hummingbot_instance(make_deployment())
        manifest = json.loads(
            (new_instance_dir(bots_tree) / "data" / "resume.manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["drift"] == []


# ===========================================================================
# Off-mode: hook not invoked, containers.run exactly as before
# ===========================================================================

class TestOffMode:
    @pytest.mark.asyncio
    async def test_hook_not_invoked_and_run_called_as_before(self, bots_tree, patched_security):
        client = make_docker_client()
        service = make_service(client)
        deployment = make_deployment(
            resume_mode="off", resume_from=None, resume_accept_ungraceful=False
        )

        with patch("services.docker_service.seed_resume_state", new=AsyncMock()) as spy:
            response = await service.create_hummingbot_instance(deployment)

        spy.assert_not_called()
        assert response["success"] is True
        client.containers.run.assert_called_once()
        kwargs = client.containers.run.call_args.kwargs
        assert kwargs["name"] == NEW_NAME
        assert kwargs["image"] == "hummingbot/hummingbot:v2.9"
        assert kwargs["detach"] is True
        assert kwargs["environment"]["SCRIPT_CONFIG"] == SCRIPT_CONFIG
        # Fresh empty data/ — current behavior untouched.
        assert list((new_instance_dir(bots_tree) / "data").iterdir()) == []


# ===========================================================================
# db_manager session path (the real production wiring)
# ===========================================================================

class TestDbManagerPath:
    @pytest.mark.asyncio
    async def test_session_opened_and_repo_queried(self, bots_tree, patched_security):
        session = MagicMock()

        @asynccontextmanager
        async def session_ctx():
            yield session

        db_manager = MagicMock()
        db_manager.get_session_context = session_ctx
        repo = graceful_repo()

        client = make_docker_client()
        service = make_service(client, db_manager=db_manager)
        # Graceful history in the DB -> no ungraceful override needed.
        deployment = make_deployment(resume_accept_ungraceful=False)

        with patch("database.BotRunRepository", return_value=repo) as repo_cls:
            response = await service.create_hummingbot_instance(deployment)

        assert response["success"] is True
        repo_cls.assert_called_once_with(session)
        repo.get_bot_runs.assert_awaited()
        client.containers.run.assert_called_once()


# ===========================================================================
# seed_resume_state called directly (no create_hummingbot_instance around it)
# ===========================================================================

class TestSeedResumeStateDirect:
    @pytest.mark.asyncio
    async def test_returns_manifest_with_graceful_guard(self, bots_tree, patched_security):
        # Stage the new instance by hand (conf staged, data/ empty).
        inst = new_instance_dir(bots_tree)
        (inst / "data").mkdir(parents=True)
        (inst / "conf" / "controllers").mkdir(parents=True)
        (inst / "conf" / "controllers" / CONTROLLER_FILE).write_text(
            yaml.safe_dump(TEMPLATE_CFG), encoding="utf-8"
        )
        (inst / "conf" / "conf_client.yml").write_text(
            yaml.safe_dump({"db_mode": {"db_engine": "postgres+asyncpg"}}), encoding="utf-8"
        )

        deployment = make_deployment(resume_accept_ungraceful=False)
        manifest = await seed_resume_state(
            deployment=deployment,
            new_instance_dir=inst,
            bots_path=bots_tree,
            docker_client=make_docker_client(src_not_found=True),
            bot_run_repo=graceful_repo(),
        )

        assert manifest["decisions"] == {CONTROLLER_ID: "copied"}
        assert manifest["guard_report"]["passed"] is True
        assert manifest["guard_report"]["warnings"] == []
        assert (inst / "data" / LEDGER_NAME).read_bytes() == LEDGER_BYTES
        on_disk = json.loads((inst / "data" / "resume.manifest.json").read_text(encoding="utf-8"))
        assert on_disk == json.loads(json.dumps(manifest, default=str))

    @pytest.mark.asyncio
    async def test_unexpected_error_cleans_up_and_reraises(self, bots_tree, patched_security, caplog):
        # created_by_this_attempt=True: this caller made the dir, so the hook may
        # remove it (CDX-001 — cleanup is now opt-in per attempt; see
        # test_copyforward_p1_exclusive_target.py for the default-deny half).
        inst = new_instance_dir(bots_tree)
        (inst / "data").mkdir(parents=True)

        boom = RuntimeError("docker daemon exploded")
        client = MagicMock()
        client.containers.get.side_effect = boom

        with caplog.at_level(logging.ERROR, logger="services.resume_service"):
            with pytest.raises(RuntimeError):
                await seed_resume_state(
                    deployment=make_deployment(),
                    new_instance_dir=inst,
                    bots_path=bots_tree,
                    docker_client=client,
                    bot_run_repo=graceful_repo(),
                    created_by_this_attempt=True,
                )
        assert not inst.exists()
        assert any("UNEXPECTED:RuntimeError" in r.getMessage() for r in caplog.records)
