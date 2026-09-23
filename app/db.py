import asyncio
import logging
import asyncpg
from contextlib import asynccontextmanager

from app.config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

class DatabaseUnavailable(Exception):
    """PostgreSQL cannot be reached right now."""


_CONNECTIVITY = (
    OSError,
    asyncio.TimeoutError,
    asyncpg.InterfaceError,
    asyncpg.exceptions.PostgresConnectionError,
    asyncpg.exceptions.CannotConnectNowError,
    asyncpg.exceptions.TooManyConnectionsError,
)


async def connect() -> None:
    """Create the connection pool. Never raises — a dead database must not
    prevent the process from starting, or it cannot report that it is dead."""
    global _pool
    try:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.pg_pool_min,
            max_size=settings.pg_pool_max,
            command_timeout=settings.pg_command_timeout,
        )
        log.info("postgres pool created")
    except (OSError, asyncpg.PostgresError, asyncio.TimeoutError):
        log.exception("postgres unreachable at startup; starting degraded")
        _pool = None


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("postgres pool is not available")
    return _pool


@asynccontextmanager
async def acquire():
    """Acquire a connection, translating connectivity failures into
    DatabaseUnavailable so the API answers 503 rather than 500."""
    if _pool is None:
        raise DatabaseUnavailable("connection pool is not available")
    try:
        async with _pool.acquire() as conn:
            yield conn
    except _CONNECTIVITY as exc:
        raise DatabaseUnavailable(str(exc)) from exc


async def ping() -> bool:
    if _pool is None:
        return False
    try:
        async with asyncio.timeout(settings.health_probe_timeout):
            async with _pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
        return True
    except Exception:
        log.warning("postgres ping failed", exc_info=True)
        return False