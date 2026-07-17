"""Alembic environment (CDX-013).

Runs under two callers:
  * the stacks' hummingbot-api-migrate service — a bare `alembic upgrade head`
    with CWD=/hummingbot-api and DATABASE_URL set, against postgres+asyncpg;
  * the test suite / a human runbook command, against sqlite.

so the URL may name either an async driver (postgresql+asyncpg) or a sync one
(sqlite). Both are handled below; the dialect decides, not a string guess.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

# alembic.ini sets prepend_sys_path=. so the app package is importable.
from database.migration_runner import run_online_migrations
from database.models import Base  # noqa: F401  (registers all models on Base.metadata)

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: when alembic is driven in-process (tests,
    # or any tooling that imports the app), the default True would silently
    # disable every logger the application already configured.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Every model module must be imported before this point for autogenerate to
# see the full schema; database.models defines them all on one Base.
target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the URL the same way the application does.

    An explicit sqlalchemy.url (set programmatically by tests, or by a human
    running against a specific DB) wins; otherwise fall back to the app
    settings, which read DATABASE_URL from the environment — the mechanism the
    migrate service uses.
    """
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from config import settings

    return settings.database.url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Type AND server-default comparison are enabled so a drifted column —
        # including a wrong DEFAULT — is caught by the schema-parity check
        # rather than silently ignored (CDX-R04). compare_server_default is off
        # by default in alembic, which is exactly why the parity guard needs it
        # explicitly.
        compare_type=True,
        compare_server_default=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )


def _do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection (`alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Routing (sync vs async engine, by dialect) lives in an importable helper
    # so it can be falsification-tested without a live database (CDX-R05);
    # env.py itself runs migrations at import and cannot be imported for that.
    run_online_migrations(_database_url(), _do_run_migrations)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
