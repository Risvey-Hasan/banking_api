import os
import asyncio
from collections import Counter
from decimal import Decimal

import httpx

from tests.helpers import (
    new_id,
    payload,
    settled,
    top_up,
    wait_for_terminal,
)

# Overridable so the suite can run from the host (default) or from inside
# the compose network (TXNSVC_API=http://api:8000).
API = os.environ.get("TXNSVC_API", "http://localhost:8000")
LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=100)


async def test_hundred_concurrent_debits_settle_to_zero(client, auth_headers, account):
    """Section 6: no lost updates, no negative balance, across worker processes.

    The section names SAR 10,000 and 100 debits of SAR 100, so the account is
    topped up to exactly that through the API rather than the numbers being
    adjusted to suit the fixture. The top-up is a real credit with a real
    ledger entry -- the balance is never reached into directly.
    """
    await top_up(client, account, "9000.00")
    assert await settled(client, account) == Decimal("10000.00")

    ids = [new_id() for _ in range(100)]

    async with httpx.AsyncClient(base_url=API, timeout=60.0, limits=LIMITS,
                                 headers=auth_headers) as c:
        responses = await asyncio.gather(
            *(c.post("/transactions", json=payload(tx, account, "DEBIT", "100.00"))
              for tx in ids)
        )
    assert {r.status_code for r in responses} == {202}

    states = Counter(
        await asyncio.gather(*(wait_for_terminal(client, tx, 120.0) for tx in ids))
    )
    assert states == Counter({"COMPLETED": 100})
    assert await settled(client, account) == Decimal("0.00")

    extra = new_id()
    await client.post("/transactions", json=payload(extra, account, "DEBIT", "100.00"))
    assert await wait_for_terminal(client, extra) == "REJECTED"
    assert await settled(client, account) == Decimal("0.00")


async def test_thousand_concurrent_duplicates_apply_once(
    client, auth_headers, conn, account
):
    """Section 5: the same logical transaction, submitted 1000 times at once."""
    tx = new_id()
    body = payload(tx, account, "DEBIT", "250.75")

    async with httpx.AsyncClient(base_url=API, timeout=120.0, limits=LIMITS,
                                 headers=auth_headers) as c:
        results = await asyncio.gather(
            *(c.post("/transactions", json=body) for _ in range(1000)),
            return_exceptions=True,
        )

    codes = Counter(
        r.status_code for r in results if not isinstance(r, Exception)
    )
    assert codes[202] == 1, f"expected exactly one acceptance, got {dict(codes)}"

    rows = await conn.fetchval(
        "SELECT count(*) FROM transactions WHERE transaction_id = $1", tx
    )
    assert rows == 1

    assert await wait_for_terminal(client, tx) == "COMPLETED"
    assert await settled(client, account) == Decimal("749.25")