"""Alembic environment (CDX-013).

Runs under two callers:
  * the stacks' hummingbot-api-migrate service — a bare `alembic upgrade head`
    with CWD=/hummingbot-api and DATABASE_URL set, against postgres+asyncpg;
  * the test suite / a human runbook command, against sqlite.

so the URL may name either an async driver (postgresql+asyncpg) or a sync one
(sqlite). Both are handled below; the dialect decides, not a string guess.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection, make_url

# alembic.ini sets prepend_sys_path=. so the app package is importable.
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


def _is_async_url(url: str) -> bool:
    """True when the URL's dialect requires an async driver."""
    return bool(getattr(make_url(url).get_dialect(), "is_async", False))


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Type/default comparison is enabled so a drifted column is caught by
        # the schema-parity check rather than silently ignored.
        compare_type=True,
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


async def _run_async_migrations(url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    connectable = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_do_run_migrations)
            await connection.commit()
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    url = _database_url()
    if _is_async_url(url):
        asyncio.run(_run_async_migrations(url))
        return
    connectable = create_engine(url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            _do_run_migrations(connection)
            connection.commit()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
