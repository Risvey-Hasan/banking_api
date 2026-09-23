"""Section 10: the API must never report durable acceptance
while PostgreSQL is unavailable."""

import subprocess
import time

import httpx

BASE = "http://localhost:8000"


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def payload(tx_id: str, account_id: int) -> dict:
    return {
        "transaction_id": tx_id, "device_id": "DEV-1", "account_id": account_id,
        "type": "CREDIT", "amount": "1.00", "currency": "SAR",
        "timestamp": "2026-09-23T10:31:44Z",
    }


def attempt(client, tx_id: str, account_id: int):
    try:
        r = client.post("/transactions", json=payload(tx_id, account_id))
        return r.status_code, r.headers.get("retry-after")
    except Exception as exc:
        return type(exc).__name__, None


def main() -> None:
    stamp = int(time.time())
    attempted = [f"TX-P{stamp}-{i}" for i in range(5)]

    with httpx.Client(base_url=BASE, timeout=20.0) as client:
        aid = client.post(
            "/accounts", json={"currency": "SAR", "initial_balance": "0.00"}
        ).json()["id"]
        print(f"account {aid}")

        print("\n*** stopping postgres ***")
        compose("stop", "postgres")
        time.sleep(2)

        h = client.get("/health")
        print(f"health -> {h.status_code} {h.json()}")

        results = [attempt(client, tx, aid) for tx in attempted]
        codes = [c for c, _ in results]
        print(f"submits while down : {results}")
        print(f"any 202 issued?    : {202 in codes}   <- must be False")

        print("\n*** starting postgres ***")
        compose("start", "postgres")
        for _ in range(60):
            time.sleep(1)
            try:
                if client.get("/health").json()["status"] == "healthy":
                    break
            except Exception:
                pass

        h = client.get("/health")
        print(f"health -> {h.status_code} {h.json()}")

        # Nothing attempted during the outage may exist.
        found = [
            tx for tx in attempted
            if client.get(f"/transactions/{tx}").status_code == 200
        ]
        print(f"rows from refused submits : {found}   <- must be []")

        after = f"TX-P{stamp}-after"
        code, _ = attempt(client, after, aid)
        print(f"submit after recovery     : {code}")

        time.sleep(3)
        balance = client.get(f"/accounts/{aid}").json()["settled_balance"]
        print(f"balance : {balance}   (expected 1.0000)")

        ok = 202 not in codes and not found and code == 202 and balance == "1.0000"
        print("PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()