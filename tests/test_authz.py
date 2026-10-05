"""Authorisation: can one customer reach another's money?

Every refusal asserted here is a 404 rather than a 403. A 403 confirms the
resource exists, and account ids are small consecutive integers -- so a 403
would turn each of these endpoints into an enumerator. "Not yours" and "not
there" must be indistinguishable from outside.

These tests are mostly NEGATIVE, which is a weakness worth naming: a broken
scope refuses everything, so a suite of refusals passes whether authorisation
works or is merely broken. That is exactly how the user-id/account-id mix-up in
_SELECT_SCOPED survived its first run. The positive cases at the end are the
ones that distinguish the two.
"""

from decimal import Decimal

from tests.helpers import (
    new_id,
    payload,
    reversal_id,
    reverse,
    settled,
    wait_for_terminal,
)


# ------------------------------------------------------------ no credential

async def test_every_endpoint_refuses_an_anonymous_caller(anonymous, account):
    for method, path in (
        ("GET", "/accounts"),
        ("GET", f"/accounts/{account}"),
        ("GET", f"/accounts/{account}/transactions"),
        ("GET", f"/transactions/{new_id()}"),
        ("GET", "/auth/me"),
    ):
        r = await anonymous.request(method, path)
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
        assert r.headers.get("www-authenticate") == "Bearer"

    r = await anonymous.post("/transactions", json=payload(new_id(), account))
    assert r.status_code == 401

    r = await anonymous.post(f"/transactions/{new_id()}/reverse",
                             json={"account_id": account})
    assert r.status_code == 401


async def test_health_and_metrics_stay_public(anonymous):
    """Deliberately unauthenticated: an operator must be able to see the
    service's state without holding a customer's credential."""
    assert (await anonymous.get("/health")).status_code == 200
    assert (await anonymous.get("/metrics")).status_code == 200


# -------------------------------------------------- across the owner boundary

async def test_a_customer_cannot_read_another_customers_account(
    other_client, account
):
    r = await other_client.get(f"/accounts/{account}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"

    r = await other_client.get(f"/accounts/{account}/transactions")
    assert r.status_code == 404


async def test_a_missing_account_is_indistinguishable_from_someone_elses(
    other_client, account
):
    theirs = await other_client.get(f"/accounts/{account}")
    absent = await other_client.get("/accounts/99999999")
    assert theirs.status_code == absent.status_code == 404
    assert theirs.json()["error"]["code"] == absent.json()["error"]["code"]


async def test_a_customer_cannot_move_another_customers_money(
    other_client, account
):
    for kind in ("DEBIT", "CREDIT"):
        r = await other_client.post(
            "/transactions", json=payload(new_id(), account, kind, "10.00")
        )
        assert r.status_code == 404, kind


async def test_a_customer_cannot_read_another_customers_transaction(
    client, other_client, account
):
    tx = new_id()
    assert (await client.post(
        "/transactions", json=payload(tx, account, "DEBIT", "10.00")
    )).status_code == 202

    assert (await other_client.get(f"/transactions/{tx}")).status_code == 404
    # The owner can, which is what proves the scope is doing work rather than
    # refusing everything.
    assert (await client.get(f"/transactions/{tx}")).status_code == 200


async def test_a_reversal_must_claim_an_account_the_caller_owns(
    client, other_client, account
):
    tx = new_id()
    await client.post("/transactions", json=payload(tx, account, "DEBIT", "10.00"))
    assert await wait_for_terminal(client, tx) == "COMPLETED"

    # No claim at all.
    r = await client.post(f"/transactions/{tx}/reverse")
    assert r.status_code == 422

    # A claim on an account the caller does not own.
    r = await reverse(client, tx, 99999999)
    assert r.status_code == 404


async def test_a_squatter_cannot_block_or_steal_a_reversal(
    client, other_client, account, other_customer
):
    """The reversal id includes the claimed account, so a squatter gets their
    own row rather than the owner's only one -- and the worker kills it."""
    tx = new_id()
    await client.post("/transactions", json=payload(tx, account, "DEBIT", "100.00"))
    assert await wait_for_terminal(client, tx) == "COMPLETED"

    squatter_account = other_customer["account_id"]
    before = await settled(other_client, squatter_account)

    # The door cannot refuse this: it cannot know whose the original is, because
    # section 8 allows the original not to exist yet.
    r = await reverse(other_client, tx, squatter_account)
    assert r.status_code == 202

    # The owner is not locked out.
    r = await reverse(client, tx, account)
    assert r.status_code == 202
    assert await wait_for_terminal(client, reversal_id(account, tx)) == "COMPLETED"
    assert await settled(client, account) == Decimal("1000.00")

    # And the squatter's reversal dies without moving anything.
    squat = reversal_id(squatter_account, tx)
    assert await wait_for_terminal(other_client, squat) == "NOT_APPLICABLE"
    assert await settled(other_client, squatter_account) == before


# ------------------------------------------------------------- the own-data path

async def test_a_customer_sees_exactly_their_own_account(client, account):
    r = await client.get("/accounts")
    assert r.status_code == 200
    listed = r.json()
    assert [a["id"] for a in listed] == [account]
    assert listed[0]["account_number"].isdigit()
    assert len(listed[0]["account_number"]) == 12


async def test_a_transaction_id_taken_by_someone_else_is_refused_without_disclosure(
    client, other_client, account, other_customer
):
    """transaction_id is a GLOBAL primary key, so one customer can collide with
    another's. The conflict must not hand over the other customer's row."""
    tx = new_id()
    await client.post("/transactions", json=payload(tx, account, "DEBIT", "10.00"))

    r = await other_client.post(
        "/transactions",
        json=payload(tx, other_customer["account_id"], "DEBIT", "10.00"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRANSACTION_ID_UNAVAILABLE"
    assert "amount" not in r.json()["error"].get("details", {})
