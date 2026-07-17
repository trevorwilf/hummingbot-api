"""Phase 5 tests: three small guards (CDX-015, CLA-008 #7, CLA-008 P2).

Covers, in ``services.resume_service``:

  * **CDX-015** ``db_mode.db_engine`` normalization: ``None``/``"SQLite"``/
    ``" sqlite "`` -> sqlite (carry the per-instance DB); ``postgres...`` ->
    non-sqlite; ANY other value -> ``DB_ENGINE_UNKNOWN`` abort. The old exact
    ``== "sqlite"`` compare silently routed every unrecognised value down the
    Postgres path, dropping the source DB from the copy set.
  * **CLA-008 #7** nested ``.owner`` sidecar: the sidecar keeps the ledger's
    RELATIVE path instead of being flattened to ``data/<basename>.owner``, which
    separated a nested ledger from its identity marker.
  * **CLA-008 P2** ``_guard_source_container`` error mapping: Docker API /
    connection failures -> ``SOURCE_STATE_UNVERIFIED`` (409, fail-closed)
    instead of an opaque 500; ``NotFound`` keeps meaning "verified absent";
    programming errors are NOT swallowed.

Expected values are derived from the phase spec, not from running the
implementation. Real ``tmp_path`` filesystems and the real planning/copy code
throughout; the only mock is the Docker client in the P2 group, where the error
mapping IS the unit under test and the daemon is an unavoidable external.

The copy-set helpers are imported from the Phase 3 suite rather than re-declared
so these tests exercise the same fixtures the rest of the hook is tested with.
"""

import json
from unittest.mock import MagicMock

import pytest
import yaml
from docker.errors import APIError, NotFound
from requests.exceptions import ConnectionError as RequestsConnectionError

from test_copyforward_copyset import (
    kinds,
    make_dep,
    make_source,
    new_instance,
    write_conf_client,
    write_controller,
    write_ledger,
)

from services.resume_service import (
    GuardReport,
    ResumeAbortReason,
    ResumeError,
    _execute_copy_plan,
    _guard_source_container,
    compute_copy_plan,
)


def write_conf_client_raw(new_instance_dir, db_mode):
    """Write ``conf_client.yml`` with an explicit ``db_mode`` mapping.

    ``write_conf_client(db_engine=None)`` omits the whole ``db_mode`` block, but
    CDX-015 distinguishes "no db_mode" from "db_mode with db_engine: null" — both
    must select the engine's sqlite default, and only this helper can express the
    second.
    """
    cdir = new_instance_dir / "conf"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "conf_client.yml").write_text(
        yaml.safe_dump({"db_mode": db_mode}), encoding="utf-8"
    )


def sqlite_source(tmp_path, controller_id="ctrl_a"):
    """A source holding a valid ledger + a per-instance sqlite DB to carry."""
    src = make_source(tmp_path)
    write_ledger(
        src,
        f"range_inventory_ladder_{controller_id}.json",
        owner_id=controller_id,
        ledger_controller_id=controller_id,
    )
    (src.data_dir / "trades.sqlite").write_text("db", encoding="utf-8")
    return src


# ===========================================================================
# CDX-015 — db_engine normalization
# ===========================================================================

class TestDbEngineNormalization:
    """The observable consequence of the classification is whether the source
    sqlite DB is in the copy set, so every case asserts THAT rather than the
    predicate's return value: a bot that silently loses its trade history is the
    actual bug CDX-015 describes.
    """

    @pytest.mark.parametrize("engine", ["sqlite", "SQLite", "SQLITE", " sqlite ", "\tSqLiTe\n"])
    def test_sqlite_variants_carry_the_db(self, tmp_path, engine):
        """Spec: normalize ``str(engine).strip().casefold()``; 'sqlite' -> sqlite.

        The old exact compare accepted only the lowercase, unpadded literal, so
        'SQLite' took the Postgres branch and dropped trades.sqlite from the set.
        """
        src = sqlite_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        write_conf_client(new, db_engine=engine)

        plan = compute_copy_plan(new, src, make_dep())

        assert "trades.sqlite" in {it.src.name for it in plan.files_to_copy}
        assert kinds(plan, "sqlite")

    @pytest.mark.parametrize(
        "engine", ["postgres", "postgresql", "postgresql+asyncpg", "PostgreSQL", " POSTGRES "]
    )
    def test_postgres_variants_carry_no_sqlite(self, tmp_path, engine):
        """Spec: ``startswith("postgres")`` -> non-sqlite. A Postgres deployment
        has no per-instance sqlite to carry (§15)."""
        src = sqlite_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        write_conf_client(new, db_engine=engine)

        plan = compute_copy_plan(new, src, make_dep())

        assert "trades.sqlite" not in {it.src.name for it in plan.files_to_copy}
        assert kinds(plan, "sqlite") == []

    @pytest.mark.parametrize("engine", ["mysql", "garbage", "sqlkite", "sqlite3", 0, True, 3.5])
    def test_unknown_engine_aborts(self, tmp_path, engine):
        """Spec: ANY other value -> ResumeError abort, never silently non-sqlite.

        Includes the non-str YAML types: an unquoted ``db_engine: 0`` normalizes
        to '0', which names no engine we know, so it must abort rather than be
        read as 'not sqlite'. 'sqlite3' is here deliberately — it is NOT the
        engine's name, and a prefix/substring match would wrongly accept it.
        """
        src = sqlite_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        write_conf_client(new, db_engine=engine)

        with pytest.raises(ResumeError) as excinfo:
            compute_copy_plan(new, src, make_dep())

        assert excinfo.value.reason == ResumeAbortReason.DB_ENGINE_UNKNOWN

    def test_explicit_null_db_engine_defaults_to_sqlite(self, tmp_path):
        """Spec: ``None`` -> sqlite, preserving the engine's documented default
        (DBSqliteMode). 'db_engine: null' is unset, not unknown — it must not
        abort."""
        src = sqlite_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        write_conf_client_raw(new, {"db_engine": None})

        plan = compute_copy_plan(new, src, make_dep())

        assert "trades.sqlite" in {it.src.name for it in plan.files_to_copy}

    def test_abort_leaves_nothing_copied(self, tmp_path):
        """Fail-closed means the deploy stops, not that it proceeds with a
        partial set: compute_copy_plan must not have written anything."""
        src = sqlite_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        write_conf_client(new, db_engine="mysql")

        with pytest.raises(ResumeError):
            compute_copy_plan(new, src, make_dep())

        assert not (new / "data").exists()


# ===========================================================================
# CLA-008 #7 — nested .owner sidecar keeps the ledger's relative path
# ===========================================================================

class TestNestedOwnerSidecar:
    """The engine finds a ledger's identity marker ADJACENT to the ledger. The
    ledger already preserved its relative path; the sidecar was flattened to the
    bare basename, so a nested ledger arrived without its marker.
    """

    @staticmethod
    def _nested_case(tmp_path):
        src = make_source(tmp_path)
        (src.data_dir / "sub" / "dir").mkdir(parents=True)
        write_ledger(
            src, "sub/dir/ledger.json", owner_id="ctrl_c", ledger_controller_id="ctrl_c"
        )
        new = new_instance(tmp_path)
        write_controller(
            new, "c.yml", controller_id="ctrl_c", state_file_name="sub/dir/ledger.json"
        )
        return src, new

    def test_owner_planned_at_the_ledgers_relative_path(self, tmp_path):
        """Spec: preserve the SAME relative path for the owner as the ledger."""
        src, new = self._nested_case(tmp_path)

        plan = compute_copy_plan(new, src, make_dep())

        ledger = kinds(plan, "ledger")[0]
        owner = kinds(plan, "owner")[0]
        assert ledger.dst == new / "data" / "sub" / "dir" / "ledger.json"
        assert owner.dst == new / "data" / "sub" / "dir" / "ledger.json.owner"
        assert owner.dst.parent == ledger.dst.parent  # adjacent, per spec

    def test_owner_lands_adjacent_on_disk(self, tmp_path):
        """The plan is only a promise — execute it and prove the pair lands
        together, with the sidecar's contents intact and nothing at the old
        flattened location."""
        src, new = self._nested_case(tmp_path)
        plan = compute_copy_plan(new, src, make_dep())
        new_data = new / "data"
        new_data.mkdir(parents=True, exist_ok=True)

        _execute_copy_plan(plan, new_data)

        ledger_dst = new_data / "sub" / "dir" / "ledger.json"
        owner_dst = new_data / "sub" / "dir" / "ledger.json.owner"
        assert ledger_dst.is_file()
        assert owner_dst.is_file()
        assert json.loads(owner_dst.read_text(encoding="utf-8"))["controller_id"] == "ctrl_c"
        # The flattened destination the old code used must not be written at all.
        assert not (new_data / "ledger.json.owner").exists()

    def test_flat_ledger_owner_unchanged(self, tmp_path):
        """A non-nested ledger's sidecar destination is unchanged by this fix —
        for a bare name the relative path IS the basename."""
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())

        assert kinds(plan, "owner")[0].dst == (
            new / "data" / "range_inventory_ladder_ctrl_a.json.owner"
        )


# ===========================================================================
# CLA-008 P2 — _guard_source_container error mapping
# ===========================================================================

class TestSourceContainerErrorMapping:
    """Mocking the Docker client is legitimate here: the unit under test is the
    exception -> abort-reason mapping, and the daemon is the unavoidable
    external. The assertions are on the REAL guard's behavior (raised reason,
    recorded report), never on the mock.
    """

    @staticmethod
    def _client_raising(exc):
        client = MagicMock()
        client.containers.get.side_effect = exc
        return client

    @pytest.mark.parametrize(
        "exc",
        [
            APIError("500 Server Error: Internal Server Error"),
            RequestsConnectionError("cannot connect to the Docker daemon"),
        ],
        ids=["api_error", "daemon_unreachable"],
    )
    def test_docker_failure_refuses_fail_closed(self, tmp_path, exc):
        """Spec: map Docker API/connection errors to a clean ResumeError/409
        'source container state could not be verified' — refuse, never proceed.

        ``ConnectionError`` is covered explicitly because it does NOT subclass
        ``DockerException``; catching only Docker's own base class would let an
        unreachable daemon escape as a 500 again.
        """
        source = make_source(tmp_path)
        report = GuardReport()

        with pytest.raises(ResumeError) as excinfo:
            _guard_source_container(source, self._client_raising(exc), report)

        assert excinfo.value.reason == ResumeAbortReason.SOURCE_STATE_UNVERIFIED
        assert "could not be verified" in excinfo.value.message
        # The refusal is recorded, not just raised: the guard report is what the
        # 409 body shows the operator.
        assert report.passed is False
        assert excinfo.value.guard_report is report

    def test_not_found_still_passes(self, tmp_path):
        """``NotFound`` subclasses ``APIError``, so it would be swallowed by the
        new mapping if the arms were ordered the other way. It must keep its
        current meaning: verified absent -> the on-disk dir is a valid source."""
        source = make_source(tmp_path)
        report = GuardReport()

        _guard_source_container(source, self._client_raising(NotFound("no such container")), report)

        assert report.passed is True

    def test_stopped_container_still_passes(self, tmp_path):
        """The mapping must not disturb the ordinary verified-quiesced path."""
        source = make_source(tmp_path)
        report = GuardReport()
        client = MagicMock()
        client.containers.get.return_value = MagicMock(status="exited")

        _guard_source_container(source, client, report)

        assert report.passed is True

    def test_programming_error_is_not_swallowed(self, tmp_path):
        """A blanket ``except Exception`` would rebrand our own bugs as a tidy
        'the daemon could not be reached' 409. Only docker-client exceptions map;
        a TypeError must surface as itself."""
        source = make_source(tmp_path)
        report = GuardReport()

        with pytest.raises(TypeError):
            _guard_source_container(source, self._client_raising(TypeError("bad call")), report)
