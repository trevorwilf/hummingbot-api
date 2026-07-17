import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .migration_state import MigrationStateError, check_schema_at_head

logger = logging.getLogger(__name__)


class AsyncDatabaseManager:
    def __init__(self, database_url: str):
        # Convert postgresql:// to postgresql+asyncpg:// for async support
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://")

        self.engine = create_async_engine(
            database_url,
            # Connection pool settings for async
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,  # Recycle connections after 30 minutes
            pool_pre_ping=True,  # Test connections before using them
            # Engine settings
            echo=False,  # Set to True for SQL query logging
            echo_pool=False,  # Set to True for connection pool logging
            # Connection arguments for asyncpg
            connect_args={
                "server_settings": {"application_name": "hummingbot-api"},
                "command_timeout": 60,
            }
        )
        self.async_session = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

    async def verify_schema_at_head(self) -> str:
        """Refuse to serve unless the database is at the alembic head (CDX-013).

        Replaces the old create_tables(): the API no longer creates or alters
        any schema at startup. Migrations are versioned, transactional and
        applied out-of-band (the stacks' hummingbot-api-migrate service runs
        `alembic upgrade head` before this process starts); startup only
        verifies the result.

        Fatal on: no alembic_version table, a revision behind head, or any
        failure to perform the check at all. All three mean "the schema is not
        known to match this code", and the previous fail-open behaviour
        (log the error, serve anyway) is exactly what CDX-013 reported.
        """
        try:
            async with self.engine.connect() as conn:
                revision = await conn.run_sync(check_schema_at_head)
        except MigrationStateError:
            raise
        except Exception as e:
            # Could not even ask (connection refused, permissions, broken
            # scaffold, ...). Unknown schema state -> refuse, never assume.
            raise MigrationStateError(
                f"Could not verify the database schema version: {e}. Refusing to "
                "start rather than serve against an unverified schema."
            ) from e

        logger.info(f"Database schema verified at alembic head revision '{revision}'")
        return revision

    async def close(self):
        """Close all database connections."""
        await self.engine.dispose()
        logger.info("Database connections closed")

    def get_session(self) -> AsyncSession:
        """Get a new database session."""
        return self.async_session()

    @asynccontextmanager
    async def get_session_context(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get a database session with automatic error handling and cleanup.
        Usage:
            async with db_manager.get_session_context() as session:
                # Use session here
        """
        async with self.async_session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    async def health_check(self) -> bool:
        """
        Check if the database connection is healthy.
        Returns:
            bool: True if connection is healthy, False otherwise.
        """
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False
