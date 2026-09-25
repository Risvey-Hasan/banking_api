import asyncio

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app import metrics

API = "http://localhost:8000"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def stack_is_up():
    """Fail fast and clearly if the compose stack isn't running."""
    async with httpx.AsyncClient(base_url=API, timeout=5.0) as c:
        for _ in range(30):
            try:
                r = await c.get("/health")
                if r.status_code == 200 and r.json()["status"] == "healthy":
                    return
            except Exception:
                pass
            await asyncio.sleep(1.0)
    raise RuntimeError(
        "Stack is not healthy at " + API +
        " — run: docker compose up -d --build --scale worker=2"
    )


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(base_url=API, timeout=60.0) as c:
        yield c


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(dsn=settings.database_url)
    try:
        yield c
    finally:
        await c.close()


@pytest_asyncio.fixture
async def account(client):
    """A fresh SAR account with 1000.00, so tests never share state."""
    r = await client.post(
        "/accounts", json={"currency": "SAR", "initial_balance": "1000.00"}
    )
    assert r.status_code == 201
    return r.json()["id"]


@pytest_asyncio.fixture
async def empty_account(client):
    r = await client.post(
        "/accounts", json={"currency": "SAR", "initial_balance": "0.00"}
    )
    assert r.status_code == 201
    return r.json()["id"]


@pytest_asyncio.fixture(scope="session", autouse=True)
async def ledger_reconciles_at_end(stack_is_up):
    """After the whole suite, every account must still agree with its ledger."""
    yield
    c = await asyncpg.connect(dsn=settings.database_url)
    try:
        mismatches = await c.fetchval(metrics._MISMATCHES)
    finally:
        await c.close()
    assert mismatches == 0, f"{mismatches} account(s) disagree with their ledger"