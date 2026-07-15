"""Phase 7 — end-to-end regression matrix (design §18 "Regression").

Exercises the *whole* copy-forward hook through the real deploy entry point
``services.docker_service.DockerService.create_hummingbot_instance`` (not the
individual pieces — those are unit-tested in the P2–P6 files). Every test here
drives resolve → guards → copy-plan → copy → drift → manifest via a genuine
deploy call, with only Docker, the DB and the filesystem faked.

Covers the five §18 regression items:

1. **Happy path** — a fabricated stopped source (ledger + ``.owner`` + a
   ``*.diagnostic_*.jsonl`` + a ``*.tmp``) is resumed: the ledger lands
   byte-identical (sha256) in the new ``data/`` *before* ``containers.run``, the
   ``.owner`` is copied, the never-copy junk is not, and the manifest matches
   reality.
2. **§11 fail-closed matrix** — one parametrized test walking EVERY row of the
   §11 failure-modes table: each abort row raises the correct
   ``ResumeAbortReason`` and ``containers.run`` is never called (instance dir
   cleaned up); the two non-abort rows (fresh-seed, absolute-skip) proceed to
   ``containers.run`` exactly as the design mandates.
3. **Config drift** — a template mutated vs. the source YAML raises a drift
   warning and records the field diff in the manifest (template wins).
4. **Off-mode equivalence** — a deploy with no resume fields never invokes the
   hook and produces ``containers.run`` args identical to a pre-change golden
   capture.
5. **``latest`` lineage e2e** — multiple fake ``bot_runs`` rows (incl. the
   operator-timestamp-embedded name) resolve to the newest; a duplicated newest
   lineage aborts ``LATEST_AMBIGUOUS``.

Absolute prohibitions honored: Docker is a ``MagicMock`` (``containers.run`` is
never allowed to touch a daemon — it is a spy), the DB is mock repos / a fake
async session-context, and every filesystem artifact lives under a chdir'd
``tmp_path``. No real container is ever started.
"""

import contextlib
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
from services.docker_service import DockerService
from services.resume_service import ResumeAbortReason, ResumeError

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

SRC_NAME = "LADDER_BOT-20260710-101010"
NEW_NAME = "LADDER_BOT-20260714-121212"
CONTROLLER_ID = "ladder_xmr"
CONTROLLER_FILE = "ladder_xmr.yml"
LEDGER_NAME = f"range_inventory_ladder_{CONTROLLER_ID}.json"
LEDGER_BYTES = json.dumps({"levels": [1, 2, 3], "seq": 42, "quote": "usdt"}).encode("utf-8")
OWNER_BYTES = json.dumps({"controller_id": CONTROLLER_ID, "pid": 7}).encode("utf-8")
SCRIPT_CONFIG = f"{NEW_NAME}.yml"
IMAGE = "hummingbot/hummingbot:v2.9"
CONFIG_PASSWORD = "test-password"

TEMPLATE_CFG = {
    "controller_name": "range_inventory_ladder",
    "id": CONTROLLER_ID,
    "connector_name": "kraken",
    "buy_spread": 0.1,
}


# ---------------------------------------------------------------------------
# Filesystem builders (kept tiny so every test can restage a variant)
# ---------------------------------------------------------------------------

def _write_source_instance(bots: Path, name: str, *, controller_cfg=TEMPLATE_CFG,
                           ledger_bytes=LEDGER_BYTES, owner_bytes=OWNER_BYTES,
                           ledger_name=LEDGER_NAME, with_junk=True):
    """Fabricate a stopped source instance: data/ (ledger + .owner [+ junk]) and
    a conf/controllers/ YAML (the drift-diff baseline)."""
    src = bots / "instances" / name
    (src / "data").mkdir(parents=True, exist_ok=True)
    if ledger_bytes is not None:
        (src / "data" / ledger_name).write_bytes(ledger_bytes)
    if owner_bytes is not None:
        (src / "data" / f"{ledger_name}.owner").write_bytes(owner_bytes)
    if with_junk:
        # §6 never-copy list: session-stamped diagnostics + atomic-write remnants.
        (src / "data" / "ladder.diagnostic_20260710.jsonl").write_text("junk", encoding="utf-8")
        (src / "data" / "partial.tmp").write_text("junk", encoding="utf-8")
    (src / "conf" / "controllers").mkdir(parents=True, exist_ok=True)
    (src / "conf" / "controllers" / CONTROLLER_FILE).write_text(
        yaml.safe_dump(controller_cfg), encoding="utf-8"
    )
    return src


def _write_script_config(bots: Path, script_name: str):
    scripts = bots / "conf" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / script_name).write_text(
        yaml.safe_dump(
            {"script_file_name": "v2_with_controllers.py", "controllers_config": [CONTROLLER_FILE]}
        ),
        encoding="utf-8",
    )


@pytest.fixture
def bots_tree(tmp_path, monkeypatch):
    """A full fake ``bots/`` tree under a chdir'd tmp_path: a Postgres credentials
    profile, shared script/controller templates, and a stopped source instance
    (``SRC_NAME``) holding a valid ledger + ``.owner`` plus never-copy junk."""
    monkeypatch.chdir(tmp_path)
    bots = tmp_path / "bots"

    creds = bots / "credentials" / "master_account"
    creds.mkdir(parents=True)
    # Postgres db_mode -> the copy plan must add no sqlite files (this operator).
    (creds / "conf_client.yml").write_text(
        yaml.safe_dump({"db_mode": {"db_engine": "postgres+asyncpg"}}), encoding="utf-8"
    )
    (creds / "connectors").mkdir()

    controllers = bots / "conf" / "controllers"
    controllers.mkdir(parents=True)
    (controllers / CONTROLLER_FILE).write_text(yaml.safe_dump(TEMPLATE_CFG), encoding="utf-8")

    _write_script_config(bots, SCRIPT_CONFIG)
    _write_source_instance(bots, SRC_NAME)
    return bots


@pytest.fixture
def patched_security(monkeypatch, tmp_path):
    """A config password (SCRIPT_CONFIG requires one) with gateway cert
    generation stubbed out so nothing real is created."""
    from config import settings

    monkeypatch.setattr(settings.security, "config_password", CONFIG_PASSWORD)
    monkeypatch.setattr("services.docker_service.ensure_gateway_certs", MagicMock())
    monkeypatch.setattr(
        "services.docker_service.gateway_certs_dir",
        MagicMock(return_value=str(tmp_path / "certs")),
    )


# ---------------------------------------------------------------------------
# Docker / service / deployment / repo builders
# ---------------------------------------------------------------------------

def make_docker_client(*, src_status="exited", src_not_found=False, on_run=None):
    """A fully mocked Docker client — its ``containers.run`` never touches a
    daemon; it is a spy (optionally with an ``on_run`` side effect that inspects
    the seeded ``data/`` at the exact moment of launch)."""
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
    """A ``DockerService`` that never spins up a real daemon or cleanup thread
    (``__new__`` bypasses ``__init__``)."""
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
        image=IMAGE,
        resume_mode="explicit",
        resume_from=SRC_NAME,
        # Most tests wire no DB -> unknown history counts as ungraceful, so the
        # baseline deploy opts in explicitly (the guard itself is P4-tested).
        resume_accept_ungraceful=True,
    )
    kwargs.update(overrides)
    return V2ControllerDeployment(**kwargs)


def graceful_row(instance_name):
    return SimpleNamespace(
        instance_name=instance_name,
        run_status="STOPPED",
        stopped_at=datetime(2026, 7, 10, 12, 0, 0),
    )


def make_db_manager(rows):
    """Return ``(db_manager, repo)`` where the manager yields a session and the
    repo answers ``get_bot_runs`` with ``rows``. Callers patch
    ``database.BotRunRepository`` to return ``repo`` for the duration of the
    deploy (mirrors the production ``seed_resume_state`` DB path)."""
    session = MagicMock()

    @asynccontextmanager
    async def session_ctx():
        yield session

    db_manager = MagicMock()
    db_manager.get_session_context = session_ctx
    repo = MagicMock()
    repo.get_bot_runs = AsyncMock(return_value=list(rows))
    return db_manager, repo


def new_instance_dir(bots):
    return bots / "instances" / NEW_NAME


def read_manifest(bots, name=NEW_NAME):
    return json.loads(
        (bots / "instances" / name / "data" / "resume.manifest.json").read_text(encoding="utf-8")
    )


# ===========================================================================
# §18.1 — Happy path
# ===========================================================================

class TestE2EHappyPath:
    @pytest.mark.asyncio
    async def test_ledger_lands_byte_identical_before_run(self, bots_tree, patched_security):
        """The ledger + .owner are present and byte-identical in the new data/ at
        the instant containers.run fires; junk is excluded; manifest == reality."""
        new_data = new_instance_dir(bots_tree) / "data"
        captured = {}

        def on_run(*args, **kwargs):
            # Snapshot exactly what is on disk at launch time (§3 ordering).
            captured["ledger"] = (new_data / LEDGER_NAME).read_bytes() \
                if (new_data / LEDGER_NAME).exists() else None
            captured["owner"] = (new_data / f"{LEDGER_NAME}.owner").read_bytes() \
                if (new_data / f"{LEDGER_NAME}.owner").exists() else None
            captured["diag"] = (new_data / "ladder.diagnostic_20260710.jsonl").exists()
            captured["tmp"] = (new_data / "partial.tmp").exists()
            captured["manifest"] = (new_data / "resume.manifest.json").exists()
            return MagicMock()

        client = make_docker_client(on_run=on_run)
        service = make_service(client)

        response = await service.create_hummingbot_instance(make_deployment())

        assert response["success"] is True
        client.containers.run.assert_called_once()

        # Byte-identical (sha256) ledger seeded before launch.
        assert captured["ledger"] == LEDGER_BYTES
        assert hashlib.sha256(captured["ledger"]).hexdigest() == hashlib.sha256(LEDGER_BYTES).hexdigest()
        # .owner carried forward, byte-identical.
        assert captured["owner"] == OWNER_BYTES
        # Never-copy junk excluded; manifest already written.
        assert captured["diag"] is False
        assert captured["tmp"] is False
        assert captured["manifest"] is True

    @pytest.mark.asyncio
    async def test_manifest_matches_reality(self, bots_tree, patched_security):
        """The manifest's file entries (name/size/sha256) describe exactly the
        bytes that landed and the source that produced them."""
        service = make_service(make_docker_client())
        await service.create_hummingbot_instance(make_deployment())

        manifest = read_manifest(bots_tree)
        assert manifest["source_instance"] == SRC_NAME
        assert manifest["mode"] == "explicit"
        assert manifest["decisions"] == {CONTROLLER_ID: "copied"}
        assert manifest["drift"] == []
        assert manifest["guard_report"]["passed"] is True

        by_name = {f["name"]: f for f in manifest["files"]}
        assert set(by_name) == {LEDGER_NAME, f"{LEDGER_NAME}.owner"}
        assert by_name[LEDGER_NAME]["size"] == len(LEDGER_BYTES)
        assert by_name[LEDGER_NAME]["sha256"] == hashlib.sha256(LEDGER_BYTES).hexdigest()
        assert by_name[f"{LEDGER_NAME}.owner"]["sha256"] == hashlib.sha256(OWNER_BYTES).hexdigest()

        # And the manifest description equals what is actually on disk now.
        new_data = new_instance_dir(bots_tree) / "data"
        for name, entry in by_name.items():
            landed = (new_data / name).read_bytes()
            assert entry["size"] == len(landed)
            assert entry["sha256"] == hashlib.sha256(landed).hexdigest()


# ===========================================================================
# §18.2 — §11 fail-closed matrix (one parametrized test, every row)
# ===========================================================================
#
# Each entry is (row_id, expected_reason). ``expected_reason is None`` marks the
# two §11 rows whose action is NOT an abort (per-controller semantics, §6): an
# expected-ledger-missing controller fresh-seeds, and an absolute
# ``state_file_name`` is skipped+warned — both proceed to containers.run. No §11
# row is skipped. ARCHIVE_NESTED / EXTRA_PATH_MISSING are not distinct §11 rows
# (they are resolution/extra-path sub-cases folded into the "not found" and
# "escape" rows) and are covered in the P2/P3 unit suites.

_MATRIX = [
    ("source_not_found", ResumeAbortReason.SOURCE_NOT_FOUND),
    ("latest_ambiguous", ResumeAbortReason.LATEST_AMBIGUOUS),
    ("source_running", ResumeAbortReason.SOURCE_RUNNING),
    ("ledger_missing_fresh_seed", None),        # §11 "expected ledger missing" -> warn+fresh seed
    ("ledger_invalid", ResumeAbortReason.LEDGER_INVALID),
    ("owner_mismatch", ResumeAbortReason.OWNER_MISMATCH),
    ("state_file_absolute_skip", None),         # §11 "state_file_name absolute" -> skip+warn
    ("dest_not_empty", ResumeAbortReason.DEST_NOT_EMPTY),
    ("ungraceful_source", ResumeAbortReason.UNGRACEFUL_SOURCE),
    ("extra_path_escape", ResumeAbortReason.EXTRA_PATH_ESCAPE),
    ("copy_io_error", ResumeAbortReason.COPY_IO_ERROR),
]


def _prepare_row(row_id, bots_tree):
    """Set up one §11 row and return
    ``(deployment, docker_client, db_manager, patch_ctx, extra)``.

    ``patch_ctx`` is a context manager active around the deploy (copy2 failure /
    BotRunRepository injection); ``extra`` carries per-row post-assert data.
    """
    src = bots_tree / "instances" / SRC_NAME
    patch_ctx = contextlib.nullcontext()
    db_manager = None
    extra = {}

    if row_id == "source_not_found":
        deployment = make_deployment(resume_from="GHOST-20260101-000000")
        client = make_docker_client()

    elif row_id == "latest_ambiguous":
        # Two identical newest-lineage rows -> timestamp tie -> ambiguous (§5).
        dup = "LADDER_BOT-20260713-090000"
        db_manager, repo = make_db_manager([graceful_row(dup), graceful_row(dup)])
        deployment = make_deployment(resume_mode="latest", resume_from=None)
        client = make_docker_client()
        patch_ctx = patch("database.BotRunRepository", return_value=repo)

    elif row_id == "source_running":
        deployment = make_deployment()
        client = make_docker_client(src_status="running")

    elif row_id == "ledger_missing_fresh_seed":
        # Remove the source ledger + sidecar -> this controller fresh-seeds.
        (src / "data" / LEDGER_NAME).unlink()
        (src / "data" / f"{LEDGER_NAME}.owner").unlink()
        deployment = make_deployment()
        client = make_docker_client()
        extra["decision"] = "fresh_seed"

    elif row_id == "ledger_invalid":
        (src / "data" / LEDGER_NAME).write_bytes(b"{not valid json")
        deployment = make_deployment()
        client = make_docker_client()

    elif row_id == "owner_mismatch":
        (src / "data" / f"{LEDGER_NAME}.owner").write_text(
            json.dumps({"controller_id": "SOME_OTHER_CONTROLLER"}), encoding="utf-8"
        )
        deployment = make_deployment()
        client = make_docker_client()

    elif row_id == "state_file_absolute_skip":
        # An absolute state_file_name escapes data/ -> skip+warn (POSIX abs works
        # cross-platform via the service's own is-absolute check).
        abs_cfg = dict(TEMPLATE_CFG, state_file_name="/var/lib/hummingbot/ladder.json")
        (bots_tree / "conf" / "controllers" / CONTROLLER_FILE).write_text(
            yaml.safe_dump(abs_cfg), encoding="utf-8"
        )
        deployment = make_deployment()
        client = make_docker_client()
        extra["decision"] = "skipped"

    elif row_id == "dest_not_empty":
        # Pre-create the destination instance dir with a stray state file so the
        # deploy's makedirs is skipped and the DEST_NOT_EMPTY guard trips.
        dest_data = new_instance_dir(bots_tree) / "data"
        dest_data.mkdir(parents=True)
        (dest_data / "stray.json").write_text("{}", encoding="utf-8")
        deployment = make_deployment()
        client = make_docker_client()

    elif row_id == "ungraceful_source":
        # No DB wired -> unknown history is not graceful; no override.
        deployment = make_deployment(resume_accept_ungraceful=False)
        client = make_docker_client()

    elif row_id == "extra_path_escape":
        outside = bots_tree.parent / "outside_secret"
        outside.mkdir(exist_ok=True)
        (outside / "target.txt").write_text("secret", encoding="utf-8")
        link = src / "data" / "leak.txt"
        try:
            os.symlink(outside / "target.txt", link)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not supported on this platform/privilege level")
        # A benign relative name (no '..', not absolute) -> passes model
        # validation; the symlink target is what escapes data/ at plan time.
        deployment = make_deployment(resume_extra_paths=["leak.txt"])
        client = make_docker_client()

    elif row_id == "copy_io_error":
        real_copy2 = shutil.copy2

        def failing_copy2(src_path, dst, *args, **kwargs):
            # Scope the failure to the hook's copies into data/ (config staging
            # in docker_service shares the same shutil module).
            if "data" in Path(dst).parts:
                raise OSError("disk full")
            return real_copy2(src_path, dst, *args, **kwargs)

        deployment = make_deployment()
        client = make_docker_client()
        patch_ctx = patch("services.resume_service.shutil.copy2", side_effect=failing_copy2)

    else:  # pragma: no cover - guard against a typo'd matrix entry
        raise AssertionError(f"unhandled matrix row {row_id!r}")

    return deployment, client, db_manager, patch_ctx, extra


class TestE2EFailClosedMatrix:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("row_id,expected_reason", _MATRIX, ids=[r[0] for r in _MATRIX])
    async def test_matrix_row(self, bots_tree, patched_security, row_id, expected_reason):
        deployment, client, db_manager, patch_ctx, extra = _prepare_row(row_id, bots_tree)
        service = make_service(client, db_manager=db_manager)

        if expected_reason is not None:
            with patch_ctx:
                with pytest.raises(ResumeError) as exc:
                    await service.create_hummingbot_instance(deployment)
            assert exc.value.reason is expected_reason
            # The core invariant: a failed resume never launches a container...
            client.containers.run.assert_not_called()
            # ...and leaves no half-seeded instance dir behind (§5 cleanup).
            assert not new_instance_dir(bots_tree).exists(), (
                f"[{row_id}] half-created instance dir was not cleaned up"
            )
        else:
            # Non-abort §11 rows proceed to a real (mocked) launch.
            with patch_ctx:
                response = await service.create_hummingbot_instance(deployment)
            assert response["success"] is True
            client.containers.run.assert_called_once()
            manifest = read_manifest(bots_tree)
            assert manifest["decisions"] == {CONTROLLER_ID: extra["decision"]}
            # The fresh-seeded / skipped controller copied no ledger.
            assert not (new_instance_dir(bots_tree) / "data" / LEDGER_NAME).exists()


# ===========================================================================
# §18.3 — Config drift
# ===========================================================================

class TestE2EDrift:
    @pytest.mark.asyncio
    async def test_drift_warns_and_records_manifest_entry(self, bots_tree, patched_security, caplog):
        # The source ran a live-edited config (spread changed + a field added)
        # vs. the shared template that the redeploy stages.
        drifted = dict(TEMPLATE_CFG, buy_spread=0.5, live_edited_field=True)
        (bots_tree / "instances" / SRC_NAME / "conf" / "controllers" / CONTROLLER_FILE).write_text(
            yaml.safe_dump(drifted), encoding="utf-8"
        )
        service = make_service(make_docker_client())
        with caplog.at_level(logging.WARNING, logger="services.resume_service"):
            response = await service.create_hummingbot_instance(make_deployment())

        assert response["success"] is True
        manifest = read_manifest(bots_tree)
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


# ===========================================================================
# §18.4 — Off-mode equivalence (pre-change golden capture)
# ===========================================================================
#
# The container-run args a deploy WITHOUT resume fields produces must be
# byte-for-byte the pre-hook behavior. This golden encodes exactly what the
# unchanged code path passed to containers.run for this deployment; any drift in
# the off-mode path (an accidental seed, an added env var, a changed flag) breaks
# the equality assertions below.

_GOLDEN_SCALARS = {
    "image": IMAGE,
    "name": NEW_NAME,
    "network_mode": "host",          # DOCKER_BOT_NETWORK_MODE default
    "labels": {"autoheal": "true"},  # no COMPOSE_PROJECT_NAME in the test env
    "detach": True,
    "tty": True,
    "stdin_open": True,
    "environment": {"CONFIG_PASSWORD": CONFIG_PASSWORD, "SCRIPT_CONFIG": SCRIPT_CONFIG},
}
_GOLDEN_BIND_TARGETS = {
    "/home/hummingbot/conf",
    "/home/hummingbot/conf/connectors",
    "/home/hummingbot/conf/scripts",
    "/home/hummingbot/conf/controllers",
    "/home/hummingbot/data",
    "/home/hummingbot/logs",
    "/home/hummingbot/scripts",
    "/home/hummingbot/controllers",
    "/home/hummingbot/certs",  # SEC-048 mTLS certs (present when config_password set)
}


class TestE2EOffModeEquivalence:
    @pytest.mark.asyncio
    async def test_offmode_hook_not_invoked_and_run_args_match_golden(self, bots_tree, patched_security):
        client = make_docker_client()
        service = make_service(client)
        # A deploy with NO resume fields at all — pure pre-change shape.
        deployment = make_deployment(
            resume_mode="off", resume_from=None, resume_accept_ungraceful=False
        )

        with patch("services.docker_service.seed_resume_state", new=AsyncMock()) as spy:
            response = await service.create_hummingbot_instance(deployment)

        # The hook function is never even entered when resume is off.
        spy.assert_not_called()
        assert response["success"] is True

        client.containers.run.assert_called_once()
        kwargs = client.containers.run.call_args.kwargs

        # Scalar run args match the golden exactly.
        for key, expected in _GOLDEN_SCALARS.items():
            assert kwargs[key] == expected, f"off-mode run arg '{key}' drifted from golden"

        # Volumes: exactly the 8 pre-change bind mounts, data bound rw.
        bind_targets = {spec["bind"] for spec in kwargs["volumes"].values()}
        assert bind_targets == _GOLDEN_BIND_TARGETS
        data_spec = next(
            spec for host, spec in kwargs["volumes"].items() if host.endswith(os.path.join("data"))
        )
        assert data_spec == {"bind": "/home/hummingbot/data", "mode": "rw"}

        # log_config is the pre-change json-file rotation config.
        assert kwargs["log_config"].type == "json-file"
        assert kwargs["log_config"].config == {"max-size": "10m", "max-file": "5"}

        # And the decisive proof off-mode did not seed: data/ is empty.
        assert list((new_instance_dir(bots_tree) / "data").iterdir()) == []


# ===========================================================================
# §18.5 — `latest` lineage e2e
# ===========================================================================

class TestE2ELatestLineage:
    # An operator-supplied name that embeds its OWN timestamp-like token, plus
    # the API's timestamp suffix (the real KRAKEN_LADDER_V1 case, §5): only the
    # FINAL -YYYYMMDD-HHMMSS suffix is stripped.
    BASE = "KRAKEN_LADDER_V1-20260712-2302"
    OLDER = f"{BASE}-20260712-230254"
    WINNER = f"{BASE}-20260713-101010"
    NEW = f"{BASE}-20260714-120000"

    def _stage(self, bots_tree):
        """Stage the winner + older source instances on disk and a matching
        script config for the new (latest) instance name."""
        _write_source_instance(bots_tree, self.WINNER)
        _write_source_instance(bots_tree, self.OLDER)
        _write_script_config(bots_tree, f"{self.NEW}.yml")

    def _deployment(self, **overrides):
        kwargs = dict(
            instance_name=self.NEW,
            credentials_profile="master_account",
            controllers_config=[CONTROLLER_FILE],
            script_config=f"{self.NEW}.yml",
            image=IMAGE,
            resume_mode="latest",
            resume_accept_ungraceful=False,
        )
        kwargs.update(overrides)
        return V2ControllerDeployment(**kwargs)

    @pytest.mark.asyncio
    async def test_newest_lineage_resolved(self, bots_tree, patched_security):
        self._stage(bots_tree)
        # bot_runs history: both prior runs graceful; the winner is newest by the
        # PARSED api timestamp (not mtime, not list order).
        db_manager, repo = make_db_manager([
            graceful_row(self.OLDER),
            graceful_row(self.WINNER),
        ])
        client = make_docker_client()
        service = make_service(client, db_manager=db_manager)

        with patch("database.BotRunRepository", return_value=repo):
            response = await service.create_hummingbot_instance(self._deployment())

        assert response["success"] is True
        client.containers.run.assert_called_once()
        manifest = read_manifest(bots_tree, name=self.NEW)
        assert manifest["source_instance"] == self.WINNER
        assert manifest["mode"] == "latest"
        assert manifest["decisions"] == {CONTROLLER_ID: "copied"}
        # The ledger was carried forward byte-identical from the WINNER.
        landed = (bots_tree / "instances" / self.NEW / "data" / LEDGER_NAME).read_bytes()
        assert landed == LEDGER_BYTES

    @pytest.mark.asyncio
    async def test_ambiguous_lineage_aborts(self, bots_tree, patched_security):
        self._stage(bots_tree)
        # Two rows share the SAME newest name/timestamp -> conflicting lineage.
        db_manager, repo = make_db_manager([
            graceful_row(self.WINNER),
            graceful_row(self.WINNER),
        ])
        client = make_docker_client()
        service = make_service(client, db_manager=db_manager)

        with patch("database.BotRunRepository", return_value=repo):
            with pytest.raises(ResumeError) as exc:
                await service.create_hummingbot_instance(self._deployment())

        assert exc.value.reason is ResumeAbortReason.LATEST_AMBIGUOUS
        client.containers.run.assert_not_called()
        assert not (bots_tree / "instances" / self.NEW).exists()
