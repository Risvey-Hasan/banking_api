import asyncio
import uuid
from decimal import Decimal

import httpx

TIMESTAMP = "2026-09-23T10:31:44Z"


def new_id(prefix: str = "TX") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def payload(tx_id: str, account_id: int, tx_type: str = "DEBIT",
            amount: str = "100.00", currency: str = "SAR") -> dict:
    return {
        "transaction_id": tx_id,
        "device_id": "DEV-TEST",
        "account_id": account_id,
        "type": tx_type,
        "amount": amount,
        "currency": currency,
        "timestamp": TIMESTAMP,
    }


async def wait_for_terminal(client: httpx.AsyncClient, tx_id: str,
                            timeout: float = 30.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        r = await client.get(f"/transactions/{tx_id}")
        if r.status_code == 200:
            state = r.json()["status"]
            if state not in ("PENDING", "PROCESSING"):
                return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"{tx_id} did not reach a terminal status in {timeout}s")


async def settled(client: httpx.AsyncClient, account_id: int) -> Decimal:
    r = await client.get(f"/accounts/{account_id}")
    return Decimal(r.json()["settled_balance"])


async def wait_for_status(client: httpx.AsyncClient, tx_id: str,
                          expected: str, timeout: float = 60.0) -> None:
    """Wait for one specific status, not merely any terminal one."""
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        r = await client.get(f"/transactions/{tx_id}")
        last = r.json().get("status") if r.status_code == 200 else f"HTTP {r.status_code}"
        if last == expected:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{tx_id} was {last!r}, expected {expected!r} within {timeout}s")

def reversal_id(account_id: int, tx_id: str) -> str:
    """The id the API derives for a reversal.

    Includes the claimed account, not just the transaction. With only the
    transaction in it, the first caller to ask owned the only reversal row that
    could exist for it -- so anyone who could guess an id could permanently stop
    its owner from reversing. See app/api/transactions.py.
    """
    return f"REV-{account_id}-{tx_id}"


async def reverse(client: httpx.AsyncClient, tx_id: str, account_id: int):
    """Ask for a reversal, naming the account the caller claims it concerns."""
    return await client.post(f"/transactions/{tx_id}/reverse",
                             json={"account_id": account_id})


async def top_up(client: httpx.AsyncClient, account_id: int, amount: str) -> None:
    """Credit an account and wait for the money to land.

    Sign-up fixes the opening balance, so a test needing a specific starting
    figure earns it through the API rather than reaching into the database. That
    keeps the genesis-credit invariant (D10) true for every account the suite
    creates.
    """
    tx = new_id("TOPUP")
    r = await client.post("/transactions",
                          json=payload(tx, account_id, "CREDIT", amount))
    assert r.status_code == 202, r.text
    assert await wait_for_terminal(client, tx) == "COMPLETED"
