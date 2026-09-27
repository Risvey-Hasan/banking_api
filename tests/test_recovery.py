import os
import asyncio
import subprocess
from decimal import Decimal

import httpx
import pytest
import redis.asyncio as aioredis

from app.config import settings
from tests.helpers import new_id, payload, settled, wait_for_terminal

# Overridable so the suite can run from the host (default) or from inside
# the compose network (TXNSVC_API=http://api:8000).
API = os.environ.get("TXNSVC_API", "http://localhost:8000")
LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=100)


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def test_redelivered_entry_is_a_noop(client, account):
    """The apply-time CAS makes at-least-once delivery harmless."""
    tx = new_id()
    await client.post("/transactions", json=payload(tx, account, "DEBIT", "250.75"))
    assert await wait_for_terminal(client, tx) == "COMPLETED"
    before = await settled(client, account)

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        for _ in range(3):
            await r.xadd(settings.redis_stream, {"transaction_id": tx})
    finally:
        await r.aclose()

    await asyncio.sleep(3.0)
    assert await settled(client, account) == before


@pytest.mark.slow
async def test_worker_killed_mid_flight_loses_nothing(client, empty_account):
    """Section 7: SIGKILL the workers during load; every credit lands once."""
    ids = [new_id() for _ in range(200)]

    async with httpx.AsyncClient(base_url=API, timeout=120.0, limits=LIMITS) as c:
        submitting = asyncio.gather(
            *(c.post("/transactions", json=payload(tx, empty_account, "CREDIT", "1.00"))
              for tx in ids)
        )
        await asyncio.sleep(0.2)
        await asyncio.to_thread(compose, "kill", "worker")
        await submitting

    await asyncio.to_thread(compose, "up", "-d", "--scale", "worker=2")

    states = set(
        await asyncio.gather(*(wait_for_terminal(client, tx, 180.0) for tx in ids))
    )
    assert states == {"COMPLETED"}
    assert await settled(client, empty_account) == Decimal("200.00")
