"""Phase 6 tests: CLA-M02 path-coupling assertion + CLA-M01 drift surfacing.

**CLA-M02** — ``docker_service`` addresses one bots tree by two names and never
checked they agree: the resume hook writes through a CWD-relative ``bots/``
(``/hummingbot-api/bots`` in the API container) while ``_run_instance_container``
builds the bot containers' bind-mount sources from ``$BOTS_PATH`` (a HOST path).
The deployment only works because the compose file happens to bind
``${BOTS_PATH}/bots`` there. Break that and the hook seeds a ``data/`` the bot
can never read — it starts clean and re-seeds from the wallet, silently. The fix
proves the coupling at deploy time by inspecting the API's own container mounts:
proven mismatch → abort (409); unverifiable (not in a container) → structured
warning on the response, because bricking every dev deploy over assertion
machinery is not fail-closed, it is just broken.

**CLA-M01** — ``_diff_controller_configs`` warned about config drift in the LOG
only. Template-wins is deliberate and unchanged; what was wrong is that an
operator learned only from a log line they never read that the resumed bot would
deploy a different amount of capital than the bot it replaced. Sizing-critical
drift is now classified and surfaced as structured warnings on BOTH the deploy
and preview responses.

Test authenticity (batch prompt §"Test authenticity"): the drift tests run the
REAL ``_diff_controller_configs`` / ``preview_resume`` / ``seed_resume_state``
over REAL ``tmp_path`` YAML files. The coupling tests mock the Docker client's
``containers.get`` — the unit under test IS the interpretation of the daemon's
mount table, and the daemon is the unavoidable external (docker is prohibited in
this batch and the API is not running in a container under pytest). No test
patches the unit under test.

Expected values come from the SPEC: field names are enumerated from the engine's
ladder config (``range_inventory_ladder.py`` :210/:218/:270/:278/:291/:310), and
the coupling verdicts from the phase-6 spec's mismatch/match/unavailable
trichotomy — never from running the implementation. Each test names, in a
comment, the single-line implementation mutation it is built to catch.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from docker.errors import APIError, NotFound

from test_copyforward_p1_exclusive_target import (  # real deploy/preview fixtures
    CONTROLLER_FILE,
    CONTROLLER_ID,
    NEW_NAME,
    SRC_NAME,
    TEMPLATE_CFG,
    bots_tree,  # noqa: F401  (pytest fixture)
    make_db_manager,
    make_deployment,
    make_docker_client,
    make_service,
    patched_security,  # noqa: F401  (pytest fixture)
)

from services import docker_service as docker_service_module
from services.docker_service import (
    _host_source_for,
    _inspect_self_mounts,
)
from services.resume_service import (
    ResumeAbortReason,
    ResumeError,
    _diff_controller_configs,
    _is_sizing_critical,
    preview_resume,
)

# ---------------------------------------------------------------------------
# CLA-M02 helpers
# ---------------------------------------------------------------------------
#
# Nothing here fakes os.path. The production shape is simply two DIFFERENT
# strings for one directory — the hook's container-side write root
# (``abspath("bots")``, i.e. /hummingbot-api/bots in the API container) and the
# host-side root the bot containers mount (``$BOTS_PATH/bots``) — and that shape
# is reproduced exactly with real tmp_path directories: ``cwd/bots`` vs
# ``$BOTS_PATH/bots``, which are genuinely different paths, just as they are in
# production. The tests read the two roots with the same os.path calls the
# implementation uses rather than hardcoding a separator style, so they assert
# the coupling rule, not this dev box's OS.


def _p(posix_path):
    """A POSIX container path spelled for whatever OS the suite runs on.

    Production is Linux, where this is the identity. On the Windows dev box it
    only fixes the separator: the CONTAINMENT relationships the assertions are
    about ("is this mount a parent of that path", "is bots-backup a sibling of
    bots") are the same either way, which is what these cases test.
    """
    return os.path.normpath(posix_path)


def mount(dest, source):
    return {"Destination": dest, "Source": source, "Type": "bind"}


def coupling_client(mounts):
    """A Docker client whose self-inspection returns ``mounts``.

    Mocking the daemon is legitimate here and is nowhere near the unit under
    test: what is tested is how the mount table is INTERPRETED, and there is no
    way to obtain a real one (docker is prohibited by this batch, and pytest
    does not run inside the API container).
    """
    client = MagicMock()
    container = MagicMock()
    container.attrs = {"Mounts": list(mounts)}
    client.containers.get.return_value = container
    return client


@pytest.fixture
def coupling_env(tmp_path, monkeypatch):
    """The production coupling, reproduced with real paths.

    ``write_root`` is what the resume hook writes through; ``mount_root`` is the
    host directory every bot bind-mount is built from. In a correct deployment
    they are the same directory reached by two names; the mount table is what
    says so.
    """
    monkeypatch.chdir(tmp_path)
    host_parent = tmp_path / "host"
    monkeypatch.setenv("BOTS_PATH", str(host_parent))
    monkeypatch.setenv("HOSTNAME", "hbapi-container-id")
    return SimpleNamespace(
        write_root=os.path.abspath("bots"),
        mount_root=os.path.abspath(os.path.join(str(host_parent), "bots")),
        cwd=os.path.abspath(os.getcwd()),
        host_parent=str(host_parent),
        elsewhere=os.path.abspath(str(tmp_path / "elsewhere" / "bots")),
    )


def coupling_service(client, cwd=None):
    service = docker_service_module.DockerService.__new__(docker_service_module.DockerService)
    service.SOURCE_PATH = cwd or os.getcwd()
    service.db_manager = None
    service._pull_status = {}
    service._cleanup_thread = None
    service.client = client
    return service


# ---------------------------------------------------------------------------
# CLA-M02 — the assertion itself
# ---------------------------------------------------------------------------

class TestCouplingVerified:
    """Match → the deploy proceeds, silently."""

    def test_exact_mount_of_write_root_matches(self, coupling_env):
        # The production compose layout: ${BOTS_PATH}/bots bound at the hook's
        # write root. Spec: "match → proceeds".
        service = coupling_service(coupling_client([
            mount(coupling_env.write_root, coupling_env.mount_root),
        ]))
        assert service._check_bots_path_coupling() is None

    def test_parent_mount_backing_the_write_root_matches(self, coupling_env):
        # Binding the PARENT (/hummingbot-api -> $BOTS_PATH) backs bots/ just as
        # validly. Calling this "unmounted" would be a false abort, so
        # _host_source_for walks the longest covering mount.
        # Mutation: require dest == write_root exactly -> this test fails.
        service = coupling_service(coupling_client([
            mount(coupling_env.cwd, coupling_env.host_parent),
        ]))
        assert service._check_bots_path_coupling() is None

    def test_longest_covering_mount_wins_over_parent(self, coupling_env):
        # A parent mount pointing somewhere ELSE must not decide the verdict when
        # a more specific mount covers the write root. If _host_source_for kept
        # the first (or shallowest) match it would abort a correct deployment.
        # Mutation: flip `depth > best_depth` to `depth < best_depth`.
        service = coupling_service(coupling_client([
            mount(coupling_env.cwd, coupling_env.elsewhere),
            mount(coupling_env.write_root, coupling_env.mount_root),
        ]))
        assert service._check_bots_path_coupling() is None

    def test_trailing_separator_is_not_a_mismatch(self, coupling_env):
        # Path spelling, not a real disagreement. Aborting here would be a false
        # positive on a correct deployment. Mutation: compare the raw strings
        # instead of normpath'd ones.
        service = coupling_service(coupling_client([
            mount(coupling_env.write_root, coupling_env.mount_root + os.sep),
        ]))
        assert service._check_bots_path_coupling() is None


class TestCouplingMismatchAborts:
    """Proven mismatch → abort. This is the finding."""

    def test_mount_source_disagrees_with_bots_path(self, coupling_env):
        # THE bug CLA-M02 describes: BOTS_PATH says the bot containers will mount
        # $BOTS_PATH/bots, but the hook's write root is actually backed by a
        # different host directory. The hook would seed a data/ no bot ever reads,
        # so the resumed bot re-seeds from the wallet — silently.
        # Mutation: delete the `normpath(actual) != normpath(mount_root)` raise.
        service = coupling_service(coupling_client([
            mount(coupling_env.write_root, coupling_env.elsewhere),
        ]))
        with pytest.raises(ResumeError) as exc:
            service._check_bots_path_coupling()
        assert exc.value.reason == ResumeAbortReason.BOTS_PATH_MISCONFIGURED
        # Spec: "abort the deploy with a clear error naming both paths".
        assert coupling_env.elsewhere in exc.value.message
        assert coupling_env.mount_root in exc.value.message
        assert coupling_env.write_root in exc.value.message

    def test_write_root_backed_by_no_mount_aborts(self, coupling_env):
        # In a container, with mounts, but none backing bots/: the hook writes to
        # the container's own writable layer while bots mount a host path. Proven
        # unreadable. Mutation: return the warning instead of raising when
        # _host_source_for yields None.
        service = coupling_service(coupling_client([
            mount(os.path.join(coupling_env.cwd, "other"), coupling_env.elsewhere),
        ]))
        with pytest.raises(ResumeError) as exc:
            service._check_bots_path_coupling()
        assert exc.value.reason == ResumeAbortReason.BOTS_PATH_MISCONFIGURED
        assert coupling_env.write_root in exc.value.message
        assert coupling_env.mount_root in exc.value.message

    def test_container_with_zero_mounts_aborts_not_warns(self, coupling_env):
        # The masquerade the reviewer is told to hunt: an EMPTY mount list is a
        # real answer ("we have no mounts"), not an unavailable one. It must not
        # be laundered into a warning. Mutation: add `if not mounts: return None`
        # to _inspect_self_mounts -> this test fails.
        service = coupling_service(coupling_client([]))
        with pytest.raises(ResumeError) as exc:
            service._check_bots_path_coupling()
        assert exc.value.reason == ResumeAbortReason.BOTS_PATH_MISCONFIGURED

    def test_bots_path_unset_inside_a_container_aborts(self, coupling_env, monkeypatch):
        # A container with BOTS_PATH unset falls back to SOURCE_PATH (cwd), so the
        # bot containers get told to mount the CONTAINER path /hummingbot-api/bots
        # as a HOST path — which does not exist on the host. A real
        # misconfiguration, and the check proves it rather than assuming the
        # fallback is fine.
        monkeypatch.delenv("BOTS_PATH")
        service = coupling_service(coupling_client([
            mount(coupling_env.write_root, coupling_env.mount_root),
        ]))
        with pytest.raises(ResumeError) as exc:
            service._check_bots_path_coupling()
        assert exc.value.reason == ResumeAbortReason.BOTS_PATH_MISCONFIGURED


class TestCouplingUnavailableWarns:
    """Unverifiable → warn, never abort. Each case is a genuinely unknowable
    answer, per the closed set enumerated on ``_inspect_self_mounts``."""

    @pytest.mark.parametrize(
        "client_factory, why",
        [
            (lambda: _client_raising(NotFound("no such container")), "not in a container"),
            (lambda: _client_raising(APIError("500 server error")), "daemon API error"),
            (lambda: _client_raising(DockerExceptionForTest("daemon down")), "daemon unreachable"),
            (lambda: _client_returning_attrs(MagicMock()), "not a real daemon (mock attrs)"),
            (lambda: _client_returning_attrs({"Mounts": "not-a-list"}), "malformed payload"),
            (lambda: _client_returning_attrs({}), "no Mounts key"),
            (lambda: None, "no docker client at all"),
        ],
    )
    def test_unavailable_self_inspection_warns_and_names_both_paths(
        self, coupling_env, client_factory, why
    ):
        # Spec: "Self-inspection unavailable (not in a container / test env) →
        # structured warning in the deploy response naming both paths".
        # Mutation: raise ResumeError instead of returning the warning -> every
        # dev/test deploy breaks and this test fails.
        service = coupling_service(client_factory())
        warning = service._check_bots_path_coupling()
        assert warning is not None, f"expected a warning when {why}"
        assert warning["code"] == "BOTS_PATH_COUPLING_UNVERIFIED"
        # Both paths, by name — an operator cannot act on "could not verify".
        assert warning["hook_write_root"] == coupling_env.write_root
        assert warning["bot_mount_root"] == coupling_env.mount_root
        assert coupling_env.write_root in warning["message"]
        assert coupling_env.mount_root in warning["message"]

    def test_unavailable_is_not_reached_by_a_comparison_error(self, coupling_env):
        # The reviewer's named risk: can a real mismatch masquerade as
        # unavailable? Only code INSIDE _inspect_self_mounts may yield
        # "unavailable"; the comparison runs outside every handler. A daemon that
        # answers with a mismatch must abort even though the client is a mock —
        # mock-ness alone never suppresses a verdict.
        client = coupling_client([mount(coupling_env.write_root, coupling_env.elsewhere)])
        service = coupling_service(client)
        with pytest.raises(ResumeError):
            service._check_bots_path_coupling()


class DockerExceptionForTest(docker_service_module.DockerException):
    pass


def _client_raising(exc):
    client = MagicMock()
    client.containers.get.side_effect = exc
    return client


def _client_returning_attrs(attrs):
    client = MagicMock()
    container = MagicMock()
    container.attrs = attrs
    client.containers.get.return_value = container
    return client


class TestSelfInspectionUnits:
    def test_empty_mount_list_is_an_answer_not_none(self, coupling_env):
        # [] and None must never be conflated: [] proves "no mounts", None means
        # "could not ask". The abort/warn split rests entirely on this.
        assert _inspect_self_mounts(coupling_client([])) == []

    def test_non_dict_mount_entries_are_unavailable(self, coupling_env):
        assert _inspect_self_mounts(_client_returning_attrs({"Mounts": ["str"]})) is None

    def test_host_source_for_uncovered_path_is_none(self):
        assert _host_source_for(
            [mount(_p("/other"), _p("/host/other"))], _p("/hummingbot-api/bots")
        ) is None

    def test_host_source_for_resolves_remainder_under_parent_mount(self):
        # A parent mount contributes its remainder: /hummingbot-api -> /h/x means
        # /hummingbot-api/bots is /h/x/bots. Getting this wrong turns a correct
        # deployment into an abort.
        assert _host_source_for(
            [mount(_p("/hummingbot-api"), _p("/h/x"))], _p("/hummingbot-api/bots")
        ) == _p("/h/x/bots")

    def test_sibling_prefix_is_not_a_covering_mount(self):
        # /hummingbot-api/bots-backup must not be read as covering
        # /hummingbot-api/bots. A pure string-prefix test would match it and
        # produce a bogus host path — and so a bogus verdict on real money.
        assert _host_source_for(
            [mount(_p("/hummingbot-api/bots-backup"), _p("/h/backup"))],
            _p("/hummingbot-api/bots"),
        ) is None


class TestCouplingInDeployPath:
    """The check must run on the REAL deploy path, not just exist as a helper."""

    @pytest.mark.asyncio
    async def test_mismatch_aborts_the_deploy_before_any_mutation(
        self, bots_tree, patched_security, monkeypatch, tmp_path
    ):
        # A proven mismatch must abort BEFORE staging: nothing built, no
        # container started. Mutation: delete the _check_bots_path_coupling()
        # call from create_hummingbot_instance -> this test fails (the deploy
        # succeeds into a tree the bot cannot read).
        monkeypatch.setenv("BOTS_PATH", str(tmp_path / "host"))
        monkeypatch.setenv("HOSTNAME", "hbapi-container-id")
        # The daemon says our bots/ is backed by a host dir that is NOT
        # $BOTS_PATH/bots.
        client = coupling_client([
            mount(os.path.abspath("bots"), os.path.abspath(str(tmp_path / "elsewhere")))
        ])
        service = make_service(client)

        with pytest.raises(ResumeError) as exc:
            await service.create_hummingbot_instance(make_deployment())

        assert exc.value.reason == ResumeAbortReason.BOTS_PATH_MISCONFIGURED
        client.containers.run.assert_not_called()
        # No staging or target directory was created.
        instances = bots_tree / "instances"
        assert sorted(p.name for p in instances.iterdir()) == [SRC_NAME]

    @pytest.mark.asyncio
    async def test_unavailable_coupling_warning_reaches_the_deploy_response(
        self, bots_tree, patched_security, monkeypatch
    ):
        # Not in a container (the pytest reality) -> the deploy SUCCEEDS and the
        # warning is on the response body. Mutation: drop the
        # `response["warnings"] = [coupling_warning]` line -> this test fails
        # while everything still deploys, which is exactly the invisible-warning
        # defect CLA-M02/M01 are about.
        monkeypatch.setenv("BOTS_PATH", str(bots_tree.parent))
        client = make_docker_client()  # containers.get raises NotFound
        db_manager, _ = make_db_manager(SRC_NAME)
        service = make_service(client, db_manager=db_manager)

        # resume_mode="off" on purpose: CLA-M02's coupling is deploy-wide, so the
        # warning must appear even on a deploy the resume hook never touches.
        with patch("services.resume_service.BotRunRepository", create=True):
            response = await service.create_hummingbot_instance(
                make_deployment(
                    resume_mode="off", resume_from=None, resume_accept_ungraceful=False
                )
            )

        assert response["success"] is True
        codes = [w["code"] for w in response["warnings"]]
        assert "BOTS_PATH_COUPLING_UNVERIFIED" in codes
        entry = response["warnings"][0]
        assert entry["hook_write_root"] == os.path.abspath("bots")
        assert entry["bot_mount_root"] == os.path.abspath(
            os.path.join(str(bots_tree.parent), "bots")
        )


# ---------------------------------------------------------------------------
# CLA-M01 — sizing-critical drift classification + surfacing
# ---------------------------------------------------------------------------

class TestSizingCriticalClassification:
    """Field names are the SPEC (engine ladder config), not the implementation."""

    @pytest.mark.parametrize("field", [
        "total_amount_quote",          # range_inventory_ladder.py:210
        "max_fund_value_quote",        # :218
        "shared_account_quote_quota",  # :226
        "use_wallet_balance",          # :262
        "claimed_base_value_quote",    # :270  (claimed_base_*)
        "claimed_base_amount",         # :278  (claimed_base_*)
        "buy_prices",                  # :291  range bounds
        "buy_amounts_pct",             # :302  per-level sizing
        "sell_prices",                 # :310  range bounds
        "sell_amounts_pct",            # :320  per-level sizing
    ])
    def test_engine_sizing_fields_are_classified_critical(self, field):
        # Mutation: remove any single name from _SIZING_CRITICAL_FIELDS -> the
        # matching case fails.
        assert _is_sizing_critical(field) is True

    def test_future_claimed_base_sibling_is_critical_by_prefix(self):
        # The triage names the family as a glob (`claimed_base_*`), so a new
        # engine field must be loud by default, not silent until someone adds it.
        assert _is_sizing_critical("claimed_base_something_new") is True

    @pytest.mark.parametrize("field", [
        "executor_refresh_time",   # :329  — timing, not sizing
        "event_refresh_enabled",   # :342
        "post_refresh_settle_seconds",  # :358
        "buy_spread",
        "id",
        "controller_name",
        "connector_name",
        "trading_pair",
    ])
    def test_non_sizing_fields_are_not_classified_critical(self, field):
        # The classification has to discriminate. Mutation: `return True` in
        # _is_sizing_critical -> every case here fails.
        assert _is_sizing_critical(field) is False


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def drift_between(tmp_path, source_cfg, template_cfg):
    """Run the REAL diff over two REAL YAML files."""
    src_instance = tmp_path / "src"
    new_instance = tmp_path / "new"
    write_yaml(src_instance / "conf" / "controllers" / CONTROLLER_FILE, source_cfg)
    write_yaml(new_instance / "conf" / "controllers" / CONTROLLER_FILE, template_cfg)
    source = MagicMock()
    source.instance_dir = src_instance
    return _diff_controller_configs(source, new_instance)


class TestDriftDiffClassification:
    def test_sizing_drift_is_marked_and_carries_both_values(self, tmp_path):
        # Spec: surface "(field, source value, template value)".
        # Mutation: hardcode "sizing_critical": False in _diff_controller_configs.
        drift = drift_between(
            tmp_path,
            {**TEMPLATE_CFG, "total_amount_quote": 500},
            {**TEMPLATE_CFG, "total_amount_quote": 80},
        )
        assert len(drift) == 1
        assert drift[0]["sizing_critical_fields"] == ["total_amount_quote"]
        field = next(f for f in drift[0]["fields"] if f["field"] == "total_amount_quote")
        assert field["sizing_critical"] is True
        assert field["source"] == 500      # what the stopped bot was running
        assert field["template"] == 80     # what the resumed bot will run

    def test_unrelated_field_drift_is_not_flagged_sizing_critical(self, tmp_path):
        # Spec: "unrelated-field drift → not flagged sizing-critical".
        drift = drift_between(
            tmp_path,
            {**TEMPLATE_CFG, "executor_refresh_time": 300},
            {**TEMPLATE_CFG, "executor_refresh_time": 600},
        )
        assert len(drift) == 1
        assert drift[0]["sizing_critical_fields"] == []
        assert drift[0]["fields"][0]["sizing_critical"] is False

    def test_identical_configs_produce_no_drift(self, tmp_path):
        # Guards the vacuous-fixture failure mode: if the diff flagged
        # everything, the tests above would pass for the wrong reason.
        assert drift_between(tmp_path, TEMPLATE_CFG, TEMPLATE_CFG) == []


class TestDriftSurfacedInResponses:
    """The fix is the RESPONSE, not the log. These are the tests that matter."""

    @pytest.mark.asyncio
    async def test_sizing_drift_reaches_the_preview_response(
        self, bots_tree, monkeypatch
    ):
        # Source instance was live-edited to a bigger fund than the template.
        # Spec: preview must surface it. Mutation: drop the
        # _warn_sizing_critical_drift(drift, plan) call in preview_resume ->
        # this test fails (the log line still prints, which is the old bug).
        src_cfg = bots_tree / "instances" / SRC_NAME / "conf" / "controllers" / CONTROLLER_FILE
        write_yaml(src_cfg, {**TEMPLATE_CFG, "total_amount_quote": 5000})

        db_manager, _ = make_db_manager(SRC_NAME)
        with patch("services.resume_service.BotRunRepository", create=True):
            result = await preview_resume(
                deployment=make_deployment(),
                bots_path=bots_tree,
                docker_client=make_docker_client(),
                db_manager=db_manager,
            )

        sizing = [w for w in result["warnings"] if w["code"] == "SIZING_CRITICAL_DRIFT"]
        assert len(sizing) == 1
        assert sizing[0]["field"] == "total_amount_quote"
        assert sizing[0]["source"] == 5000
        assert sizing[0]["template"] == TEMPLATE_CFG.get("total_amount_quote", "<absent>")
        assert sizing[0]["controller_id"] == CONTROLLER_ID
        assert sizing[0]["file"] == CONTROLLER_FILE
        # The full diff is available too.
        assert result["drift"][0]["sizing_critical_fields"] == ["total_amount_quote"]

    @pytest.mark.asyncio
    async def test_non_sizing_drift_is_not_warned_in_preview(self, bots_tree):
        # A response that warns about everything warns about nothing.
        src_cfg = bots_tree / "instances" / SRC_NAME / "conf" / "controllers" / CONTROLLER_FILE
        write_yaml(src_cfg, {**TEMPLATE_CFG, "buy_spread": 0.9})

        db_manager, _ = make_db_manager(SRC_NAME)
        with patch("services.resume_service.BotRunRepository", create=True):
            result = await preview_resume(
                deployment=make_deployment(),
                bots_path=bots_tree,
                docker_client=make_docker_client(),
                db_manager=db_manager,
            )

        assert [w for w in result["warnings"] if w["code"] == "SIZING_CRITICAL_DRIFT"] == []
        assert result["drift"][0]["fields"][0]["field"] == "buy_spread"
        assert result["drift"][0]["sizing_critical_fields"] == []

    @pytest.mark.asyncio
    async def test_sizing_drift_reaches_the_deploy_response(
        self, bots_tree, patched_security, monkeypatch
    ):
        # The deploy half of the same spec line ("in BOTH the preview and deploy
        # responses"). Mutation: drop the _warn_sizing_critical_drift call from
        # _seed -> this test fails.
        monkeypatch.setenv("BOTS_PATH", str(bots_tree.parent))
        src_cfg = bots_tree / "instances" / SRC_NAME / "conf" / "controllers" / CONTROLLER_FILE
        write_yaml(src_cfg, {**TEMPLATE_CFG, "claimed_base_amount": 12})

        client = make_docker_client()
        db_manager, _ = make_db_manager(SRC_NAME)
        service = make_service(client, db_manager=db_manager)

        with patch("services.resume_service.BotRunRepository", create=True):
            response = await service.create_hummingbot_instance(make_deployment())

        assert response["success"] is True
        sizing = [
            w for w in response["resume_warnings"]
            if w["code"] == "SIZING_CRITICAL_DRIFT"
        ]
        assert len(sizing) == 1
        assert sizing[0]["field"] == "claimed_base_amount"
        assert sizing[0]["source"] == 12
        assert sizing[0]["template"] == "<absent>"

    @pytest.mark.asyncio
    async def test_template_still_wins_no_carry_forward(
        self, bots_tree, patched_security, monkeypatch
    ):
        # CLA-M01 is "louder warning ONLY". The deployed instance must run the
        # TEMPLATE's value; if the warning had turned into a carry-forward this
        # would catch it. Mutation: copy the source value over the staged
        # template in _seed -> this test fails.
        monkeypatch.setenv("BOTS_PATH", str(bots_tree.parent))
        src_cfg = bots_tree / "instances" / SRC_NAME / "conf" / "controllers" / CONTROLLER_FILE
        write_yaml(src_cfg, {**TEMPLATE_CFG, "total_amount_quote": 5000})

        client = make_docker_client()
        db_manager, _ = make_db_manager(SRC_NAME)
        service = make_service(client, db_manager=db_manager)

        with patch("services.resume_service.BotRunRepository", create=True):
            response = await service.create_hummingbot_instance(make_deployment())

        assert response["success"] is True
        deployed = yaml.safe_load(
            (bots_tree / "instances" / NEW_NAME / "conf" / "controllers" / CONTROLLER_FILE)
            .read_text(encoding="utf-8")
        )
        # The template has no total_amount_quote at all; the source's 5000 must
        # not have been carried forward.
        assert "total_amount_quote" not in deployed
