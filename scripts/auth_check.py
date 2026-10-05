"""Sign-up, login, sessions and CSRF -- the whole identity path, end to end.

The automated suite cannot cover this yet (its fixtures still predate auth), so
this script is the proof for now. Every assertion prints PASS or FAIL and the
script exits non-zero if anything fails.

    python scripts/auth_check.py
"""

import asyncio
import sys
import uuid

import httpx

BASE = "http://localhost:8000"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def person() -> dict:
    tag = uuid.uuid4().hex[:10]
    return {
        "full_name": f"Test Person {tag}",
        "email": f"person-{tag}@example.com",
        "password": "a-long-enough-password",
    }


async def main() -> None:
    who = person()

    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as c:
        # --- sign-up --------------------------------------------------------
        r = await c.post("/auth/signup", json=who)
        check("signup returns 201", r.status_code == 201, str(r.status_code))
        body = r.json()
        account = body["account"]
        check("an account was opened", account["id"] > 0)
        check("account number is 12 digits",
              len(account["account_number"]) == 12 and account["account_number"].isascii(),
              account["account_number"])
        check("opening balance is recorded",
              account["settled_balance"] == "1000.0000", account["settled_balance"])
        check("the account carries the owner's name",
              account["owner_name"] == who["full_name"], account["owner_name"])
        check("role defaults to CUSTOMER", body["user"]["role"] == "CUSTOMER")

        # --- no duplicate sign-ups ------------------------------------------
        r = await c.post("/auth/signup", json=who)
        check("duplicate signup is 409", r.status_code == 409, str(r.status_code))
        check("duplicate signup says why",
              r.json()["error"]["code"] == "EMAIL_TAKEN")

        # --- email is normalised, so case cannot duplicate ------------------
        shouty = dict(who, email=who["email"].upper())
        r = await c.post("/auth/signup", json=shouty)
        check("UPPERCASE of a taken email is also 409", r.status_code == 409,
              str(r.status_code))

        # --- validation -----------------------------------------------------
        r = await c.post("/auth/signup", json=dict(person(), password="short"))
        check("a short password is 422", r.status_code == 422, str(r.status_code))
        r = await c.post("/auth/signup", json=dict(person(), email="not-an-email"))
        check("a malformed email is 422", r.status_code == 422, str(r.status_code))

        # --- unauthenticated ------------------------------------------------
        r = await c.get("/auth/me")
        check("/auth/me without a credential is 401", r.status_code == 401,
              str(r.status_code))
        check("401 advertises the scheme",
              r.headers.get("www-authenticate") == "Bearer")

        # --- wrong password -------------------------------------------------
        r = await c.post("/auth/token",
                         json={"email": who["email"], "password": "wrong"})
        check("a wrong password is 401", r.status_code == 401, str(r.status_code))
        wrong_code = r.json()["error"]["code"]

        r = await c.post("/auth/token",
                         json={"email": "nobody-here@example.com", "password": "wrong"})
        check("an unknown email gives the SAME error as a wrong password",
              r.status_code == 401 and r.json()["error"]["code"] == wrong_code,
              f"{r.status_code} {r.json()['error']['code']}")

        # --- bearer ---------------------------------------------------------
        r = await c.post("/auth/token",
                         json={"email": who["email"], "password": who["password"]})
        check("/auth/token returns 200", r.status_code == 200, str(r.status_code))
        token = r.json()["access_token"]
        check("/auth/token sets no cookie", "set-cookie" not in r.headers)

        r = await c.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        check("bearer token identifies the caller",
              r.status_code == 200 and r.json()["user"]["email"] == who["email"],
              str(r.status_code))
        check("/auth/me carries the account",
              r.json()["account"]["account_number"] == account["account_number"])

        r = await c.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
        check("a bogus bearer token is 401", r.status_code == 401, str(r.status_code))

    # --- cookies and CSRF, in a client that keeps a cookie jar -------------
    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as c:
        r = await c.post("/auth/login",
                         json={"email": who["email"], "password": who["password"]})
        check("/auth/login returns 200", r.status_code == 200, str(r.status_code))
        check("the session cookie is httpOnly",
              "httponly" in r.headers.get("set-cookie", "").lower())
        csrf = c.cookies.get("txnsvc_csrf")
        check("a CSRF cookie was issued and is readable", bool(csrf))

        r = await c.get("/auth/me")
        check("the cookie alone authenticates a GET", r.status_code == 200,
              str(r.status_code))

        # A write with the cookie but no CSRF header is what a forged
        # cross-site POST looks like.
        r = await c.post("/auth/logout")
        check("a cookie write without the CSRF header is 403",
              r.status_code == 403, str(r.status_code))
        check("and says why", r.json()["error"]["code"] == "CSRF_FAILED")

        r = await c.post("/auth/logout", headers={"X-CSRF-Token": "wrong-value"})
        check("a mismatched CSRF token is 403", r.status_code == 403,
              str(r.status_code))

        r = await c.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        check("logout with the matching token is 204", r.status_code == 204,
              str(r.status_code))

        r = await c.get("/auth/me")
        check("the session no longer works after logout", r.status_code == 401,
              str(r.status_code))

    # --- revocation is server-side, not merely cookie deletion -------------
    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as c:
        r = await c.post("/auth/token",
                         json={"email": who["email"], "password": who["password"]})
        stolen = r.json()["access_token"]
        await c.post("/auth/logout", headers={"Authorization": f"Bearer {stolen}"})
        r = await c.get("/auth/me", headers={"Authorization": f"Bearer {stolen}"})
        check("a revoked token is dead even for whoever copied it",
              r.status_code == 401, str(r.status_code))

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
