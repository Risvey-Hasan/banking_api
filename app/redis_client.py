import asyncio
import logging

import redis.asyncio as aioredis

from app.config import settings

log = logging.getLogger(__name__)

_client: aioredis.Redis | None = None


async def connect(socket_timeout: float | None = None) -> None:
    global _client
    _client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=socket_timeout or settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_connect_timeout,
        health_check_interval=30,
    )
    log.info("redis client created (socket_timeout=%s)",
             socket_timeout or settings.redis_socket_timeout)


async def disconnect() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def client() -> aioredis.Redis:
    if _client is None:
        raise RuntimeError("redis client is not available")
    return _client


async def ping() -> bool:
    if _client is None:
        return False
    try:
        async with asyncio.timeout(settings.health_probe_timeout):
            return await _client.ping()
    except Exception:
        log.warning("redis ping failed", exc_info=True)
        return False