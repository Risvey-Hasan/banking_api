import asyncio
import logging

import redis.asyncio as aioredis

from app.config import settings

log = logging.getLogger(__name__)

_client: aioredis.Redis | None = None


async def connect() -> None:
    global _client
    _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    log.info("redis client created")


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