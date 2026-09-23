"""Section 10: Redis stopped and restored while requests are being submitted.

Durably accepted work must not be silently lost.
"""

import asyncio
import subprocess
import time
from collections import Counter

import httpx

BASE = "http://localhost:8000"
RATE = 10          # requests per second
DURATION = 25      # seconds of submission
STOP_AT, START_AT = 5, 15


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def submit(client, tx_id: str, account_id: int):
    try:
        r = await client.post(
            "/transactions",
            json={
                "transaction_id": tx_id, "device_id": "DEV-1",
                "account_id": account_id, "type": "CREDIT",
                "amount": "1.00", "currency": "SAR",
                "timestamp": "2026-09-23T10:31:44Z",
            },
        )
        return r.status_code, tx_id
    except Exception as exc:
        return type(exc).__name__, tx_id


async def main() -> None:
    stamp = int(time.time())
    accepted: list[str] = []
    outcomes: Counter = Counter()
    health: Counter = Counter()

    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as client:
        account = (
            await client.post(
                "/accounts", json={"currency": "SAR", "initial_balance": "0.00"}
            )
        ).json()
        aid = account["id"]
        print(f"account {aid}, submitting {RATE}/s for {DURATION}s\n")

        for second in range(DURATION):
            if second == STOP_AT:
                print(f"[t={second:2d}s] *** stopping redis ***")
                compose("stop", "redis")
            if second == START_AT:
                print(f"[t={second:2d}s] *** starting redis ***")
                compose("start", "redis")

            tick = time.monotonic()
            batch = [f"TX-R{stamp}-{second:03d}-{i:02d}" for i in range(RATE)]
            for code, tx in await asyncio.gather(
                *(submit(client, tx, aid) for tx in batch)
            ):
                outcomes[code] += 1
                if code in (200, 202):
                    accepted.append(tx)

            try:
                health[(await client.get("/health")).json()["status"]] += 1
            except Exception:
                health["unreachable"] += 1

            await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))

        print(f"\nsubmitted : {sum(outcomes.values())}")
        print(f"accepted  : {len(accepted)}")
        print(f"outcomes  : {dict(outcomes)}")
        print(f"health    : {dict(health)}")

        print("\ndraining...")
        pending = list(accepted)
        deadline = time.monotonic() + 180
        while pending and time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            rs = await asyncio.gather(
                *(client.get(f"/transactions/{tx}") for tx in pending)
            )
            pending = [
                tx for tx, r in zip(pending, rs)
                if r.json()["status"] in ("PENDING", "PROCESSING")
            ]
            print(f"  outstanding: {len(pending)}")

        balance = (await client.get(f"/accounts/{aid}")).json()["settled_balance"]
        expected = f"{len(accepted)}.0000"
        print(f"\nunsettled : {len(pending)}")
        print(f"balance   : {balance}")
        print(f"expected  : {expected}")
        print("PASS" if not pending and balance == expected else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())