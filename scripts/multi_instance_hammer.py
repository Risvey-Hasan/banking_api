"""Section 5, across API instances: 1000 concurrent duplicates spread over
three separate API containers must produce exactly one accepted transaction."""

import asyncio
import uuid
from collections import Counter

import httpx

BASES = [
    "http://localhost:8000",
    "http://localhost:8001",
    "http://localhost:8002",
]
N = 1000
LIMITS = httpx.Limits(max_connections=60, max_keepalive_connections=60)


async def main() -> None:
    clients = [
        httpx.AsyncClient(base_url=b, timeout=120.0, limits=LIMITS) for b in BASES
    ]
    try:
        # Confirm each port is a distinct, healthy instance.
        for base, c in zip(BASES, clients):
            r = await c.get("/health")
            print(f"{base} -> {r.status_code} {r.json()['status']}")

        account = (
            await clients[0].post(
                "/accounts", json={"currency": "SAR", "initial_balance": "10000.00"}
            )
        ).json()["id"]

        tx = f"TX-MI-{uuid.uuid4().hex[:12].upper()}"
        body = {
            "transaction_id": tx, "device_id": "DEV-MI", "account_id": account,
            "type": "DEBIT", "amount": "250.75", "currency": "SAR",
            "timestamp": "2026-09-25T10:31:44Z",
        }

        results = await asyncio.gather(
            *(
                clients[i % len(clients)].post("/transactions", json=body)
                for i in range(N)
            ),
            return_exceptions=True,
        )

        codes = Counter(
            r.status_code if not isinstance(r, Exception) else type(r).__name__
            for r in results
        )
        print(f"\ntransaction : {tx}")
        print(f"account     : {account}")
        print(f"codes       : {dict(codes)}")
        print(f"accepted    : {codes[202]}   <- must be exactly 1")

        final = (await clients[0].get(f"/accounts/{account}")).json()
        print(f"balance     : {final['settled_balance']}   <- must be 9749.2500")
    finally:
        await asyncio.gather(*(c.aclose() for c in clients))


if __name__ == "__main__":
    asyncio.run(main())