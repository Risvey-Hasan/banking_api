"""Authorisation on the existing API: can one customer touch another's money?

Two freshly signed-up customers, then every way one might reach into the
other's account. Every refusal must be a 404 rather than a 403, because a 403
confirms the resource exists and account ids are small integers.

    python scripts/authz_check.py
"""

import asyncio
import sys
import uuid

import httpx

BASE = "http://localhost:8000"
TS = "2026-10-05T10:31:44Z"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def enrol(c: httpx.AsyncClient) -> dict:
    tag = uuid.uuid4().hex[:10]
    body = {
        "full_name": f"Person {tag}",
        "email": f"authz-{tag}@example.com",
        "password": "a-long-enough-password",
    }
    r = await c.post("/auth/signup", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    r = await c.post("/auth/token",
                     json={"email": body["email"], "password": body["password"]})
    return {
        "account_id": created["account"]["id"],
        "number": created["account"]["account_number"],
        "headers": {"Authorization": f"Bearer {r.json()['access_token']}"},
        "name": body["full_name"],
    }


async def settle(c: httpx.AsyncClient, tx_id: str, headers: dict,
                 tries: int = 60) -> str:
    """Wait for a terminal status, or report why we could not read it.

    Returning "HTTP 404" rather than raising KeyError matters: a scoping bug
    makes every read fail, and a helper that dies on the response shape hides
    which of the two went wrong.
    """
    for _ in range(tries):
        r = await c.get(f"/transactions/{tx_id}", headers=headers)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        state = r.json()["status"]
        if state not in ("PENDING", "PROCESSING"):
            return state
        await asyncio.sleep(0.2)
    return "TIMEOUT"


def tx(account_id: int, kind: str = "DEBIT", amount: str = "10.00") -> dict:
    return {
        "transaction_id": f"AZ-{uuid.uuid4().hex[:12].upper()}",
        "device_id": "DEV-AUTHZ",
        "account_id": account_id,
        "type": kind,
        "amount": amount,
        "currency": "SAR",
        "timestamp": TS,
    }


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as c:
        alice = await enrol(c)
        bob = await enrol(c)
        A, B = alice["headers"], bob["headers"]

        # --- nothing works without a credential ---------------------------
        for method, path in (("GET", "/accounts"),
                             ("GET", f"/accounts/{alice['account_id']}"),
                             ("GET", f"/accounts/{alice['account_id']}/transactions")):
            r = await c.request(method, path)
            check(f"{method} {path} unauthenticated is 401",
                  r.status_code == 401, str(r.status_code))

        r = await c.post("/transactions", json=tx(alice["account_id"]))
        check("POST /transactions unauthenticated is 401", r.status_code == 401,
              str(r.status_code))

        # --- a customer sees exactly one account: their own ----------------
        r = await c.get("/accounts", headers=A)
        listed = r.json()
        check("a customer lists one account",
              r.status_code == 200 and len(listed) == 1, str(len(listed)))
        check("and it is theirs", listed[0]["id"] == alice["account_id"])

        # --- reading across the boundary ----------------------------------
        r = await c.get(f"/accounts/{bob['account_id']}", headers=A)
        check("reading another customer's account is 404", r.status_code == 404,
              str(r.status_code))
        check("and does not confirm it exists",
              r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND")

        r = await c.get(f"/accounts/{bob['account_id']}/transactions", headers=A)
        check("listing another customer's transactions is 404",
              r.status_code == 404, str(r.status_code))

        r = await c.get("/accounts/99999999", headers=A)
        check("a nonexistent account is the SAME 404",
              r.status_code == 404
              and r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND",
              str(r.status_code))

        # --- writing across the boundary ----------------------------------
        r = await c.post("/transactions", json=tx(bob["account_id"]), headers=A)
        check("debiting another customer's account is 404", r.status_code == 404,
              str(r.status_code))

        r = await c.post("/transactions", json=tx(bob["account_id"], "CREDIT"),
                         headers=A)
        check("crediting another customer's account is 404", r.status_code == 404,
              str(r.status_code))

        # --- own account still works --------------------------------------
        own = tx(alice["account_id"], "DEBIT", "25.00")
        r = await c.post("/transactions", json=own, headers=A)
        check("debiting your own account is 202", r.status_code == 202,
              str(r.status_code))

        state = await settle(c, own["transaction_id"], A)
        check("it settles as COMPLETED", state == "COMPLETED", state)

        # --- reading someone else's transaction ---------------------------
        r = await c.get(f"/transactions/{own['transaction_id']}", headers=B)
        check("reading another customer's transaction is 404",
              r.status_code == 404, str(r.status_code))

        # --- reversal requires a claim, and the claim is authorised --------
        r = await c.post(f"/transactions/{own['transaction_id']}/reverse",
                         headers=A)
        check("reversing without an account claim is 422", r.status_code == 422,
              str(r.status_code))

        r = await c.post(f"/transactions/{own['transaction_id']}/reverse",
                         json={"account_id": bob["account_id"]}, headers=A)
        check("claiming an account you do not own is 404", r.status_code == 404,
              str(r.status_code))

        # --- the squat that used to lock Alice out -------------------------
        #
        # Bob asks to reverse Alice's transaction, claiming his own account. The
        # door cannot refuse him: it cannot know whose the original is, because
        # section 8 allows the original not to exist yet. What must hold is that
        # his request neither moves money NOR takes the row Alice needs.
        r = await c.post(f"/transactions/{own['transaction_id']}/reverse",
                         json={"account_id": bob["account_id"]}, headers=B)
        check("a squatter's reversal is accepted at the door",
              r.status_code == 202, str(r.status_code))
        squat_id = f"REV-{bob['account_id']}-{own['transaction_id']}"
        bob_before = (await c.get(f"/accounts/{bob['account_id']}",
                                  headers=B)).json()["settled_balance"]

        # Alice can still reverse her own transaction -- a different row.
        r = await c.post(f"/transactions/{own['transaction_id']}/reverse",
                         json={"account_id": alice["account_id"]}, headers=A)
        check("the owner can still reverse it", r.status_code == 202,
              str(r.status_code))

        rev = f"REV-{alice['account_id']}-{own['transaction_id']}"
        state = await settle(c, rev, A)
        check("the owner's reversal completes", state == "COMPLETED", state)

        r = await c.get(f"/accounts/{alice['account_id']}", headers=A)
        check("and the balance is restored",
              r.json()["settled_balance"] == "1000.0000",
              r.json()["settled_balance"])

        state = await settle(c, squat_id, B)
        check("the squatter's reversal dies as NOT_APPLICABLE",
              state == "NOT_APPLICABLE", state)

        bob_after = (await c.get(f"/accounts/{bob['account_id']}",
                                 headers=B)).json()["settled_balance"]
        check("and moved no money", bob_before == bob_after,
              f"{bob_before} -> {bob_after}")

        # --- a repeat claim is idempotent, not a new reversal ---------------
        r = await c.post(f"/transactions/{own['transaction_id']}/reverse",
                         json={"account_id": alice["account_id"]}, headers=A)
        check("repeating the same claim returns the existing reversal",
              r.status_code == 200
              and r.json()["transaction_id"] == rev,
              str(r.status_code))

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
