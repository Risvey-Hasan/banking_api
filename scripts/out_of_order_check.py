"""Section 8: REVERSAL, DEBIT, DEBIT, REVERSAL, DEBIT -> net effect 0."""

import time
from decimal import Decimal

import httpx

BASE = "http://localhost:8000"
AMOUNT = "250.75"


def main() -> None:
    tx = f"TX-OO-{int(time.time())}"
    rev = f"REV-{tx}"

    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        account = client.post(
            "/accounts", json={"currency": "SAR", "initial_balance": "1000.00"}
        ).json()
        aid = account["id"]
        opening = Decimal(account["settled_balance"])
        print(f"account {aid}, opening {opening}")

        debit = {
            "transaction_id": tx, "device_id": "DEV-1", "account_id": aid,
            "type": "DEBIT", "amount": AMOUNT, "currency": "SAR",
            "timestamp": "2026-09-23T10:31:44Z",
        }

        order = [
            ("REVERSAL", lambda: client.post(f"/transactions/{tx}/reverse")),
            ("DEBIT   ", lambda: client.post("/transactions", json=debit)),
            ("DEBIT   ", lambda: client.post("/transactions", json=debit)),
            ("REVERSAL", lambda: client.post(f"/transactions/{tx}/reverse")),
            ("DEBIT   ", lambda: client.post("/transactions", json=debit)),
        ]
        for label, send in order:
            print(f"  sent {label} -> {send().status_code}")

        print("\nwaiting for convergence...")
        for _ in range(60):
            time.sleep(0.5)
            o = client.get(f"/transactions/{tx}").json()["status"]
            r = client.get(f"/transactions/{rev}").json()["status"]
            if o == "REVERSED" and r == "COMPLETED":
                break

        balance = Decimal(
            client.get(f"/accounts/{aid}").json()["settled_balance"]
        )
        rows = client.get(f"/transactions/{tx}").json()

        print(f"\n{tx:20s} -> {rows['status']}")
        print(f"{rev:20s} -> {client.get(f'/transactions/{rev}').json()['status']}")
        print(f"balance  {opening} -> {balance}")
        print(f"net effect: {balance - opening}   "
              f"{'PASS' if balance == opening else 'FAIL'}")


if __name__ == "__main__":
    main()