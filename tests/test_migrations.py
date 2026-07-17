"""Phase 9 (CDX-013): versioned migrations + fatal migration state.

Covers, against REAL sqlite databases driven by the REAL alembic scaffold
(no mocked alembic, no mocked schema — every CREATE/ALTER executes):

  * `alembic upgrade head` on an empty database builds the whole schema, and
    the result matches database/models.py exactly (autogenerate parity).
  * The baseline (0001) is the DEPLOYED pre-phase-7/8 schema and deliberately
    does NOT carry the phase 7/8 columns, so a stamped production database is
    BEHIND head and the fatal check makes it apply 0002 rather than silently
    running without the retirement evidence columns or the fill dedup index.
  * The startup check is fatal when the alembic_version table is absent, when
    the database is behind head, and when the check cannot be performed at
    all; it passes only at head. Exercised through the REAL startup entrypoint
    (AsyncDatabaseManager.verify_schema_at_head), not a helper.
  * The dead `_drop_hummingbot_tables` DROPs and the ad-hoc startup ALTERs are
    gone and unreferenced, and startup no longer builds schema with create_all.

Test authenticity: expectations come from the spec (the phase brief and the
engine's stack invocation), not from running the implementation. The env has
no async sqlite driver, so the async startup surface is adapted onto a real
sync sqlite Connection (the pattern established in test_retirement_fsm.py) —
the real check function runs against a real database.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from database.connection import AsyncDatabaseManager
from database.migration_state import (
    BASELINE_REVISION,
    MigrationStateError,
    alembic_config,
    check_schema_at_head,
    get_head_revision,
)
from database.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]

# The phase 7/8 schema. The DEPLOYED image (rev b3aad361) has none of it: it
# must be absent at the baseline and present at head, or `alembic stamp
# 0001_baseline` on production would claim "up to date" while these are
# missing. Spec-derived, not implementation-derived.
PHASE_78_BOT_RUN_COLUMNS = {"retirement_status", "retirement_evidence"}
PHASE_78_TRADE_COLUMNS = {"account_name", "connector_name", "exchange_trade_id"}
DEDUP_INDEX = "uq_trade_scoped_exchange_trade_id"


# ---------------------------------------------------------------------------
# Real-DB plumbing
# ---------------------------------------------------------------------------

def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'api.db').as_posix()}"


def upgrade_to(tmp_path: Path, revision: str) -> str:
    """Run the REAL alembic upgrade against a real sqlite file."""
    url = db_url(tmp_path)
    command.upgrade(alembic_config(url), revision)
    return url


class RunSyncConnAdapter:
    """Awaitable facade over a sync Connection (no async sqlite driver here)."""

    def __init__(self, conn):
        self._conn = conn

    async def run_sync(self, fn, *args, **kwargs):
        return fn(self._conn, *args, **kwargs)


class SyncEngineAdapter:
    def __init__(self, engine):
        self._engine = engine

    @asynccontextmanager
    async def connect(self):
        with self._engine.connect() as conn:
            yield RunSyncConnAdapter(conn)


def startup_manager(url: str) -> AsyncDatabaseManager:
    """The real AsyncDatabaseManager over a real sqlite engine.

    __new__ skips the asyncpg-only __init__; every statement the startup check
    issues still executes against the real database.
    """
    mgr = AsyncDatabaseManager.__new__(AsyncDatabaseManager)
    mgr.engine = SyncEngineAdapter(create_engine(url))
    return mgr


# ===========================================================================
# upgrade head builds the schema
# ===========================================================================

class TestUpgradeBuildsSchema:

    def test_empty_database_upgrade_head_builds_full_schema(self, tmp_path):
        """Spec: an empty database + `alembic upgrade head` = the full schema."""
        url = upgrade_to(tmp_path, "head")
        tables = set(inspect(create_engine(url)).get_table_names())

        # Every table the models define must exist (expected set derived from
        # the model metadata = the schema's definition, not from the DB).
        assert set(Base.metadata.tables) <= tables
        assert "alembic_version" in tables

    def test_schema_at_head_matches_models_exactly(self, tmp_path):
        """Schema parity: autogenerate against a database at head is empty.

        This is what makes the baseline trustworthy — if a revision drifts from
        database/models.py in any column, type, index OR server default, this
        fails. compare_server_default is enabled explicitly (it is OFF by
        default in alembic); without it a wrong DEFAULT in a migration would
        slip past this "exact" check (CDX-R04).
        """
        url = upgrade_to(tmp_path, "head")
        with create_engine(url).connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            diff = compare_metadata(context, Base.metadata)
        assert diff == [], f"schema at head drifted from models: {diff}"

    def test_head_is_single_and_baseline_is_its_root(self):
        """One linear head; the runbook's stamp target is a real revision."""
        script_head = get_head_revision()
        cfg = alembic_config()
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(cfg)
        revs = list(script.walk_revisions())
        assert script_head == revs[0].revision
        assert {r.revision for r in revs} >= {BASELINE_REVISION}
        # The baseline is the root of the chain (nothing precedes it).
        base_rev = [r for r in revs if r.revision == BASELINE_REVISION][0]
        assert base_rev.down_revision is None


# ===========================================================================
# The baseline is the DEPLOYED schema — stamping it leaves prod behind head
# ===========================================================================

class TestBaselineIsDeployedSchema:

    def test_baseline_lacks_phase_78_schema_and_head_has_it(self, tmp_path):
        """The load-bearing property of this phase.

        The deployed database (rev b3aad361) has no phase 7/8 columns; the
        baseline reproduces THAT schema. If the baseline instead carried them,
        `alembic stamp 0001_baseline` on production would report "at head"
        with the columns missing — CDX-006 double-counting, silently.
        """
        url = upgrade_to(tmp_path, BASELINE_REVISION)
        insp = inspect(create_engine(url))
        bot_run_cols = {c["name"] for c in insp.get_columns("bot_runs")}
        trade_cols = {c["name"] for c in insp.get_columns("trades")}
        index_names = {i["name"] for i in insp.get_indexes("trades")}

        assert not (PHASE_78_BOT_RUN_COLUMNS & bot_run_cols)
        assert not (PHASE_78_TRADE_COLUMNS & trade_cols)
        assert DEDUP_INDEX not in index_names

        # ... and 0002 supplies all of it.
        command.upgrade(alembic_config(url), "head")
        insp = inspect(create_engine(url))
        assert PHASE_78_BOT_RUN_COLUMNS <= {c["name"] for c in insp.get_columns("bot_runs")}
        assert PHASE_78_TRADE_COLUMNS <= {c["name"] for c in insp.get_columns("trades")}
        assert DEDUP_INDEX in {i["name"] for i in insp.get_indexes("trades")}

    def test_stamped_production_database_is_behind_head_and_refused(self, tmp_path):
        """The production runbook, end to end.

        A pre-existing database stamped at the baseline must be REFUSED (it is
        behind head), which is what forces `alembic upgrade head` to run and
        actually add the phase 7/8 schema. A baseline containing the phase 7/8
        columns would make this database look up-to-date instead.
        """
        url = upgrade_to(tmp_path, BASELINE_REVISION)
        # Simulate the runbook's stamp on a pre-alembic database: drop the
        # version table, then stamp the baseline.
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE alembic_version"))
        command.stamp(alembic_config(url), BASELINE_REVISION)

        mgr = startup_manager(url)
        with pytest.raises(MigrationStateError) as exc:
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                mgr.verify_schema_at_head()
            )
        assert BASELINE_REVISION in str(exc.value)
        assert "upgrade head" in str(exc.value)

        # And after the upgrade the same startup accepts it.
        command.upgrade(alembic_config(url), "head")
        assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            startup_manager(url).verify_schema_at_head()
        ) == get_head_revision()

    def test_dedup_index_enforced_on_the_migrated_legacy_table(self, tmp_path):
        """0002 must install a dedup index that actually REJECTS a duplicate
        scoped fill on a pre-existing, populated trades table — the money
        guarantee phase 8's ad-hoc CREATE UNIQUE INDEX used to provide."""
        url = upgrade_to(tmp_path, BASELINE_REVISION)
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO orders (id, client_order_id, created_at, updated_at,"
                " account_name, connector_name, trading_pair, trade_type,"
                " order_type, amount, status, filled_amount) "
                "VALUES (1, 'c-1', '2026-01-01', '2026-01-01', 'acct-1', 'nonkyc',"
                " 'XMR-USDT', 'BUY', 'LIMIT', 1, 'OPEN', 0)"
            ))
            # A legacy fill: no scoped identity exists at the baseline.
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price, fee_paid) "
                "VALUES (1, 'legacy-1', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100, 0)"
            ))

        command.upgrade(alembic_config(url), "head")

        with engine.begin() as conn:
            # The legacy row survived, with NULL scoped identity (not fabricated).
            row = conn.execute(text(
                "SELECT account_name, connector_name, exchange_trade_id FROM trades"
                " WHERE trade_id='legacy-1'"
            )).fetchone()
            assert row == (None, None, None)
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price, fee_paid, account_name,"
                " connector_name, exchange_trade_id) "
                "VALUES (1, 'n-1', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100, 0,"
                " 'acct-1', 'nonkyc', 'T1')"
            ))
            # Same account+exchange id, DIFFERENT connector: a legitimate
            # distinct fill, must insert (the constraint is the full triple).
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price, fee_paid, account_name,"
                " connector_name, exchange_trade_id) "
                "VALUES (1, 'n-1b', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100, 0,"
                " 'acct-1', 'other-dex', 'T1')"
            ))

        # The duplicate scoped triple is REJECTED by the migrated index.
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                    " trade_type, amount, price, fee_paid, account_name,"
                    " connector_name, exchange_trade_id) "
                    "VALUES (1, 'n-2', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100, 0,"
                    " 'acct-1', 'nonkyc', 'T1')"
                ))

        # A second id-less fill still inserts (NULLs are distinct).
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price, fee_paid) "
                "VALUES (1, 'legacy-2', '2026-01-02', 'XMR-USDT', 'BUY', 1, 100, 0)"
            ))

    def test_legacy_stopped_row_survives_migration_as_unverified(self, tmp_path):
        """CDX-005 across the migration: a STOPPED row that predates the
        evidence schema must land UNVERIFIED with no fabricated evidence. A
        server_default of 'VERIFIED' in 0002 would bless every legacy stop."""
        url = upgrade_to(tmp_path, BASELINE_REVISION)
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO bot_runs (bot_name, instance_name, deployed_at,"
                " strategy_type, strategy_name, run_status, deployment_status,"
                " account_name, stopped_at) "
                "VALUES ('legacy', 'inst-1', '2026-01-01', 'script', 's', 'STOPPED',"
                " 'DEPLOYED', 'acct-1', '2026-01-01')"
            ))

        command.upgrade(alembic_config(url), "head")

        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT retirement_status, retirement_evidence FROM bot_runs"
                " WHERE bot_name='legacy'"
            )).fetchone()
        assert row[0] == "UNVERIFIED"
        assert row[1] is None


# ===========================================================================
# Fatal migration state at startup
# ===========================================================================

class TestStartupSchemaCheck:

    @pytest.mark.asyncio
    async def test_absent_version_table_is_fatal(self, tmp_path):
        """Spec: version table absent -> FATAL, with the runbook commands.

        Mutation this catches: making the check pass when the version table is
        absent (e.g. `if current is None: return head` in
        migration_state.check_schema_at_head) -> no raise -> this fails.
        """
        url = upgrade_to(tmp_path, "head")
        with create_engine(url).begin() as conn:
            conn.execute(text("DROP TABLE alembic_version"))  # a pre-alembic database

        with pytest.raises(MigrationStateError) as exc:
            await startup_manager(url).verify_schema_at_head()

        message = str(exc.value)
        # The error must be actionable: both runbook commands, by name.
        assert f"alembic stamp {BASELINE_REVISION}" in message
        assert "alembic upgrade head" in message

    @pytest.mark.asyncio
    async def test_database_at_head_passes(self, tmp_path):
        url = upgrade_to(tmp_path, "head")
        assert await startup_manager(url).verify_schema_at_head() == get_head_revision()

    @pytest.mark.asyncio
    async def test_unknown_revision_is_fatal(self, tmp_path):
        """A database stamped with a revision this code does not know (e.g. a
        rollback to an older image) must be refused, not assumed compatible."""
        url = upgrade_to(tmp_path, "head")
        with create_engine(url).begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = 'deadbeef'"))

        with pytest.raises(MigrationStateError) as exc:
            await startup_manager(url).verify_schema_at_head()
        assert "deadbeef" in str(exc.value)

    @pytest.mark.asyncio
    async def test_check_failure_is_fatal_not_ignored(self):
        """Spec: 'check failure -> FATAL'. If the database cannot even be
        asked, the schema state is unknown; the old code logged and served
        anyway. Mutation: swallow the exception and return -> this fails.
        """

        class ExplodingEngine:
            @asynccontextmanager
            async def connect(self):
                raise OSError("connection refused")
                yield  # pragma: no cover

        mgr = AsyncDatabaseManager.__new__(AsyncDatabaseManager)
        mgr.engine = ExplodingEngine()
        with pytest.raises(MigrationStateError) as exc:
            await mgr.verify_schema_at_head()
        assert "connection refused" in str(exc.value)

    def test_check_reports_head_revision_for_a_current_database(self, tmp_path):
        """The check helper itself, on a real connection at head."""
        url = upgrade_to(tmp_path, "head")
        with create_engine(url).connect() as conn:
            assert check_schema_at_head(conn) == get_head_revision()


# ===========================================================================
# The removed machinery stays removed
# ===========================================================================

class TestDeadCodeRemoved:

    def test_dead_hummingbot_table_drops_are_gone(self):
        """CDX-013: `hummingbot_orders` / `hummingbot_trade_fills` /
        `hummingbot_order_status` match no table in either repo — the DROPs
        were no-ops that did not do what they claimed. They must not come back
        (and no startup path may drop tables at all)."""
        assert not hasattr(AsyncDatabaseManager, "_drop_hummingbot_tables")
        for name in ("connection.py", "migration_state.py"):
            source = (REPO_ROOT / "database" / name).read_text(encoding="utf-8")
            assert "hummingbot_orders" not in source
            assert "hummingbot_trade_fills" not in source
            assert "hummingbot_order_status" not in source
            assert "DROP TABLE" not in source.upper()

    def test_startup_no_longer_builds_or_alters_schema(self):
        """create_all and the ad-hoc ALTERs are gone: schema is alembic's job.

        Their failure modes were logged-and-swallowed, which is precisely the
        fail-open CDX-013 reported.
        """
        source = (REPO_ROOT / "database" / "connection.py").read_text(encoding="utf-8")
        assert "create_all" not in source
        assert "ALTER TABLE" not in source.upper()
        assert not hasattr(AsyncDatabaseManager, "create_tables")
        assert not hasattr(AsyncDatabaseManager, "_run_migrations")
        assert not hasattr(AsyncDatabaseManager, "_create_unique_indexes")

    def test_startup_path_calls_the_verification(self):
        """Cheap sanity check: main.py references the verification and no longer
        builds tables. NOT sufficient on its own (a substring survives an
        `if False:` guard) — the real wiring is proven by
        test_lifespan_aborts_when_schema_not_at_head below (CDX-R02)."""
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        assert "verify_schema_at_head()" in main_source
        assert "create_tables()" not in main_source

    @pytest.mark.asyncio
    async def test_lifespan_aborts_when_schema_not_at_head(self, monkeypatch):
        """The REAL FastAPI lifespan must abort startup when the schema check
        fails — not merely mention it (CDX-R02).

        Every startup side effect BEFORE the schema check is neutralised, the
        database manager is made to report a bad schema, and the actual
        `main.lifespan` context is entered. Correct code awaits the check at
        main.py:145 and the MigrationStateError propagates out of startup.

        Mutation this catches: main.py:145 -> `if False: await
        db_manager.verify_schema_at_head()`. The check is then never awaited, no
        MigrationStateError is raised, and this test fails (the older
        substring/helper tests kept passing under exactly that mutation).
        """
        import sys
        from unittest.mock import AsyncMock, MagicMock

        # logfire is an optional runtime dep not installed in this test env, and
        # main.py configures it at import. Stub it so the REAL main module (and
        # its real lifespan) can be imported; everything else in main imports
        # normally here.
        monkeypatch.setitem(sys.modules, "logfire", MagicMock())
        import main

        # Neutralise the pre-check startup side effects (main.py:108-141).
        monkeypatch.setattr(
            main.BackendAPISecurity, "new_password_required", staticmethod(lambda: False)
        )
        monkeypatch.setattr(main, "ETHKeyFileSecretManger", MagicMock())
        monkeypatch.setattr(main.GatewayHttpClient, "get_instance", MagicMock())
        # http gateway URL -> the mTLS cert-sync branch (which imports extra
        # modules) is skipped; the check still runs right after.
        monkeypatch.setattr(main.settings.gateway, "url", "http://localhost:15888")

        # The database reports it is NOT at head.
        failing_manager = MagicMock()
        failing_manager.verify_schema_at_head = AsyncMock(
            side_effect=MigrationStateError("schema behind head")
        )
        monkeypatch.setattr(main, "AsyncDatabaseManager", MagicMock(return_value=failing_manager))

        with pytest.raises(MigrationStateError):
            async with main.lifespan(MagicMock()):
                pytest.fail("startup must abort at the schema check, never reach the body")

        failing_manager.verify_schema_at_head.assert_awaited_once()


# ===========================================================================
# Compatibility with the stacks' migrate service (read-only cross-check)
# ===========================================================================

class TestMigrateServiceCompatibility:

    def test_alembic_ini_is_at_the_invocation_root(self):
        """The stacks run `cd /hummingbot-api && [ -f alembic.ini ] && alembic
        upgrade head` ("hummingbot stack - no vpn":227-229, "- vpn":347-349).
        alembic.ini must therefore sit at the repo root, which is that CWD.
        """
        assert (REPO_ROOT / "alembic.ini").is_file()
        assert (REPO_ROOT / "alembic" / "env.py").is_file()
        assert (REPO_ROOT / "alembic" / "versions").is_dir()

    def test_runtime_image_packages_the_scaffold(self):
        """The scaffold existing in the CHECKOUT is not enough: neither the
        migrate service nor the API container mounts the repo root — they run
        from the built image. If the Dockerfile does not COPY alembic.ini and
        alembic/ into /hummingbot-api, the migrate service takes its silent
        `[ -f alembic.ini ]` skip branch and the API's startup check has no
        scaffold to read (CDX-R01).

        Mutation this catches: reverting the Dockerfile copy line to
        `COPY main.py config.py deps.py ./` (dropping alembic.ini) — the
        assertion on alembic.ini being copied then fails.
        """
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        copy_lines = [ln.strip() for ln in dockerfile.splitlines()
                      if ln.strip().upper().startswith("COPY")]

        def _copied(token: str) -> bool:
            # A COPY whose source list contains `token` (destination is the last
            # arg, so a bare `alembic` dir or `alembic.ini` on the left side).
            return any(token in ln.split()[1:-1] for ln in copy_lines)

        assert _copied("alembic.ini"), (
            "Dockerfile must COPY alembic.ini into the image; without it the "
            "migrate service skips migrations and the API cannot verify head."
        )
        assert _copied("alembic"), "Dockerfile must COPY the alembic/ directory into the image."

    def test_alembic_is_a_declared_runtime_dependency(self):
        """The API imports alembic at startup (database/migration_state.py) and
        the migrate service invokes the alembic CLI. alembic is not a transitive
        dependency of anything in environment.yml, so it must be declared
        explicitly or the rebuilt image would lack it entirely (CDX-R01)."""
        env_yml = (REPO_ROOT / "environment.yml").read_text(encoding="utf-8")
        assert "alembic" in env_yml, "environment.yml must declare the alembic dependency."

    def test_env_resolves_target_db_from_settings(self, monkeypatch, tmp_path):
        """The migrate service passes the database ONLY via DATABASE_URL, and
        alembic.ini deliberately holds no URL, so env.py must resolve the URL
        from the app settings (which read DATABASE_URL). This runs the REAL
        resolver: a real `alembic upgrade head` with sqlalchemy.url unset, and
        proves the migration landed on the settings-resolved database (CDX-R03).

        Mutation this catches: alembic/env.py `_database_url` -> `return
        "sqlite:///wrong.db"`. Migrations then go to wrong.db, the target below
        never gets an alembic_version table, and this test fails. (The old test
        only constructed DatabaseSettings() and never executed the resolver, so
        it passed under exactly that mutation.)
        """
        cfg = alembic_config()
        assert cfg.get_main_option("sqlalchemy.url") in (None, "")

        target = f"sqlite:///{(tmp_path / 'from_settings.db').as_posix()}"
        # env.py does `from config import settings; return settings.database.url`
        # — the singleton the real migrate process resolves through. Point it at
        # the target the way DATABASE_URL would in that process.
        import config

        monkeypatch.setattr(config.settings.database, "url", target)

        command.upgrade(cfg, "head")

        # The RESOLVED database received the migration.
        target_tables = set(inspect(create_engine(target)).get_table_names())
        assert "alembic_version" in target_tables
        assert set(Base.metadata.tables) <= target_tables

    def test_database_url_env_var_feeds_settings(self, monkeypatch):
        """The other half of the contract: DatabaseSettings reads DATABASE_URL,
        so a fresh process picks up the migrate service's env var."""
        target = "postgresql+asyncpg://u:p@h:5432/db"
        monkeypatch.setenv("DATABASE_URL", target)
        from config import DatabaseSettings

        assert DatabaseSettings().url == target


# ===========================================================================
# Online-migration engine routing (sync vs async, by dialect) — CDX-R05
# ===========================================================================

class TestOnlineMigrationRouting:

    def test_is_async_url_classifies_drivers_by_dialect(self):
        from database.migration_runner import is_async_url

        assert is_async_url("postgresql+asyncpg://u:p@h:5432/db") is True
        assert is_async_url("sqlite:///x.db") is False
        assert is_async_url("postgresql+psycopg2://u:p@h:5432/db") is False

    def test_async_url_routes_to_async_runner_never_sync_engine(self):
        """Production runs against postgresql+asyncpg. That URL MUST take the
        async runner; building the synchronous engine for it fails before any
        migration is applied (CDX-R05).

        Mutation this catches: disabling the async branch in
        migration_runner.run_online_migrations (equivalently env.py's old
        `if _is_async_url(url):` -> `if False and ...`) routes the async URL
        into sync_engine_factory, tripping the assertion below.
        """
        from database.migration_runner import run_online_migrations

        calls = {"async": 0}

        def sync_factory_must_not_run(*args, **kwargs):
            raise AssertionError("sync engine must never be built for an async URL")

        def async_runner(url, do_run):
            calls["async"] += 1

        run_online_migrations(
            "postgresql+asyncpg://u:p@h:5432/db",
            do_run_migrations=lambda conn: None,
            sync_engine_factory=sync_factory_must_not_run,
            async_runner=async_runner,
        )
        assert calls["async"] == 1

    def test_sync_url_routes_to_sync_engine_never_async_runner(self, tmp_path):
        """The mirror: a sqlite (sync) URL takes the synchronous engine and
        never the async runner."""
        from database.migration_runner import run_online_migrations

        ran = {"do_run": 0}

        def async_runner_must_not_run(url, do_run):
            raise AssertionError("async runner must never fire for a sync URL")

        def do_run(conn):
            ran["do_run"] += 1

        url = f"sqlite:///{(tmp_path / 'sync.db').as_posix()}"
        run_online_migrations(
            url, do_run_migrations=do_run, async_runner=async_runner_must_not_run
        )
        assert ran["do_run"] == 1
