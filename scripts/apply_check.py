"""End-to-end check of the apply path. Run: python scripts/apply_check.py"""

import time
from decimal import Decimal

import httpx
import redis

BASE = "http://localhost:8000"
REDIS_URL = "redis://localhost:6379/0"
STREAM = "transactions"


def post_tx(client, tx_id, account_id, tx_type, amount):
    return client.post(
        "/transactions",
        json={
            "transaction_id": tx_id,
            "device_id": "DEV-1",
            "account_id": account_id,
            "type": tx_type,
            "amount": amount,
            "currency": "SAR",
            "timestamp": "2026-09-23T10:31:44Z",
        },
    )


def wait_for_terminal(client, tx_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/transactions/{tx_id}").json()["status"]
        if status not in ("PENDING", "PROCESSING"):
            return status
        time.sleep(0.2)
    return "TIMEOUT"


def balance(client, account_id) -> Decimal:
    return Decimal(client.get(f"/accounts/{account_id}").json()["settled_balance"])


def main() -> None:
    stamp = int(time.time())
    with httpx.Client(base_url=BASE, timeout=15.0) as client:
        account = client.post(
            "/accounts", json={"currency": "SAR", "initial_balance": "1000.00"}
        ).json()
        aid = account["id"]
        print(f"account {aid}, opening balance {account['settled_balance']}")

        tx1 = f"TX-A-{stamp}"
        post_tx(client, tx1, aid, "DEBIT", "250.75")
        print(f"{tx1}  ->  {wait_for_terminal(client, tx1):14s} balance={balance(client, aid)}")

        tx2 = f"TX-B-{stamp}"
        post_tx(client, tx2, aid, "DEBIT", "5000.00")
        print(f"{tx2}  ->  {wait_for_terminal(client, tx2):14s} balance={balance(client, aid)}")

        before = balance(client, aid)
        redis.from_url(REDIS_URL, decode_responses=True).xadd(
            STREAM, {"transaction_id": tx1}
        )
        print(f"\nredelivered {tx1} by hand; waiting 3s...")
        time.sleep(3)
        after = balance(client, aid)
        verdict = "OK — no second effect" if before == after else "DUPLICATED"
        print(f"balance before={before}  after={after}  ->  {verdict}")


if __name__ == "__main__":
    main()