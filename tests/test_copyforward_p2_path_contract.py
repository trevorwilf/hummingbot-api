"""Phase 2 — CONTRACT C1: the ``state_file_name`` path contract, API half
(CDX-007 / CLA-004).

Two things are under test here:

  * ``services.state_file_contract.classify_state_file_name`` — the ONE api-side
    C1 predicate, tested directly as a table.
  * ``preview_resume`` — the preview must reach the same C1 verdict the deploy
    would, because a preview that green-lights a deploy the deploy then refuses
    (or worse, one it silently mutilates) is not a pre-deploy check.

The copy-plan half of C1 (abort vs opt-out skip inside ``compute_copy_plan``) is
covered in ``tests/test_copyforward_copyset.py::TestC1*``, and the end-to-end
deploy half in ``tests/test_copyforward_e2e.py`` (§11 matrix rows
``state_file_absolute_abort`` / ``state_file_absolute_optout``).

EVERY expected value below is derived from CONTRACT C1 as specified, never by
running the implementation. The contract, verbatim:

    ACCEPT: unset/None; or a str whose stripped value is non-empty and, parsed as
    BOTH PurePosixPath and PureWindowsPath: is_absolute() is False, has no drive
    and no root/anchor, contains no ``..`` component, is not ``.``, and its POSIX
    normalization remains a strict descendant of data/ when joined.
    REJECT (fail-closed): absolute POSIX or Windows paths, drive letters, UNC
    paths, any ``..`` component, ``.`` — UNLESS an explicit opt-out boolean
    (allow_absolute_state_file_name, default False) is set, which permits
    ABSOLUTE paths only, never traversal.
    Canonical value: the stripped string. Empty-after-strip maps to unset/None.

Real tmp_path filesystems throughout; the Docker client is the only mock (the
source-container guard's single read-only call).
"""

import json
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from docker.errors import NotFound

from models.bot_orchestration import V2ControllerDeployment
from services.resume_service import (
    ResumeAbortReason,
    ResumeError,
    preview_resume,
)
from services.state_file_contract import (
    StateFileStatus,
    classify_state_file_name,
)


# ===========================================================================
# 1.  The shared predicate, as a spec table
# ===========================================================================

class TestClassifyStateFileName:
    """C1's accept/reject set, decision by decision."""

    @pytest.mark.parametrize(
        "value",
        [
            "x.json",              # plain file
            "sub/x.json",          # nested, relative
            "a/b/c/x.json",        # deeper
            "sub\\x.json",         # backslash separator: relative under BOTH flavors
            "./x.json",            # a leading '.' component is dropped; still contained
            ".hidden.json",        # a dotfile is a name, not a directory reference
        ],
    )
    def test_accepts_relative_contained(self, value):
        verdict = classify_state_file_name(value)
        assert verdict.status is StateFileStatus.RELATIVE_OK
        assert verdict.canonical == value

    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    def test_unset_and_empty_after_strip_map_to_unset(self, value):
        """C1: 'Empty-after-strip maps to unset/None (default behavior), which is
        not fail-open: None selects the default state file name.'"""
        verdict = classify_state_file_name(value)
        assert verdict.status is StateFileStatus.UNSET
        assert verdict.canonical is None

    def test_canonical_is_the_stripped_string(self):
        """C1: 'Canonical value: the stripped string.'"""
        verdict = classify_state_file_name("  sub/x.json  ")
        assert verdict.status is StateFileStatus.RELATIVE_OK
        assert verdict.canonical == "sub/x.json"

    @pytest.mark.parametrize(
        "value",
        [
            "/tmp/x.json",         # POSIX absolute
            "/x.json",
            "C:\\x.json",          # Windows drive-letter absolute
            "C:/x.json",
            "\\\\share\\x",        # UNC
            "//share/x",
        ],
    )
    def test_absolute_forms_classify_as_absolute(self, value):
        """ABSOLUTE is the only status the opt-out can rescue — so every absolute
        form must land here rather than in INVALID, and none may reach RELATIVE_OK.

        Note ``C:\\x.json`` and ``\\\\share\\x`` are single RELATIVE components to
        PurePosixPath: a POSIX-only check calls them ordinary filenames. That is
        the fail-open C1 closes."""
        verdict = classify_state_file_name(value)
        assert verdict.status is StateFileStatus.ABSOLUTE
        assert verdict.canonical == value.strip()

    @pytest.mark.parametrize(
        "value",
        [
            "../x.json",           # traversal
            "../conf/x.yml",
            "sub/../../x.json",
            "sub\\..\\..\\x.json",  # '..' invisible to PurePosixPath (one component)
            "..",
            "/tmp/../etc/x.json",  # absolute AND traversing -> traversal wins
            "C:\\..\\x.json",
            ".",                   # directory reference, not a file
            "./",
            "C:",                  # drive-relative: has a drive, not absolute
            "\\x.json",            # root-relative under Windows: rooted, not absolute
        ],
    )
    def test_reject_set_is_invalid(self, value):
        """INVALID is unconditional: no opt-out admits any of these."""
        verdict = classify_state_file_name(value)
        assert verdict.status is StateFileStatus.INVALID
        assert verdict.canonical is None
        assert verdict.reason

    @pytest.mark.parametrize("value", [5, 0, True, False, 1.5, ["x.json"], {"a": 1}])
    def test_non_string_is_invalid(self, value):
        """C1 accepts 'unset/None; or a str ...'. Anything else is outside the
        accept set. (The old code str()'d whatever it got into a filename.)"""
        verdict = classify_state_file_name(value)
        assert verdict.status is StateFileStatus.INVALID
        assert verdict.canonical is None

    def test_traversal_is_checked_before_absoluteness(self):
        """C1: the opt-out 'permits ABSOLUTE paths only, never traversal'. If an
        absolute-and-traversing path classified as ABSOLUTE, the opt-out would
        admit ``/tmp/../etc/x.json`` — so the ordering is part of the contract."""
        assert (
            classify_state_file_name("/tmp/../etc/x.json").status
            is StateFileStatus.INVALID
        )

    def test_verdict_carries_a_reason_for_rejections(self):
        verdict = classify_state_file_name("../x.json")
        assert "'..'" in verdict.reason

    def test_classifier_never_raises_on_hostile_input(self):
        """The classifier reports; the caller fails closed. A raising classifier
        would turn a C1 violation into an opaque 500 rather than a 409."""
        for value in (None, "", "..", 5, object(), b"x.json"):
            assert classify_state_file_name(value).status in set(StateFileStatus)


# ===========================================================================
# 2.  Preview reaches the same C1 verdict as the deploy
# ===========================================================================

SRC_NAME = "LADDER_BOT-20260710-101010"
NEW_NAME = "LADDER_BOT-20260714-121212"
CONTROLLER_ID = "ladder_xmr"
CONTROLLER_FILE = "ladder_xmr.yml"
LEDGER_NAME = f"range_inventory_ladder_{CONTROLLER_ID}.json"
ABS_STATE_FILE = "/var/lib/hummingbot/ladder.json"

TEMPLATE_CFG = {
    "controller_name": "range_inventory_ladder",
    "id": CONTROLLER_ID,
    "connector_name": "kraken",
}


@pytest.fixture
def bots_tree(tmp_path, monkeypatch):
    """A real ``bots/`` tree with a stopped source instance holding a real ledger."""
    monkeypatch.chdir(tmp_path)
    bots = tmp_path / "bots"

    creds = bots / "credentials" / "master_account"
    creds.mkdir(parents=True)
    # Postgres: keeps the sqlite half of the copy plan out of this phase's tests.
    (creds / "conf_client.yml").write_text(
        yaml.safe_dump({"db_mode": {"db_engine": "postgres+asyncpg"}}), encoding="utf-8"
    )

    controllers = bots / "conf" / "controllers"
    controllers.mkdir(parents=True)
    (controllers / CONTROLLER_FILE).write_text(yaml.safe_dump(TEMPLATE_CFG), encoding="utf-8")

    src = bots / "instances" / SRC_NAME / "data"
    src.mkdir(parents=True)
    (src / LEDGER_NAME).write_text(json.dumps({"levels": [1, 2], "seq": 7}), encoding="utf-8")
    (src / f"{LEDGER_NAME}.owner").write_text(
        json.dumps({"controller_id": CONTROLLER_ID, "pid": 7}), encoding="utf-8"
    )
    return bots


def write_template(bots, **cfg_overrides):
    cfg = dict(TEMPLATE_CFG)
    cfg.update(cfg_overrides)
    (bots / "conf" / "controllers" / CONTROLLER_FILE).write_text(
        yaml.safe_dump(cfg), encoding="utf-8"
    )


def make_docker_client():
    client = MagicMock()
    client.containers.get.side_effect = NotFound("no such container")
    return client


def make_deployment(**overrides):
    kwargs = dict(
        instance_name=NEW_NAME,
        credentials_profile="master_account",
        controllers_config=[CONTROLLER_FILE],
        image="hummingbot/hummingbot:v2.9",
        resume_mode="explicit",
        resume_from=SRC_NAME,
        resume_accept_ungraceful=True,
    )
    kwargs.update(overrides)
    return V2ControllerDeployment(**kwargs)


def graceful_repo(*names):
    repo = MagicMock()
    repo.get_bot_runs = AsyncMock(
        return_value=[
            SimpleNamespace(
                instance_name=n, run_status="STOPPED", stopped_at=datetime(2026, 7, 10, 12, 0, 0)
            )
            for n in names
        ]
    )
    return repo


def _snapshot(root):
    """Every file's bytes under ``root`` — proof a read-only path stayed read-only."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = os.path.join(dirpath, name)
            out[os.path.relpath(p, root)] = open(p, "rb").read()
    return out


class TestPreviewC1:
    @pytest.mark.asyncio
    async def test_preview_refuses_absolute_state_file_and_mutates_nothing(self, bots_tree):
        """The preview is the pre-deploy check: it must refuse what the deploy
        refuses. Before this phase it reported ``would_succeed: True`` with the
        controller quietly marked "skipped"."""
        write_template(bots_tree, state_file_name=ABS_STATE_FILE)
        before = _snapshot(bots_tree)

        with pytest.raises(ResumeError) as exc:
            await preview_resume(
                deployment=make_deployment(),
                bots_path=bots_tree,
                docker_client=make_docker_client(),
                bot_run_repo=graceful_repo(SRC_NAME),
            )

        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID
        assert _snapshot(bots_tree) == before

    @pytest.mark.asyncio
    async def test_preview_refuses_traversal_even_with_optout(self, bots_tree):
        """C1: the opt-out permits absolute paths, never traversal."""
        write_template(bots_tree, state_file_name="../../etc/x.json")

        with pytest.raises(ResumeError) as exc:
            await preview_resume(
                deployment=make_deployment(allow_absolute_state_file_name=True),
                bots_path=bots_tree,
                docker_client=make_docker_client(),
                bot_run_repo=graceful_repo(SRC_NAME),
            )

        assert exc.value.reason is ResumeAbortReason.STATE_FILE_PATH_INVALID

    @pytest.mark.asyncio
    async def test_preview_surfaces_optout_skip_as_a_structured_warning(self, bots_tree):
        """With the opt-out, the skip is permitted — and reported in the response
        body, where the operator posting the preview can actually see it."""
        write_template(bots_tree, state_file_name=ABS_STATE_FILE)
        before = _snapshot(bots_tree)

        result = await preview_resume(
            deployment=make_deployment(allow_absolute_state_file_name=True),
            bots_path=bots_tree,
            docker_client=make_docker_client(),
            bot_run_repo=graceful_repo(SRC_NAME),
        )

        assert result["would_succeed"] is True
        assert result["decisions"] == {CONTROLLER_ID: "skipped"}
        entries = [
            w for w in result["warnings"] if w["code"] == "STATE_FILE_ABSOLUTE_SKIPPED"
        ]
        assert len(entries) == 1, result["warnings"]
        assert entries[0]["controller_id"] == CONTROLLER_ID
        assert entries[0]["state_file_name"] == ABS_STATE_FILE
        # Nothing is planned for a skipped controller...
        assert result["files"] == []
        # ...and a preview still mutates nothing.
        assert _snapshot(bots_tree) == before

    @pytest.mark.asyncio
    async def test_preview_plans_a_relative_state_file_normally(self, bots_tree):
        """The accept path still works: no warning, ledger planned at the real
        target. Without this, "abort everything" would pass the tests above."""
        custom = "custom_ladder.json"
        src_data = bots_tree / "instances" / SRC_NAME / "data"
        (src_data / custom).write_text(json.dumps({"levels": [3]}), encoding="utf-8")
        (src_data / f"{custom}.owner").write_text(
            json.dumps({"controller_id": CONTROLLER_ID}), encoding="utf-8"
        )
        write_template(bots_tree, state_file_name=custom)

        result = await preview_resume(
            deployment=make_deployment(),
            bots_path=bots_tree,
            docker_client=make_docker_client(),
            bot_run_repo=graceful_repo(SRC_NAME),
        )

        assert result["would_succeed"] is True
        assert result["decisions"] == {CONTROLLER_ID: "copied"}
        assert result["warnings"] == []
        planned = {os.path.basename(f["dst"]) for f in result["files"]}
        assert custom in planned
