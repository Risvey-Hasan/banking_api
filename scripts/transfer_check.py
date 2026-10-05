"""Transfers between accounts, end to end.

    python scripts/transfer_check.py
"""

import asyncio
import sys
import uuid
from decimal import Decimal

import httpx

BASE = "http://localhost:8000"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def enrol(c) -> dict:
    tag = uuid.uuid4().hex[:10]
    body = {"full_name": f"Holder {tag}",
            "email": f"tr-{tag}@example.com",
            "password": "a-long-enough-password"}
    created = (await c.post("/auth/signup", json=body)).json()
    tok = (await c.post("/auth/token", json={"email": body["email"],
                                             "password": body["password"]})).json()
    return {"id": created["account"]["id"],
            "number": created["account"]["account_number"],
            "name": body["full_name"],
            "headers": {"Authorization": f"Bearer {tok['access_token']}"}}


async def settle(c, tx_id, headers, tries=60) -> str:
    for _ in range(tries):
        r = await c.get(f"/transactions/{tx_id}", headers=headers)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        if r.json()["status"] not in ("PENDING", "PROCESSING"):
            return r.json()["status"]
        await asyncio.sleep(0.2)
    return "TIMEOUT"


async def balance(c, account_id, headers) -> Decimal:
    r = await c.get(f"/accounts/{account_id}", headers=headers)
    return Decimal(r.json()["settled_balance"])


def transfer(number: str, amount: str) -> dict:
    return {"transfer_id": f"TRF-{uuid.uuid4().hex[:12].upper()}",
            "to_account_number": number, "amount": amount}


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as c:
        a = await enrol(c)
        b = await enrol(c)

        # --- the payee lookup ---------------------------------------------
        r = await c.get(f"/payees/{b['number']}", headers=a["headers"])
        check("the payee lookup names the account holder",
              r.status_code == 200 and r.json()["owner_name"] == b["name"],
              r.text[:80])

        grouped = f"{b['number'][:4]} {b['number'][4:8]} {b['number'][8:]}"
        r = await c.get(f"/payees/{grouped}", headers=a["headers"])
        check("spaces in a typed number are normalised", r.status_code == 200,
              str(r.status_code))

        bad = b["number"][:-1] + str((int(b["number"][-1]) + 1) % 10)
        r = await c.get(f"/payees/{bad}", headers=a["headers"])
        check("a bad check digit is rejected without a lookup",
              r.status_code == 422, str(r.status_code))

        r = await c.get("/payees/000000000000", headers=a["headers"])
        check("an unknown but valid number is 404", r.status_code in (404, 422),
              str(r.status_code))

        r = await c.get(f"/payees/{b['number']}")
        check("the lookup requires authentication", r.status_code == 401,
              str(r.status_code))

        # --- the happy path -----------------------------------------------
        body = transfer(b["number"], "250.00")
        r = await c.post("/transfers", json=body, headers=a["headers"])
        check("a transfer is accepted with 202", r.status_code == 202,
              str(r.status_code))

        state = await settle(c, body["transfer_id"], a["headers"])
        check("it settles as COMPLETED", state == "COMPLETED", state)
        check("the sender was debited",
              await balance(c, a["id"], a["headers"]) == Decimal("750.00"))
        check("the recipient was credited",
              await balance(c, b["id"], b["headers"]) == Decimal("1250.00"))

        r = await c.get(f"/accounts/{b['id']}/transactions", headers=b["headers"])
        check("the recipient sees an incoming credit",
              any(t["transaction_id"] == body["transfer_id"] + "-IN"
                  for t in r.json()))

        # --- idempotency ---------------------------------------------------
        r = await c.post("/transfers", json=body, headers=a["headers"])
        check("resending the same transfer_id is 200, not a second transfer",
              r.status_code == 200, str(r.status_code))
        check("and moved nothing more",
              await balance(c, a["id"], a["headers"]) == Decimal("750.00"))

        # --- refusals -------------------------------------------------------
        r = await c.post("/transfers", json=transfer(a["number"], "10.00"),
                         headers=a["headers"])
        check("transferring to yourself is 422", r.status_code == 422,
              str(r.status_code))

        r = await c.post("/transfers", json=transfer(b["number"], "99999.00"),
                         headers=a["headers"])
        check("an over-balance transfer is still accepted", r.status_code == 202,
              str(r.status_code))
        over = r.json()["transaction_id"]
        state = await settle(c, over, a["headers"])
        check("and settles as REJECTED rather than overdrawing", state == "REJECTED",
              state)
        check("leaving the sender untouched",
              await balance(c, a["id"], a["headers"]) == Decimal("750.00"))

        r = await c.post("/transfers", json=transfer(b["number"], "10.00"))
        check("an unauthenticated transfer is 401", r.status_code == 401,
              str(r.status_code))

        # --- reversing a transfer takes money back from the recipient -------
        rev_body = transfer(b["number"], "100.00")
        await c.post("/transfers", json=rev_body, headers=a["headers"])
        await settle(c, rev_body["transfer_id"], a["headers"])
        check("the second transfer landed",
              await balance(c, b["id"], b["headers"]) == Decimal("1350.00"))

        r = await c.post(f"/transactions/{rev_body['transfer_id']}/reverse",
                         json={"account_id": a["id"]}, headers=a["headers"])
        check("the sender may reverse their transfer", r.status_code == 202,
              str(r.status_code))
        rev_id = f"REV-{a['id']}-{rev_body['transfer_id']}"
        state = await settle(c, rev_id, a["headers"])
        check("the reversal completes", state == "COMPLETED", state)
        check("the recipient was clawed back",
              await balance(c, b["id"], b["headers"]) == Decimal("1250.00"))
        check("the sender was made whole",
              await balance(c, a["id"], a["headers"]) == Decimal("750.00"))

        # --- only the sender may reverse it ---------------------------------
        r = await c.post(f"/transactions/{body['transfer_id']}/reverse",
                         json={"account_id": b["id"]}, headers=b["headers"])
        if r.status_code in (200, 202):
            state = await settle(c, f"REV-{b['id']}-{body['transfer_id']}",
                                 b["headers"])
            check("a recipient's reversal attempt moves nothing",
                  state == "NOT_APPLICABLE", state)
        else:
            check("a recipient cannot reverse the transfer",
                  r.status_code == 404, str(r.status_code))

        # --- the deadlock that ordered locking prevents ---------------------
        # 40 transfers A->B and 40 B->A, all at once. Without ascending-id lock
        # ordering these two directions hold what the other needs.
        a_before = await balance(c, a["id"], a["headers"])
        b_before = await balance(c, b["id"], b["headers"])
        bodies = ([transfer(b["number"], "1.00") for _ in range(40)]
                  + [transfer(a["number"], "1.00") for _ in range(40)])
        heads = [a["headers"]] * 40 + [b["headers"]] * 40

        responses = await asyncio.gather(
            *(c.post("/transfers", json=bd, headers=h)
              for bd, h in zip(bodies, heads)),
            return_exceptions=True,
        )
        accepted = [r for r in responses
                    if not isinstance(r, Exception) and r.status_code == 202]
        check("all 80 crossing transfers were accepted", len(accepted) == 80,
              str(len(accepted)))

        states = await asyncio.gather(
            *(settle(c, bd["transfer_id"], h, 150)
              for bd, h in zip(bodies, heads))
        )
        check("every one settled COMPLETED with no deadlock",
              set(states) == {"COMPLETED"}, str(sorted(set(states))))
        check("and the net effect on both balances is zero",
              await balance(c, a["id"], a["headers"]) == a_before
              and await balance(c, b["id"], b["headers"]) == b_before,
              f"A {a_before} B {b_before}")

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
