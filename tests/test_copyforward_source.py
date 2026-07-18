"""Phase 2 tests: copy-forward source resolution (design §5).

Covers services.resume_service.resolve_source for the ``explicit`` and
``latest`` strategies:

  * explicit: found / missing / archived / nested-resolvable / nested-ambiguous
  * S3 wording in the not-found error when the archive dir is absent
  * latest: real-world operator name (never double-strip the base)
  * latest: DB lineage preferred over directory listing
  * latest: directory-listing fallback on DB error
  * latest: exclude the instance being created
  * latest: timestamp tie -> abort; zero candidates -> abort
  * latest: archive fall-through — archived winner resolves; instances/
    precedence; nested-archive abort; hollow archive -> not-found naming both
    roots; DB-down fallback discovers archived/ with a deduped union

All filesystem via ``tmp_path``; the bot-run repo is mocked via ``AsyncMock``.
No Docker, no DB, no network.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.bot_orchestration import V2ControllerDeployment
from services.resume_service import (
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    _strip_api_suffix,
    resolve_source,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_deployment(**resume_kwargs) -> V2ControllerDeployment:
    """Build a deploy model carrying only the resume fields under test."""
    return V2ControllerDeployment(
        instance_name="NEW-BOT",
        credentials_profile="main",
        controllers_config=["ladder_xmr"],
        **resume_kwargs,
    )


def make_repo(instance_names=None, error=None) -> MagicMock:
    """Build a mock BotRunRepository.

    ``get_bot_runs`` returns row objects exposing ``.instance_name``, or raises
    ``error`` (to exercise the DB-unavailable directory fallback).
    """
    repo = MagicMock()
    if error is not None:
        repo.get_bot_runs = AsyncMock(side_effect=error)
    else:
        rows = [SimpleNamespace(instance_name=n) for n in (instance_names or [])]
        repo.get_bot_runs = AsyncMock(return_value=rows)
    return repo


def seed_instance(bots_path, name, tree="instances", with_data=True, with_conf=False):
    """Create a fake instance directory under ``bots_path/<tree>/<name>``."""
    inst = bots_path / tree / name
    if with_data:
        (inst / "data").mkdir(parents=True, exist_ok=True)
    if with_conf:
        (inst / "conf").mkdir(parents=True, exist_ok=True)
    inst.mkdir(parents=True, exist_ok=True)
    return inst


# ---------------------------------------------------------------------------
# Base-name parsing (§5 — never double-strip)
# ---------------------------------------------------------------------------

class TestBaseNameParsing:
    def test_strips_only_final_api_suffix(self):
        # Operator name embeds its own timestamp-like token; strip ONLY the last.
        name = "KRAKEN_LADDER_V1-20260712-2302-20260712-230254"
        assert _strip_api_suffix(name) == "KRAKEN_LADDER_V1-20260712-2302"

    def test_plain_name_with_single_suffix(self):
        assert _strip_api_suffix("MYBOT-20260101-000000") == "MYBOT"

    def test_name_without_suffix_unchanged(self):
        assert _strip_api_suffix("MYBOT") == "MYBOT"


# ---------------------------------------------------------------------------
# explicit
# ---------------------------------------------------------------------------

class TestExplicit:
    @pytest.mark.asyncio
    async def test_found(self, tmp_path):
        seed_instance(tmp_path, "SRC")
        dep = make_deployment(resume_mode="explicit", resume_from="SRC")
        result = await resolve_source(dep, "NEW-BOT-20260101-000000", tmp_path, make_repo())
        assert isinstance(result, ResolvedSource)
        assert result.instance_name == "SRC"
        assert result.origin == "instances"
        assert result.data_dir == tmp_path / "instances" / "SRC" / "data"
        assert result.instance_dir == tmp_path / "instances" / "SRC"

    @pytest.mark.asyncio
    async def test_missing_aborts_source_not_found(self, tmp_path):
        dep = make_deployment(resume_mode="explicit", resume_from="NOPE")
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, "NEW-BOT-20260101-000000", tmp_path, make_repo())
        assert exc.value.reason == ResumeAbortReason.SOURCE_NOT_FOUND
        # Message lists the searched path.
        assert "NOPE" in exc.value.message
        assert str(tmp_path / "instances" / "NOPE") in exc.value.message

    @pytest.mark.asyncio
    async def test_archived_found(self, tmp_path):
        seed_instance(tmp_path, "SRC", tree="archived")
        dep = make_deployment(
            resume_mode="explicit", resume_from="SRC", resume_from_archive=True
        )
        result = await resolve_source(dep, "NEW-BOT-20260101-000000", tmp_path, make_repo())
        assert result.origin == "archived"
        assert result.data_dir == tmp_path / "archived" / "SRC" / "data"

    @pytest.mark.asyncio
    async def test_archived_nested_resolvable(self, tmp_path):
        # Only the nested copy is a complete instance; the outer holds just the
        # nested dir -> resolve the innermost.
        (tmp_path / "archived" / "SRC" / "SRC" / "data").mkdir(parents=True)
        dep = make_deployment(
            resume_mode="explicit", resume_from="SRC", resume_from_archive=True
        )
        result = await resolve_source(dep, "NEW-BOT-20260101-000000", tmp_path, make_repo())
        assert result.origin == "archived"
        assert result.instance_dir == tmp_path / "archived" / "SRC" / "SRC"
        assert result.data_dir == tmp_path / "archived" / "SRC" / "SRC" / "data"

    @pytest.mark.asyncio
    async def test_archived_nested_ambiguous_aborts(self, tmp_path):
        # Both the base and the nested copy look complete -> ambiguous.
        (tmp_path / "archived" / "SRC" / "data").mkdir(parents=True)
        (tmp_path / "archived" / "SRC" / "SRC" / "data").mkdir(parents=True)
        dep = make_deployment(
            resume_mode="explicit", resume_from="SRC", resume_from_archive=True
        )
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, "NEW-BOT-20260101-000000", tmp_path, make_repo())
        assert exc.value.reason == ResumeAbortReason.ARCHIVE_NESTED

    @pytest.mark.asyncio
    async def test_archive_absent_mentions_s3(self, tmp_path):
        # resume_from_archive=True but no archived dir -> not found + S3 wording.
        dep = make_deployment(
            resume_mode="explicit", resume_from="SRC", resume_from_archive=True
        )
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, "NEW-BOT-20260101-000000", tmp_path, make_repo())
        assert exc.value.reason == ResumeAbortReason.SOURCE_NOT_FOUND
        assert "S3" in exc.value.message

    @pytest.mark.asyncio
    async def test_off_mode_is_programmer_error(self, tmp_path):
        dep = make_deployment()  # resume_mode defaults to "off"
        with pytest.raises(ValueError):
            await resolve_source(dep, "NEW-BOT-20260101-000000", tmp_path, make_repo())


# ---------------------------------------------------------------------------
# latest
# ---------------------------------------------------------------------------

class TestLatest:
    @pytest.mark.asyncio
    async def test_real_world_operator_name_newest_wins(self, tmp_path):
        # The operator name embeds its own timestamp; base is the single-strip
        # remainder. Candidates share that base; the newest API timestamp wins.
        new_name = "KRAKEN_LADDER_V1-20260712-2302-20260712-230254"
        older = "KRAKEN_LADDER_V1-20260712-2302-20260712-215030"
        newest = "KRAKEN_LADDER_V1-20260712-2302-20260712-220000"
        seed_instance(tmp_path, newest)
        repo = make_repo([older, newest, new_name])  # new_name is self -> excluded
        dep = make_deployment(resume_mode="latest")
        result = await resolve_source(dep, new_name, tmp_path, repo)
        assert result.instance_name == newest
        # Prove the base was never double-stripped.
        assert _strip_api_suffix(newest) == "KRAKEN_LADDER_V1-20260712-2302"

    @pytest.mark.asyncio
    async def test_db_lineage_preferred_over_directory(self, tmp_path):
        # DB names the source; a newer-looking on-disk dir NOT in the DB must be
        # ignored because the DB query succeeded.
        db_pick = "BASE-20260101-000000"
        seed_instance(tmp_path, db_pick)
        seed_instance(tmp_path, "BASE-20260202-000000")  # newer on disk, not in DB
        repo = make_repo([db_pick])
        dep = make_deployment(resume_mode="latest")
        result = await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert result.instance_name == db_pick

    @pytest.mark.asyncio
    async def test_directory_fallback_on_db_error(self, tmp_path):
        seed_instance(tmp_path, "BASE-20260101-000000")
        seed_instance(tmp_path, "BASE-20260103-000000")  # newest on disk
        repo = make_repo(error=RuntimeError("db down"))
        dep = make_deployment(resume_mode="latest")
        result = await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert result.instance_name == "BASE-20260103-000000"

    @pytest.mark.asyncio
    async def test_excludes_self(self, tmp_path):
        new_name = "BASE-20260105-000000"
        prior = "BASE-20260101-000000"
        seed_instance(tmp_path, prior)
        # Self is newest by timestamp but must be excluded, leaving the prior.
        repo = make_repo([new_name, prior])
        dep = make_deployment(resume_mode="latest")
        result = await resolve_source(dep, new_name, tmp_path, repo)
        assert result.instance_name == prior

    @pytest.mark.asyncio
    async def test_timestamp_tie_aborts(self, tmp_path):
        # Two runs advertising the same newest identity -> ambiguous lineage.
        dup = "BASE-20260101-000000"
        repo = make_repo([dup, dup])
        dep = make_deployment(resume_mode="latest")
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert exc.value.reason == ResumeAbortReason.LATEST_AMBIGUOUS

    @pytest.mark.asyncio
    async def test_zero_candidates_aborts(self, tmp_path):
        repo = make_repo([])  # DB succeeds, no rows
        dep = make_deployment(resume_mode="latest")
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert exc.value.reason == ResumeAbortReason.SOURCE_NOT_FOUND

    @pytest.mark.asyncio
    async def test_only_self_candidate_aborts(self, tmp_path):
        new_name = "BASE-20260305-000000"
        repo = make_repo([new_name])  # only self -> excluded -> zero
        dep = make_deployment(resume_mode="latest")
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, new_name, tmp_path, repo)
        assert exc.value.reason == ResumeAbortReason.SOURCE_NOT_FOUND

    @pytest.mark.asyncio
    async def test_resolved_but_data_dir_missing_aborts(self, tmp_path):
        # DB resolves a name, but its data/ dir isn't on disk -> fail-closed.
        pick = "BASE-20260101-000000"
        repo = make_repo([pick])  # no seed_instance -> no data dir
        dep = make_deployment(resume_mode="latest")
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert exc.value.reason == ResumeAbortReason.SOURCE_NOT_FOUND

    @pytest.mark.asyncio
    async def test_archived_winner_resolves_when_instance_moved(self, tmp_path):
        # The default stop flow moves instances/<name> -> archived/<name>;
        # latest must fall through to the archive.
        winner = "BASE-20260101-000000"
        seed_instance(tmp_path, winner, tree="archived")  # nothing in instances/
        repo = make_repo([winner])
        dep = make_deployment(resume_mode="latest")
        result = await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert result.instance_name == winner
        assert result.origin == "archived"
        assert result.instance_dir == tmp_path / "archived" / winner
        assert result.data_dir == tmp_path / "archived" / winner / "data"

    @pytest.mark.asyncio
    async def test_instances_precedence_when_both_trees_have_winner(self, tmp_path):
        # instances/ always wins if present; the archive is not even consulted.
        winner = "BASE-20260101-000000"
        seed_instance(tmp_path, winner)
        seed_instance(tmp_path, winner, tree="archived")
        repo = make_repo([winner])
        dep = make_deployment(resume_mode="latest")
        result = await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert result.origin == "instances"
        assert result.data_dir == tmp_path / "instances" / winner / "data"
        assert result.instance_dir == tmp_path / "instances" / winner

    @pytest.mark.asyncio
    async def test_archived_nested_ambiguous_aborts(self, tmp_path):
        # Same nesting pathology as the explicit path: both the base and its
        # nested same-name copy look complete -> refuse, never pick one.
        winner = "BASE-20260101-000000"
        (tmp_path / "archived" / winner / "data").mkdir(parents=True)
        (tmp_path / "archived" / winner / winner / "data").mkdir(parents=True)
        repo = make_repo([winner])
        dep = make_deployment(resume_mode="latest")
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert exc.value.reason == ResumeAbortReason.ARCHIVE_NESTED

    @pytest.mark.asyncio
    async def test_archived_compressed_only_aborts_naming_both_roots(self, tmp_path):
        # Only a compressed tarball exists: not resumable (no extract path).
        # The error must name BOTH searched roots and the compressed/S3 caveat.
        winner = "BASE-20260101-000000"
        archived = tmp_path / "archived"
        archived.mkdir(parents=True)
        (archived / f"{winner}_archive.tar.gz").write_bytes(b"gzip-stub")
        repo = make_repo([winner])
        dep = make_deployment(resume_mode="latest")
        with pytest.raises(ResumeError) as exc:
            await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert exc.value.reason == ResumeAbortReason.SOURCE_NOT_FOUND
        assert str(tmp_path / "instances" / winner / "data") in exc.value.message
        assert str(tmp_path / "archived" / winner) in exc.value.message
        assert "S3" in exc.value.message

    @pytest.mark.asyncio
    async def test_directory_fallback_discovers_archived_winner(self, tmp_path):
        # DB down: the directory fallback must list archived/ too, so an
        # archived winner is still discoverable.
        older = "BASE-20260101-000000"
        newest = "BASE-20260103-000000"
        seed_instance(tmp_path, older)
        seed_instance(tmp_path, newest, tree="archived")
        repo = make_repo(error=RuntimeError("db down"))
        dep = make_deployment(resume_mode="latest")
        result = await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert result.instance_name == newest
        assert result.origin == "archived"
        assert result.data_dir == tmp_path / "archived" / newest / "data"

    @pytest.mark.asyncio
    async def test_directory_fallback_same_name_both_trees_not_ambiguous(self, tmp_path):
        # DB down, same name in BOTH trees: one instance in two places, not
        # duplicated lineage -> deduped union resolves with instances/
        # precedence instead of aborting LATEST_AMBIGUOUS.
        winner = "BASE-20260101-000000"
        seed_instance(tmp_path, winner)
        seed_instance(tmp_path, winner, tree="archived")
        repo = make_repo(error=RuntimeError("db down"))
        dep = make_deployment(resume_mode="latest")
        result = await resolve_source(dep, "BASE-20260305-000000", tmp_path, repo)
        assert result.instance_name == winner
        assert result.origin == "instances"
        assert result.data_dir == tmp_path / "instances" / winner / "data"
