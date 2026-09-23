"""Fire N concurrent identical POSTs. Exactly one must be accepted."""

import asyncio
import uuid
from collections import Counter

import httpx

BASE = "http://localhost:8000"
N = 1000


async def main() -> None:
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0, limits=limits) as client:
        account = await client.post(
            "/accounts", json={"currency": "SAR", "initial_balance": "10000.00"}
        )
        account_id = account.json()["id"]

        tx_id = f"TX-{uuid.uuid4().hex[:12].upper()}"
        payload = {
            "transaction_id": tx_id,
            "device_id": "DEV-22",
            "account_id": account_id,
            "type": "DEBIT",
            "amount": "250.75",
            "currency": "SAR",
            "timestamp": "2026-09-23T10:31:44Z",
        }

        results = await asyncio.gather(
            *(client.post("/transactions", json=payload) for _ in range(N)),
            return_exceptions=True,
        )

    codes = Counter()
    failures = 0
    for r in results:
        if isinstance(r, Exception):
            failures += 1
        else:
            codes[r.status_code] += 1

    print(f"transaction_id : {tx_id}")
    print(f"account_id     : {account_id}")
    print(f"status codes   : {dict(codes)}")
    print(f"client errors  : {failures}")
    print()
    print("Verify:")
    print(f"  SELECT count(*) FROM transactions WHERE transaction_id = '{tx_id}';")


if __name__ == "__main__":
    asyncio.run(main())