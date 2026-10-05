import os
import asyncio
import uuid
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app import account_number, metrics

# Overridable so the suite can run from the host (default) or from inside
# the compose network (TXNSVC_API=http://api:8000).
API = os.environ.get("TXNSVC_API", "http://localhost:8000")


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


async def _enrol() -> dict:
    """Sign up a brand-new customer and return everything a test needs.

    One account per user, created by sign-up -- so there is no longer a
    POST /accounts to call, and "a fresh account" now means "a fresh person".
    That is a better fixture anyway: every test gets its own identity as well as
    its own money, so no test can see another's rows even by accident.
    """
    tag = uuid.uuid4().hex[:12]
    credentials = {
        "full_name": f"Test Customer {tag}",
        "email": f"suite-{tag}@example.com",
        "password": "a-long-enough-password",
    }

    async with httpx.AsyncClient(base_url=API, timeout=30.0) as c:
        r = await c.post("/auth/signup", json=credentials)
        assert r.status_code == 201, r.text
        created = r.json()

        r = await c.post("/auth/token", json={
            "email": credentials["email"],
            "password": credentials["password"],
        })
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]

    return {
        "user_id": created["user"]["id"],
        "account_id": created["account"]["id"],
        "account_number": created["account"]["account_number"],
        "full_name": credentials["full_name"],
        "email": credentials["email"],
        "password": credentials["password"],
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest_asyncio.fixture
async def customer():
    return await _enrol()


@pytest_asyncio.fixture
async def other_customer():
    """A second, unrelated customer -- for everything that must NOT be allowed."""
    return await _enrol()


@pytest_asyncio.fixture
def auth_headers(customer):
    """For tests that build their own client to drive real concurrency."""
    return customer["headers"]


@pytest_asyncio.fixture
async def client(customer):
    async with httpx.AsyncClient(
        base_url=API, timeout=60.0, headers=customer["headers"]
    ) as c:
        yield c


@pytest_asyncio.fixture
async def other_client(other_customer):
    async with httpx.AsyncClient(
        base_url=API, timeout=60.0, headers=other_customer["headers"]
    ) as c:
        yield c


@pytest_asyncio.fixture
async def anonymous():
    """No credential at all."""
    async with httpx.AsyncClient(base_url=API, timeout=30.0) as c:
        yield c


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(dsn=settings.database_url)
    try:
        yield c
    finally:
        await c.close()


@pytest_asyncio.fixture
def account(customer):
    """The one account sign-up opened, holding the configured opening balance."""
    return customer["account_id"]


async def raw_user(conn) -> int:
    """A user created by direct SQL, for tests that never log in."""
    tag = uuid.uuid4().hex[:12]
    return await conn.fetchval(
        "INSERT INTO users (full_name, email, password_hash) "
        "VALUES ($1, $2, 'not-a-real-hash') RETURNING id",
        f"Raw Fixture {tag}", f"raw-{tag}@example.com",
    )


async def raw_account(conn, balance: str = "0.00") -> int:
    """An account created by direct SQL, bypassing the API entirely.

    For constraint tests, which must reach the schema without a round trip. It
    has to invent a user and an account number too, because both are NOT NULL
    now -- an account with no owner is no longer representable, which is the
    point of that migration.

    Beware the default: a funded account made this way holds money with no
    ledger entry to explain it, and would fail the session-end reconciliation
    forever. Pass a balance only when the caller also writes the matching entry.
    """
    user_id = await raw_user(conn)
    return await conn.fetchval(
        "INSERT INTO accounts (currency, balance, owner_id, account_number) "
        "VALUES ('SAR', $1, $2, $3) RETURNING id",
        Decimal(balance), user_id, account_number.generate(),
    )


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