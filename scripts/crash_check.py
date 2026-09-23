"""Section 7 helper.

  python scripts/crash_check.py setup
  python scripts/crash_check.py verify <transaction_id> <account_id>
"""

import sys
import time

import httpx

BASE = "http://localhost:8000"


def setup() -> None:
    tx = f"TX-CRASH-{int(time.time())}"
    with httpx.Client(base_url=BASE, timeout=15.0) as client:
        account = client.post(
            "/accounts", json={"currency": "SAR", "initial_balance": "1000.00"}
        ).json()
        aid = account["id"]
        client.post(
            "/transactions",
            json={
                "transaction_id": tx, "device_id": "DEV-1", "account_id": aid,
                "type": "DEBIT", "amount": "250.75", "currency": "SAR",
                "timestamp": "2026-09-23T10:31:44Z",
            },
        )
    print(f"transaction : {tx}")
    print(f"account     : {aid}")
    print(f"\nverify with : python scripts/crash_check.py verify {tx} {aid}")


def verify(tx: str, aid: str) -> None:
    with httpx.Client(base_url=BASE, timeout=15.0) as client:
        t = client.get(f"/transactions/{tx}").json()
        a = client.get(f"/accounts/{aid}").json()
        if "status" not in t:
            print(f"{tx} -> not found")
            return
    print(f"{tx} -> {t['status']}")
    print(f"balance      : {a['settled_balance']}  (1000.0000 - 250.75 = 749.2500)")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("setup", "verify"):
        print(__doc__)
        raise SystemExit(1)
    if sys.argv[1] == "setup":
        setup()
    elif len(sys.argv) < 4:
        print("usage: crash_check.py verify <transaction_id> <account_id>")
        raise SystemExit(1)
    else:
        verify(sys.argv[2], sys.argv[3])