"""Phase 1 (CDX-001 + CLA-008 P1) — exclusive target creation + real-path preview.

The bug being fixed: ``create_hummingbot_instance`` REUSED an existing instance
directory (``docker_service.py:220`` pre-fix) and rmtree'd an existing ``conf/``
(``:229-233``); on any resume failure ``_cleanup_failed_instance`` then rmtree'd
that same directory. Deploying a name that already existed therefore adopted a
live instance's tree, and a failure deleted it — ledger, sqlite and all. The fix
is exclusive creation: build in a staging sibling this attempt created, promote
atomically, and never touch a directory we did not create.

The second half (CLA-008 P1): ``preview_resume`` ran its guards against a
throwaway temp dir. An empty temp dir always looks clean, so the destination
guards could not fail and the preview reported a PASS it had not earned. They
now run against the real ``bots/instances/<target>`` path, via the deploy's own
name generation and guard code.

Test authenticity (batch prompt §"Test authenticity"): every test here drives the
REAL ``create_hummingbot_instance`` / ``preview_resume`` / ``seed_resume_state``
against a REAL filesystem under a chdir'd ``tmp_path``. The only mock is the
Docker client — the one unavoidable external (its ``containers.run`` is a spy
that must never touch a daemon). Expected values come from the phase spec, not
from running the implementation. Each test names, in a comment, the single-line
implementation mutation it is built to catch.
"""

import asyncio
import errno
import json
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

from database.repositories.bot_run_repository import REQUIRED_RETIREMENT_EVIDENCE
from models import V2ControllerDeployment
from services import docker_service
from ledger_fixtures import valid_ledger_payload
from services.docker_service import DockerService
from services.resume_service import (
    ResumeAbortReason,
    ResumeError,
    _parse_api_timestamp,
    _strip_api_suffix,
    generate_instance_name,
    preview_resume,
    resolve_deploy_target,
    seed_resume_state,
)

# ---------------------------------------------------------------------------
# Constants (mirror the e2e suite's shape so fixtures stay recognisable)
# ---------------------------------------------------------------------------

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
OWNER_BYTES = json.dumps({"controller_id": CONTROLLER_ID, "pid": 7}).encode("utf-8")
SCRIPT_CONFIG = f"{NEW_NAME}.yml"
IMAGE = "hummingbot/hummingbot:v2.9"
CONFIG_PASSWORD = "test-password"

TEMPLATE_CFG = {
    "controller_name": "range_inventory_ladder",
    "id": CONTROLLER_ID,
    "connector_name": "kraken",
    # The ledger fixture's pair. Absent, the engine would resolve its model
    # default (ETH-USDT, range_inventory_ladder.py:203) and compare the ledger
    # against THAT -> a correct LEDGER_INVALID abort that has nothing to do with
    # what this file tests (CDX-M02/CDX-R02).
    "trading_pair": "XMR-USDT",
    "buy_spread": 0.1,
}

# The sentinels: bytes an operator would lose if the deploy reused or deleted an
# existing instance. Distinct per directory so a failure says WHICH was lost.
SENTINEL_DATA = b'{"ledger":"do-not-touch","seq":99}'
SENTINEL_LOG = b"live bot log line\n"
SENTINEL_CONF = b"connector: do-not-touch\n"


# ---------------------------------------------------------------------------
# Filesystem / service builders
# ---------------------------------------------------------------------------

def _write_source_instance(bots: Path, name: str):
    src = bots / "instances" / name
    (src / "data").mkdir(parents=True, exist_ok=True)
    (src / "data" / LEDGER_NAME).write_bytes(LEDGER_BYTES)
    (src / "data" / f"{LEDGER_NAME}.owner").write_bytes(OWNER_BYTES)
    (src / "conf" / "controllers").mkdir(parents=True, exist_ok=True)
    (src / "conf" / "controllers" / CONTROLLER_FILE).write_text(
        yaml.safe_dump(TEMPLATE_CFG), encoding="utf-8"
    )
    return src


@pytest.fixture
def bots_tree(tmp_path, monkeypatch):
    """A real ``bots/`` tree under a chdir'd tmp_path (the service resolves
    "bots" relative to CWD), holding a credentials profile, the shared
    script/controller templates, and a stopped source instance."""
    monkeypatch.chdir(tmp_path)
    bots = tmp_path / "bots"

    creds = bots / "credentials" / "master_account"
    creds.mkdir(parents=True)
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

    _write_source_instance(bots, SRC_NAME)
    return bots


@pytest.fixture
def patched_security(monkeypatch, tmp_path):
    from config import settings

    monkeypatch.setattr(settings.security, "config_password", CONFIG_PASSWORD)
    monkeypatch.setattr("services.docker_service.ensure_gateway_certs", MagicMock())
    monkeypatch.setattr(
        "services.docker_service.gateway_certs_dir",
        MagicMock(return_value=str(tmp_path / "certs")),
    )


def make_docker_client(*, src_status="exited", src_not_found=True):
    """The Docker daemon — the one unavoidable external. ``containers.run`` is a
    spy that must never be called on an aborted deploy."""
    client = MagicMock()
    if src_not_found:
        client.containers.get.side_effect = NotFound("no such container")
    else:
        container = MagicMock()
        container.status = src_status
        client.containers.get.return_value = container
    return client


def make_service(client, db_manager=None):
    """A DockerService whose __init__ (docker.from_env + cleanup thread) is
    bypassed. Only construction is faked; create_hummingbot_instance — the unit
    under test — runs for real."""
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
        resume_accept_ungraceful=True,
    )
    kwargs.update(overrides)
    return V2ControllerDeployment(**kwargs)


_RETIREMENT_TS = "2026-07-10T12:00:00+00:00"
_VERIFIED_EVIDENCE_JSON = json.dumps({
    **{k: _RETIREMENT_TS for k in REQUIRED_RETIREMENT_EVIDENCE},
    "skip_order_cancellation": False,
    "cancellation_requested_at": _RETIREMENT_TS,
})


def graceful_repo(*names):
    """A bot_run_repo whose lineage rows are all gracefully stopped
    (STOPPED + VERIFIED retirement with full evidence — CDX-005/CDX-R04)."""
    repo = MagicMock()
    repo.get_bot_runs = AsyncMock(
        return_value=[
            SimpleNamespace(
                instance_name=n,
                run_status="STOPPED",
                stopped_at=datetime(2026, 7, 10, 12, 0, 0),
                retirement_status="VERIFIED",
                retirement_evidence=_VERIFIED_EVIDENCE_JSON,
            )
            for n in names
        ]
    )
    return repo


def make_db_manager(*names):
    """``(db_manager, repo)`` for the deploy path, which opens its own session
    and constructs ``BotRunRepository`` itself — callers patch that name to hand
    back this repo (mirrors production ``seed_resume_state``)."""

    @asynccontextmanager
    async def session_ctx():
        yield MagicMock()

    db_manager = MagicMock()
    db_manager.get_session_context = session_ctx
    return db_manager, graceful_repo(*names)


def _plant_existing_target(bots: Path, name=NEW_NAME) -> Path:
    """A pre-existing instance dir at the deploy's target name, carrying one
    sentinel per subtree that the pre-fix code destroyed."""
    target = bots / "instances" / name
    (target / "data").mkdir(parents=True)
    (target / "logs").mkdir(parents=True)
    (target / "conf").mkdir(parents=True)
    (target / "data" / "sentinel.json").write_bytes(SENTINEL_DATA)
    (target / "logs" / "sentinel.log").write_bytes(SENTINEL_LOG)
    (target / "conf" / "sentinel.yml").write_bytes(SENTINEL_CONF)
    return target


def _snapshot(root: Path) -> dict:
    """Every path under ``root`` -> its bytes (None for dirs). Compared before
    and after a read-only operation to prove nothing was created, deleted or
    rewritten."""
    snap = {}
    for path in sorted(root.rglob("*")):
        snap[str(path.relative_to(root))] = None if path.is_dir() else path.read_bytes()
    return snap


# ===========================================================================
# (a) An existing target is never reused and never deleted
# ===========================================================================

class TestExclusiveTargetCreation:
    @pytest.mark.asyncio
    async def test_existing_target_refused_and_sentinels_survive(self, bots_tree, patched_security):
        """Spec 1: an existing target dir -> ResumeError/409 DEST_EXISTS, and the
        directory is left byte-identical.

        Catches (mutation): restoring the reuse path
        ``if not os.path.exists(instance_dir): os.makedirs(...)`` at
        docker_service.py:220 — the deploy would then adopt the existing tree and
        the rmtree/recopy of conf/ would eat the conf sentinel.
        """
        target = _plant_existing_target(bots_tree)
        before = _snapshot(target)
        client = make_docker_client()
        service = make_service(client)

        with pytest.raises(ResumeError) as exc:
            await service.create_hummingbot_instance(make_deployment())

        assert exc.value.reason is ResumeAbortReason.DEST_EXISTS
        # No container may start for a refused deploy.
        client.containers.run.assert_not_called()
        # The operator's bytes are all still there, unchanged.
        assert _snapshot(target) == before
        assert (target / "data" / "sentinel.json").read_bytes() == SENTINEL_DATA
        assert (target / "logs" / "sentinel.log").read_bytes() == SENTINEL_LOG
        assert (target / "conf" / "sentinel.yml").read_bytes() == SENTINEL_CONF

    @pytest.mark.asyncio
    async def test_existing_target_refused_for_non_resume_deploys_too(
        self, bots_tree, patched_security
    ):
        """Exclusive creation is not a resume feature. A plain deploy
        (``resume_mode="off"``, which never invokes the hook) must be refused on
        an existing target just the same — the pre-fix reuse path destroyed
        operator data regardless of resume mode.

        Catches (mutation): moving ``guard_target_available(instance_dir)``
        inside the ``if config.resume_mode != "off"`` branch.
        """
        target = _plant_existing_target(bots_tree)
        before = _snapshot(target)
        client = make_docker_client()
        service = make_service(client)

        with pytest.raises(ResumeError) as exc:
            await service.create_hummingbot_instance(
                make_deployment(resume_mode="off", resume_from=None,
                                resume_accept_ungraceful=False)
            )

        assert exc.value.reason is ResumeAbortReason.DEST_EXISTS
        client.containers.run.assert_not_called()
        assert _snapshot(target) == before

    @pytest.mark.asyncio
    async def test_refusal_does_no_work_at_all(self, bots_tree, patched_security):
        """The refusal happens BEFORE any work: no staging dir, and the resume
        hook never runs.

        The no-work property is what distinguishes the up-front guard from the
        promote-time check — both refuse, but only the guard refuses for free.
        A ``containers.get`` for the SOURCE instance is the observable: the
        hook's source-container guard is the first thing a seed does, so an
        un-guarded deploy would stage the whole instance and seed it before the
        promote threw it away.

        Phase 6 (CLA-M02) made ``containers.get`` no longer unique to the hook —
        the deploy now also inspects the API's OWN container to prove the bots/
        path coupling, which is a read-only query and not "work" in the sense
        this test means. So the assertion names the call it actually cares
        about (get(SOURCE)) instead of asserting the method was never called at
        all; the mutation strength is unchanged, since a seed that ran would
        still query the source.

        Catches (mutation): deleting ``guard_target_available(instance_dir)`` from
        ``create_hummingbot_instance``, or moving it after
        ``_create_staging_dir``.
        """
        _plant_existing_target(bots_tree)
        client = make_docker_client()
        service = make_service(client)

        with pytest.raises(ResumeError) as exc:
            await service.create_hummingbot_instance(make_deployment())

        assert exc.value.reason is ResumeAbortReason.DEST_EXISTS
        source_lookups = [
            call for call in client.containers.get.call_args_list
            if call.args and call.args[0] == SRC_NAME
        ]
        assert source_lookups == [], "the resume hook ran: it guarded the source container"
        # Nothing was staged, nothing was seeded.
        assert list((bots_tree / "instances").glob("*.staging-*")) == []

    @pytest.mark.asyncio
    async def test_clean_deploy_promotes_target_and_seeds(self, bots_tree, patched_security):
        """The happy path still works end to end: the instance lands at its real
        name (not the staging name), seeded, with no staging dir left over.

        Catches: a promote that never renames, or one that leaves the instance at
        its staging path.
        """
        client = make_docker_client()
        service = make_service(client)

        response = await service.create_hummingbot_instance(make_deployment())

        assert response["success"] is True
        client.containers.run.assert_called_once()
        target = bots_tree / "instances" / NEW_NAME
        assert (target / "data" / LEDGER_NAME).read_bytes() == LEDGER_BYTES
        assert list((bots_tree / "instances").glob("*.staging-*")) == []
        # The client config carries the instance's real identity, not the
        # staging dir's name (mutation: write the staging basename here).
        client_cfg = yaml.safe_load((target / "conf" / "conf_client.yml").read_text(encoding="utf-8"))
        assert client_cfg["instance_id"] == NEW_NAME


# ===========================================================================
# (b) Failure cleans up only what this attempt created
# ===========================================================================

class TestFailureCleanupScope:
    @pytest.mark.asyncio
    async def test_mid_seed_failure_removes_staging_and_spares_siblings(
        self, bots_tree, patched_security
    ):
        """Spec 6b: a ResumeError raised mid-seed (after copying began) removes
        the staging dir and touches no pre-existing sibling.

        Catches: dropping the ``except BaseException: self._remove_staging_dir``
        arm, or widening cleanup beyond the staging path.
        """
        # An unrelated neighbour instance that must survive untouched.
        neighbour = bots_tree / "instances" / "NEIGHBOUR-20260101-000000"
        (neighbour / "data").mkdir(parents=True)
        (neighbour / "data" / "neighbour.json").write_bytes(b'{"keep":"me"}')
        instances_before = _snapshot(bots_tree / "instances")

        real_copy2 = shutil.copy2

        def failing_copy2(src_path, dst, *args, **kwargs):
            # Fail only the hook's copies into data/, i.e. mid-seed, after conf
            # staging has already written into the staging dir.
            if "data" in Path(dst).parts:
                raise OSError("disk full")
            return real_copy2(src_path, dst, *args, **kwargs)

        client = make_docker_client()
        service = make_service(client)

        with patch("services.resume_service.shutil.copy2", side_effect=failing_copy2):
            with pytest.raises(ResumeError) as exc:
                await service.create_hummingbot_instance(make_deployment())

        assert exc.value.reason is ResumeAbortReason.COPY_IO_ERROR
        client.containers.run.assert_not_called()
        # The staging dir this attempt created is gone...
        assert list((bots_tree / "instances").glob("*.staging-*")) == []
        # ...the target was never created...
        assert not (bots_tree / "instances" / NEW_NAME).exists()
        # ...and every pre-existing sibling is byte-identical.
        assert _snapshot(bots_tree / "instances") == instances_before

    @pytest.mark.asyncio
    async def test_cleanup_refuses_dir_this_attempt_did_not_create(
        self, bots_tree, patched_security, caplog
    ):
        """Spec 2: ``_cleanup_failed_instance`` may remove ONLY the path this
        attempt created. Default-deny: without an ownership assertion, a failing
        seed must not delete the directory it was handed.

        Catches (mutation): reverting ``_cleanup_failed_instance`` to the
        unconditional ``if new_instance_dir.exists(): shutil.rmtree(...)`` body —
        the pre-fix code that deleted operator data.
        """
        inst = bots_tree / "instances" / NEW_NAME
        (inst / "data").mkdir(parents=True)
        (inst / "data" / "sentinel.json").write_bytes(SENTINEL_DATA)

        client = MagicMock()
        client.containers.get.side_effect = RuntimeError("docker daemon exploded")

        with pytest.raises(RuntimeError):
            await seed_resume_state(
                deployment=make_deployment(),
                new_instance_dir=inst,
                bots_path=bots_tree,
                docker_client=client,
                bot_run_repo=graceful_repo(SRC_NAME),
                # created_by_this_attempt omitted -> defaults False -> keep it.
            )

        assert inst.exists(), "a directory this attempt did not create was deleted"
        assert (inst / "data" / "sentinel.json").read_bytes() == SENTINEL_DATA


# ===========================================================================
# (c) Name uniqueness — and the parser that must keep up with it
# ===========================================================================

class TestInstanceNameUniqueness:
    def test_two_names_in_the_same_microsecond_differ(self):
        """Spec 3: a frozen clock must still yield distinct names — uniqueness
        cannot rest on the clock (two workers can read one microsecond).

        Catches (mutation): dropping the random component from
        ``generate_instance_name`` (returning just base+stamp+micros).
        """
        frozen = datetime(2026, 7, 16, 14, 30, 22, 123456)
        names = {generate_instance_name("LADDER_BOT", now=frozen) for _ in range(50)}
        assert len(names) == 50

    def test_generated_name_round_trips_to_base_and_timestamp(self):
        """The generator/parser invariant: every generated name must strip back to
        its base and parse back to its clock. If it does not, ``latest`` silently
        stops finding lineage — the deploy's own name would no longer match its
        own base.

        Catches (mutation): changing the name format without the regex (e.g.
        separating the random suffix with '_' instead of '-').
        """
        frozen = datetime(2026, 7, 16, 14, 30, 22, 123456)
        name = generate_instance_name("LADDER_BOT", now=frozen)
        assert _strip_api_suffix(name) == "LADDER_BOT"
        assert _parse_api_timestamp(name) == frozen

    def test_operator_embedded_timestamp_is_not_double_stripped(self):
        """R2: an operator name that embeds its own timestamp-like token keeps it
        (only the API's own suffix comes off, once).

        Catches: un-anchoring the suffix regex or applying it repeatedly.
        """
        frozen = datetime(2026, 7, 16, 14, 30, 22, 123456)
        name = generate_instance_name("KRAKEN_LADDER_V1-20260712-2302", now=frozen)
        assert _strip_api_suffix(name) == "KRAKEN_LADDER_V1-20260712-2302"

    def test_legacy_second_granular_names_still_parse(self):
        """Backwards compatibility: instances deployed under the legacy
        ``<base>-YYYYMMDD-HHMMSS`` format are already on disk and in bot_runs.
        They must keep resolving as lineage, or this fix strands every existing
        instance.

        Catches (mutation): making the sub-second group mandatory in
        ``_API_SUFFIX_RE``.
        """
        assert _strip_api_suffix("MYBOT-20260101-000000") == "MYBOT"
        assert _parse_api_timestamp("MYBOT-20260101-000000") == datetime(2026, 1, 1, 0, 0, 0)

    def test_sub_second_names_order_strictly(self):
        """Two deploys inside one second must be strictly orderable, or `latest`
        calls the pair a tie and aborts LATEST_AMBIGUOUS.

        Catches (mutation): dropping the ``microsecond`` fold in
        ``_parse_api_timestamp``.
        """
        base = datetime(2026, 7, 16, 14, 30, 22)
        earlier = generate_instance_name("BOT", now=base.replace(microsecond=1))
        later = generate_instance_name("BOT", now=base.replace(microsecond=999999))
        assert _parse_api_timestamp(earlier) < _parse_api_timestamp(later)

    @pytest.mark.asyncio
    async def test_latest_resolves_through_a_staged_deploy(self, bots_tree, patched_security):
        """Identity comes from the instance name, never the staging dir's name.

        A `latest` deploy must find its lineage even though the build happens at
        ``<name>.staging-<rand>``.

        Catches (mutation): dropping ``new_instance_name`` and letting ``_seed``
        fall back to ``new_instance_dir.name`` — `latest` would strip the base
        from the staging name, match nothing, and abort SOURCE_NOT_FOUND.
        """
        # The deploy names itself exactly as the router does.
        unique_name = generate_instance_name("LADDER_BOT")
        deployment = make_deployment(
            instance_name=unique_name, resume_mode="latest", resume_from=None
        )
        db_manager, repo = make_db_manager(SRC_NAME)
        service = make_service(make_docker_client(), db_manager=db_manager)

        with patch("database.BotRunRepository", return_value=repo):
            response = await service.create_hummingbot_instance(deployment)

        assert response["success"] is True
        target = bots_tree / "instances" / unique_name
        # `latest` resolved SRC_NAME as lineage and carried its ledger forward —
        # which only works if the base was stripped from the INSTANCE name.
        assert (target / "data" / LEDGER_NAME).read_bytes() == LEDGER_BYTES
        manifest = json.loads((target / "data" / "resume.manifest.json").read_text(encoding="utf-8"))
        assert manifest["source_instance"] == SRC_NAME


# ===========================================================================
# (d) Preview validates the REAL target and mutates nothing
# ===========================================================================

class TestPreviewRealTargetPath:
    @pytest.mark.asyncio
    async def test_preview_reports_real_target_path_and_mutates_nothing(
        self, bots_tree, patched_security
    ):
        """Spec 5: the preview reports the resolved target path (under the real
        bots/instances/ tree) and its planned destinations, without creating or
        mutating anything.

        Catches (mutation): reporting ``item.dst`` unmodified — the temp staging
        path the plan was computed in, which will not exist a moment later.
        """
        before = _snapshot(bots_tree)

        result = await preview_resume(
            deployment=make_deployment(instance_name="LADDER_BOT"),
            bots_path=bots_tree,
            docker_client=make_docker_client(),
            bot_run_repo=graceful_repo(SRC_NAME),
        )

        target = result["target"]
        assert target["base_name"] == "LADDER_BOT"
        # The reported path is the real one the deploy would build.
        assert Path(target["path"]) == resolve_deploy_target(bots_tree, target["instance_name"])
        assert Path(target["path"]).parent == bots_tree / "instances"
        # Honest about what it cannot do: reserve the name.
        assert target["name_is_representative"] is True
        # Planned destinations sit under the real target, not a temp dir.
        dsts = [Path(f["dst"]) for f in result["files"]]
        assert dsts, "preview planned no files"
        for dst in dsts:
            assert dst.is_relative_to(Path(target["path"]) / "data")
        # Read-only: not one byte moved.
        assert _snapshot(bots_tree) == before

    @pytest.mark.asyncio
    async def test_preview_aborts_when_the_real_target_exists(self, bots_tree, patched_security):
        """The preview's destination guard runs against REAL filesystem state.

        The deploy mints a random name, so a natural collision is unreachable;
        pinning the generator (the entropy source, not the unit under test) is
        the only way to place a real directory at the candidate path. That is the
        whole point of P1: with the pre-fix code this guard graded an always-empty
        temp dir and could never fail.

        Catches (mutation): passing ``tmp_instance_dir / "data"`` back to
        ``run_guards``, or dropping the ``target_dir=`` argument.
        """
        pinned = "LADDER_BOT-20260716-143022-123456-abcdef"
        _plant_existing_target(bots_tree, name=pinned)
        before = _snapshot(bots_tree)

        with patch(
            "services.resume_service.generate_instance_name", return_value=pinned
        ):
            with pytest.raises(ResumeError) as exc:
                await preview_resume(
                    deployment=make_deployment(instance_name="LADDER_BOT"),
                    bots_path=bots_tree,
                    docker_client=make_docker_client(),
                    bot_run_repo=graceful_repo(SRC_NAME),
                )

        assert exc.value.reason is ResumeAbortReason.DEST_EXISTS
        # The partial report travels with the error, naming the failed check and
        # the real path it failed on. (A failed guard is recorded under the abort
        # REASON, per _abort_guard/resume_service.py:1060 — the same convention
        # every other guard follows.)
        report = exc.value.guard_report.to_dict()
        assert report["passed"] is False
        failed = next(c for c in report["checks"] if c["name"] == "dest_exists")
        assert failed["passed"] is False
        assert str(bots_tree / "instances" / pinned) in failed["message"]
        # A refusing preview still mutates nothing.
        assert _snapshot(bots_tree) == before

    @pytest.mark.asyncio
    async def test_preview_warns_when_bare_base_name_collides(self, bots_tree, patched_security):
        """A directory at the operator's bare base name is real, observable state
        the preview surfaces as a warning — the deploy appends a unique suffix, so
        it is not fatal and must not abort.

        Catches (mutation): deleting the ``_preview_base_name_collision`` call, or
        promoting it to an abort.
        """
        _plant_existing_target(bots_tree, name="LADDER_BOT")
        before = _snapshot(bots_tree)

        result = await preview_resume(
            deployment=make_deployment(instance_name="LADDER_BOT"),
            bots_path=bots_tree,
            docker_client=make_docker_client(),
            bot_run_repo=graceful_repo(SRC_NAME),
        )

        assert result["would_succeed"] is True
        warnings = result["guard_report"]["warnings"]
        assert any("LADDER_BOT" in w and "already exists" in w for w in warnings), warnings
        assert _snapshot(bots_tree) == before

    @pytest.mark.asyncio
    async def test_preview_guard_report_includes_target_check(self, bots_tree, patched_security):
        """Spec 5: per-guard results are in the response, including the §7.0
        exclusive-creation check the deploy will run.

        Catches: dropping ``target_dir=`` so the guard never runs in preview.
        """
        result = await preview_resume(
            deployment=make_deployment(instance_name="LADDER_BOT"),
            bots_path=bots_tree,
            docker_client=make_docker_client(),
            bot_run_repo=graceful_repo(SRC_NAME),
        )

        checks = {c["name"]: c for c in result["guard_report"]["checks"]}
        assert checks["target_available"]["passed"] is True
        assert result["target"]["instance_name"] in checks["target_available"]["message"]


# ===========================================================================
# (e) Concurrency: exactly one deploy of a name proceeds
# ===========================================================================

class TestConcurrentDeploys:
    @pytest.mark.asyncio
    async def test_two_concurrent_deploys_of_one_name_exactly_one_proceeds(
        self, bots_tree, patched_security
    ):
        """Spec 6e: exactly one of two concurrent same-name deploys proceeds; the
        other is refused DEST_EXISTS and leaves no staging dir.

        NOTE (honesty): this proves exclusive-create + atomic promote, NOT the
        lock — without the lock the loser would still fail at promote. The lock's
        own guarantee is proven in TestDeployLockRegistry below.
        """
        service = make_service(make_docker_client())

        results = await asyncio.gather(
            service.create_hummingbot_instance(make_deployment()),
            service.create_hummingbot_instance(make_deployment()),
            return_exceptions=True,
        )

        succeeded = [r for r in results if isinstance(r, dict) and r.get("success")]
        refused = [r for r in results if isinstance(r, ResumeError)]
        assert len(succeeded) == 1, results
        assert len(refused) == 1, results
        assert refused[0].reason is ResumeAbortReason.DEST_EXISTS
        # The winner's instance is intact and the loser cleaned up after itself.
        assert (bots_tree / "instances" / NEW_NAME / "data" / LEDGER_NAME).read_bytes() == LEDGER_BYTES
        assert list((bots_tree / "instances").glob("*.staging-*")) == []


class TestDeployLockRegistry:
    @pytest.mark.asyncio
    async def test_same_name_deploys_serialize(self):
        """Spec 4: the per-instance lock serialises same-name deploys.

        Catches (mutation): returning a fresh ``asyncio.Lock()`` per call instead
        of ``setdefault`` — every worker would enter the critical section at the
        yield point and max_overlap would be 5.
        """
        from services.docker_service import _instance_deploy_lock

        overlap = 0
        max_overlap = 0

        async def worker():
            nonlocal overlap, max_overlap
            async with _instance_deploy_lock("SAME_NAME"):
                overlap += 1
                max_overlap = max(max_overlap, overlap)
                await asyncio.sleep(0)  # an unserialised impl interleaves here
                overlap -= 1

        await asyncio.gather(*[worker() for _ in range(5)])
        assert max_overlap == 1

    @pytest.mark.asyncio
    async def test_different_names_do_not_block_each_other(self):
        """Deploys of different names stay concurrent.

        Catches (mutation): collapsing the registry to one global lock — B could
        not acquire while A holds, and this would time out.
        """
        from services.docker_service import _instance_deploy_lock

        a_holds = asyncio.Event()
        release_a = asyncio.Event()

        async def hold_a():
            async with _instance_deploy_lock("NAME_A"):
                a_holds.set()
                await release_a.wait()

        task_a = asyncio.create_task(hold_a())
        await a_holds.wait()

        async def acquire_b():
            async with _instance_deploy_lock("NAME_B"):
                return True

        assert await asyncio.wait_for(acquire_b(), timeout=2.0) is True
        release_a.set()
        await task_a

    @pytest.mark.asyncio
    async def test_lock_registry_does_not_grow_unbounded(self):
        """The registry is refcounted: a long-lived API deploying many uniquely
        named instances must not accumulate a lock per name forever.

        Catches (mutation): dropping the ``finally`` cleanup.
        """
        from services.docker_service import _deploy_lock_users, _deploy_locks, _instance_deploy_lock

        for i in range(10):
            async with _instance_deploy_lock(f"EPHEMERAL_{i}"):
                pass

        assert [k for k in _deploy_locks if k.startswith("EPHEMERAL_")] == []
        assert [k for k in _deploy_lock_users if k.startswith("EPHEMERAL_")] == []


# ===========================================================================
# CDX-R01: the promote primitive must be no-replace on POSIX too
# ===========================================================================

def _posix_rename(src, dst):
    """``os.rename`` with POSIX ``rename(2)`` DIRECTORY semantics, modelled from
    the spec (SUSv4 rename(): if the "new" argument points to an existing empty
    directory, it shall be removed and "old" renamed to "new"; ENOTEMPTY
    otherwise).

    This models the kernel, not the unit under test. It is here because the
    production platform is Linux while this suite runs on Windows, whose
    ``os.rename`` is natively no-replace and would therefore mask the exact
    defect CDX-R01 is about. The reservation logic being graded is real
    production code; only rename(2)'s semantics are supplied.
    """
    if os.path.isdir(dst):
        if os.listdir(dst):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", dst)
        os.rmdir(dst)
    os.rename(src, dst)


class TestPromoteIsNoReplace:
    """CDX-R01: an ``exists()``-then-``rename()`` promote is not exclusive on
    POSIX — rename(2) completes over an empty destination that appeared in the
    window. The fix makes the primitive itself the gate (``os.mkdir`` reserves
    atomically), so there is no window to lose.
    """

    @pytest.fixture
    def posix_promote(self, monkeypatch):
        """Force the POSIX branch and give it POSIX rename(2) semantics."""
        monkeypatch.setattr(docker_service, "_NATIVE_NOREPLACE_RENAME", False)
        monkeypatch.setattr(docker_service, "_rename", _posix_rename)

    @staticmethod
    def _staging(tmp_path):
        staging = tmp_path / "INST.staging-deadbeef"
        (staging / "data").mkdir(parents=True)
        (staging / "data" / LEDGER_NAME).write_bytes(LEDGER_BYTES)
        return staging

    def test_rename_noreplace_refuses_an_empty_existing_target(self, tmp_path, posix_promote):
        """The CDX-R01 case: a bare POSIX rename ABSORBS an empty destination
        directory. The reservation must refuse it instead, moving nothing.

        Catches (mutation): drop the ``os.mkdir(dst)`` reservation in
        ``_rename_noreplace`` and call ``_rename(src, dst)`` directly — the
        POSIX semantics above then remove the empty target and the promote
        succeeds, so no FileExistsError is raised and this fails.
        """
        staging = self._staging(tmp_path)
        target = tmp_path / "INST"
        target.mkdir()  # the empty dir a bare POSIX rename would silently absorb

        with pytest.raises(FileExistsError):
            docker_service._rename_noreplace(str(staging), str(target))

        # Nothing moved, nothing removed.
        assert target.is_dir()
        assert list(target.iterdir()) == []
        assert (staging / "data" / LEDGER_NAME).read_bytes() == LEDGER_BYTES

    def test_rename_noreplace_refuses_a_populated_existing_target(self, tmp_path, posix_promote):
        """A populated target is refused with the same error and survives
        byte-identical — the operator's data is never the thing that decides
        whether the promote is safe."""
        staging = self._staging(tmp_path)
        target = tmp_path / "INST"
        (target / "data").mkdir(parents=True)
        (target / "data" / "sentinel.json").write_bytes(SENTINEL_DATA)

        with pytest.raises(FileExistsError):
            docker_service._rename_noreplace(str(staging), str(target))

        assert (target / "data" / "sentinel.json").read_bytes() == SENTINEL_DATA

    def test_rename_noreplace_promotes_onto_a_free_target(self, tmp_path, posix_promote):
        """The reservation must not break the normal promote: a free target gets
        the fully-built staging tree, and the staging path is gone."""
        staging = self._staging(tmp_path)
        target = tmp_path / "INST"

        docker_service._rename_noreplace(str(staging), str(target))

        assert (target / "data" / LEDGER_NAME).read_bytes() == LEDGER_BYTES
        assert not staging.exists()

    def test_rename_noreplace_releases_its_reservation_when_the_rename_fails(
        self, tmp_path, monkeypatch
    ):
        """An unrelated rename failure must not leave our empty reservation
        squatting on the target name.

        Catches (mutation): delete the ``os.rmdir(dst)`` release in the
        ``except OSError`` arm — the target name stays occupied by an empty dir
        and every later deploy of that name is refused DEST_EXISTS forever.
        """
        staging = self._staging(tmp_path)
        target = tmp_path / "INST"

        def boom(src, dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link", src)

        monkeypatch.setattr(docker_service, "_NATIVE_NOREPLACE_RENAME", False)
        monkeypatch.setattr(docker_service, "_rename", boom)

        with pytest.raises(OSError) as err:
            docker_service._rename_noreplace(str(staging), str(target))

        assert err.value.errno == errno.EXDEV  # the real cause surfaces, not a mask
        assert not target.exists(), "the reservation was left squatting on the target name"

    def test_promote_maps_a_racing_target_to_dest_exists(self, tmp_path, posix_promote):
        """End of the chain: the primitive's refusal reaches the caller as the
        DEST_EXISTS ResumeError the deploy path maps to 409, with both the
        target and this attempt's staging dir intact for the caller to clean.
        """
        staging = self._staging(tmp_path)
        target = tmp_path / "INST"
        target.mkdir()

        with pytest.raises(ResumeError) as err:
            DockerService._promote_staging(str(staging), str(target))

        assert err.value.reason is ResumeAbortReason.DEST_EXISTS
        assert target.is_dir() and list(target.iterdir()) == []
        assert (staging / "data" / LEDGER_NAME).read_bytes() == LEDGER_BYTES


# ===========================================================================
# CDX-R03: a failure building out the staging dir must not leak it
# ===========================================================================

class TestStagingConstructionFailure:
    @pytest.mark.asyncio
    async def test_failure_creating_staging_children_leaks_nothing(
        self, bots_tree, patched_security, monkeypatch
    ):
        """Staging root created, then ``data/`` creation fails: the root must go.

        The caller's ``except`` only arms once ``_create_staging_dir`` RETURNS,
        so a failure inside the constructor bypasses it and every retry strands
        another ``<target>.staging-*`` tree (CDX-R03).

        Catches (mutation): remove the ``try/except BaseException`` around the
        data/logs makedirs in ``_create_staging_dir`` — the root survives and
        the glob below is non-empty.
        """
        service = make_service(make_docker_client())
        real_makedirs = os.makedirs

        def failing_makedirs(path, *args, **kwargs):
            if os.path.basename(str(path)) == "data" and ".staging-" in str(path):
                raise OSError(errno.EACCES, "forced staging setup failure", str(path))
            return real_makedirs(path, *args, **kwargs)

        monkeypatch.setattr(os, "makedirs", failing_makedirs)

        with pytest.raises(OSError) as err:
            await service.create_hummingbot_instance(make_deployment())

        assert "forced staging setup failure" in str(err.value)
        assert list((bots_tree / "instances").glob("*.staging-*")) == []
        # And the failure did not conjure the target either.
        assert not (bots_tree / "instances" / NEW_NAME).exists()


# ===========================================================================
# CDX-R02: deploy resolves its target through the SHARED resolver
# ===========================================================================

class TestDeployUsesSharedTargetResolver:
    @pytest.mark.asyncio
    async def test_deploy_builds_where_resolve_deploy_target_says(
        self, bots_tree, patched_security, monkeypatch
    ):
        """``resolve_deploy_target`` is documented as the single source of truth
        for the instance path, shared by preview and deploy. Deploy used to
        re-join the path itself, so the two could drift and preview would be
        grading a path deploy does not build (CDX-R02).

        Redirecting the shared resolver must therefore move the deploy's output.

        Catches (mutation): restore
        ``instance_dir = os.path.join("bots", 'instances', instance_name)`` in
        ``create_hummingbot_instance`` — the deploy ignores the redirect, builds
        at the default path, and both assertions below fail.
        """
        service = make_service(make_docker_client())
        redirected = Path("bots") / "instances" / "REDIRECTED_BY_RESOLVER"
        monkeypatch.setattr(
            docker_service, "resolve_deploy_target", lambda bots_path, name: redirected
        )

        result = await service.create_hummingbot_instance(make_deployment())

        assert result["success"]
        assert (bots_tree / "instances" / "REDIRECTED_BY_RESOLVER" / "data" / LEDGER_NAME).exists()
        assert not (bots_tree / "instances" / NEW_NAME).exists()


# ===========================================================================
# CDX-R04: the per-name lock is actually held across the deploy
# ===========================================================================

class TestDeployLockIntegration:
    @pytest.mark.asyncio
    async def test_second_same_name_deploy_cannot_enter_staging_while_first_holds_lock(
        self, bots_tree, patched_security, monkeypatch
    ):
        """CDX-R04 — the lock's own guarantee, proven on the deploy path.

        ``TestConcurrentDeploys`` and ``TestDeployLockRegistry`` both pass with
        the lock keyed uniquely per call (i.e. serialisation disabled): the
        former is satisfied by exclusive-create + atomic promote alone, the
        latter exercises the helper in isolation. Neither reaches
        ``create_hummingbot_instance``'s use of it. This one hooks the real
        staging step and asserts the second deploy never enters it.

        Catches (mutation): key the lock uniquely per call, e.g.
        ``_instance_deploy_lock(f"{instance_name}-{secrets.token_hex(4)}")`` in
        ``create_hummingbot_instance`` — B then acquires immediately, passes the
        target guard (A has not promoted yet) and enters staging while A is
        parked, so ``stage_entries`` holds two names and this fails.
        """
        service = make_service(make_docker_client())

        a_in_staging = asyncio.Event()
        release_a = asyncio.Event()
        stage_entries = []
        real_stage = service._stage_instance

        async def watched_stage(config, staging_dir, instance_name, source_credentials_dir):
            stage_entries.append(instance_name)
            if len(stage_entries) == 1:
                a_in_staging.set()
                await release_a.wait()
            return await real_stage(config, staging_dir, instance_name, source_credentials_dir)

        monkeypatch.setattr(service, "_stage_instance", watched_stage)

        task_a = asyncio.create_task(service.create_hummingbot_instance(make_deployment()))
        await asyncio.wait_for(a_in_staging.wait(), timeout=5.0)

        task_b = asyncio.create_task(service.create_hummingbot_instance(make_deployment()))
        # Give B every scheduling opportunity to get in. Serialised, it is parked
        # at the name lock and has not run the guard, created staging, or staged.
        for _ in range(50):
            await asyncio.sleep(0)

        assert stage_entries == [NEW_NAME], (
            "a second deploy of the same name entered staging while the first held "
            f"the lock — create+seed+promote is not serialised: {stage_entries}"
        )

        release_a.set()
        result_a = await asyncio.wait_for(task_a, timeout=5.0)
        with pytest.raises(ResumeError) as err:
            await asyncio.wait_for(task_b, timeout=5.0)

        assert result_a["success"]
        # B was refused at the target guard, downstream of the lock and upstream
        # of any work: it never staged, and it left nothing behind.
        assert err.value.reason is ResumeAbortReason.DEST_EXISTS
        assert stage_entries == [NEW_NAME]
        assert list((bots_tree / "instances").glob("*.staging-*")) == []
        assert (bots_tree / "instances" / NEW_NAME / "data" / LEDGER_NAME).read_bytes() == LEDGER_BYTES
