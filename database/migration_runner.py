"""Online-migration engine routing (CDX-013 / CDX-R05).

Extracted from ``alembic/env.py`` so the sync-vs-async engine selection is unit
testable. ``env.py`` runs migrations at import time (that is how alembic loads
it), so its functions cannot be imported and exercised in isolation; this module
can. The production ``run_migrations_online`` is a one-line delegate to
``run_online_migrations`` here.

The routing is by DIALECT, never a string guess: a ``postgresql+asyncpg`` URL
requires the async engine, a ``sqlite`` (or ``postgresql+psycopg2``) URL the sync
one. Picking the wrong one fails before any migration is applied — the exact
failure CDX-R05 flagged as untested.
"""
from typing import Callable, Optional

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url


def is_async_url(url: str) -> bool:
    """True when the URL's dialect requires an async driver (e.g. asyncpg)."""
    return bool(getattr(make_url(url).get_dialect(), "is_async", False))


def _run_async_migrations(url: str, do_run_migrations: Callable) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run() -> None:
        connectable = create_async_engine(url, poolclass=pool.NullPool)
        try:
            async with connectable.connect() as connection:
                await connection.run_sync(do_run_migrations)
                await connection.commit()
        finally:
            await connectable.dispose()

    asyncio.run(_run())


def run_online_migrations(
    url: str,
    do_run_migrations: Callable,
    *,
    sync_engine_factory: Callable = create_engine,
    async_runner: Optional[Callable] = None,
) -> None:
    """Apply migrations against ``url``, routing by the URL's dialect.

    An async dialect goes through the asyncio-based runner; a sync dialect
    through a plain synchronous engine. The two injectable seams
    (``sync_engine_factory``, ``async_runner``) exist so a test can prove the
    async URL never constructs the synchronous engine, and vice versa, without a
    live database.
    """
    if is_async_url(url):
        (async_runner or _run_async_migrations)(url, do_run_migrations)
        return
    connectable = sync_engine_factory(url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            do_run_migrations(connection)
            connection.commit()
    finally:
        connectable.dispose()
