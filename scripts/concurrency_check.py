"""Section 6: 100 concurrent debits of 100.00 against a 10,000.00 balance."""

import asyncio
import time
from collections import Counter

import httpx

BASE = "http://localhost:8000"
N = 100
AMOUNT = "10000.00"


def payload(tx_id: str, account_id: int) -> dict:
    return {
        "transaction_id": tx_id,
        "device_id": "DEV-1",
        "account_id": account_id,
        "type": "DEBIT",
        "amount": AMOUNT,
        "currency": "SAR",
        "timestamp": "2026-09-23T10:31:44Z",
    }


async def terminal_status(client, tx_id, deadline):
    while time.monotonic() < deadline:
        status = (await client.get(f"/transactions/{tx_id}")).json()["status"]
        if status not in ("PENDING", "PROCESSING"):
            return status
        await asyncio.sleep(0.1)
    return "TIMEOUT"


async def balance(client, account_id) -> str:
    return (await client.get(f"/accounts/{account_id}")).json()["settled_balance"]


async def main() -> None:
    stamp = int(time.time())
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)

    async with httpx.AsyncClient(base_url=BASE, timeout=60.0, limits=limits) as client:
        account = (
            await client.post(
                "/accounts", json={"currency": "SAR", "initial_balance": "10000.00"}
            )
        ).json()
        aid = account["id"]
        print(f"account {aid}, opening balance {account['settled_balance']}")

        ids = [f"TX-C{stamp}-{i:03d}" for i in range(N)]

        t0 = time.monotonic()
        await asyncio.gather(
            *(client.post("/transactions", json=payload(tx, aid)) for tx in ids)
        )
        t1 = time.monotonic()

        deadline = time.monotonic() + 120
        statuses = Counter(
            await asyncio.gather(*(terminal_status(client, tx, deadline) for tx in ids))
        )
        t2 = time.monotonic()

        print(f"\naccepted in  : {t1 - t0:.2f}s")
        print(f"processed in : {t2 - t1:.2f}s")
        print(f"statuses     : {dict(statuses)}")
        print(f"balance      : {await balance(client, aid)}")

        extra = f"TX-C{stamp}-EXTRA"
        await client.post("/transactions", json=payload(extra, aid))
        result = await terminal_status(client, extra, time.monotonic() + 30)
        print(f"\nsubsequent debit -> {result}")
        print(f"final balance    : {await balance(client, aid)}")


if __name__ == "__main__":
    asyncio.run(main())