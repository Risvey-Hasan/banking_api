from decimal import Decimal

from tests.helpers import new_id, payload, settled, wait_for_terminal


async def test_debit_applies(client, account):
    tx = new_id()
    r = await client.post("/transactions", json=payload(tx, account, "DEBIT", "250.75"))
    assert r.status_code == 202
    assert await wait_for_terminal(client, tx) == "COMPLETED"
    assert await settled(client, account) == Decimal("749.25")


async def test_credit_applies(client, account):
    tx = new_id()
    r = await client.post("/transactions", json=payload(tx, account, "CREDIT", "500.00"))
    assert r.status_code == 202
    assert await wait_for_terminal(client, tx) == "COMPLETED"
    assert await settled(client, account) == Decimal("1500.00")


async def test_insufficient_funds_is_rejected(client, account):
    tx = new_id()
    await client.post("/transactions", json=payload(tx, account, "DEBIT", "5000.00"))
    assert await wait_for_terminal(client, tx) == "REJECTED"
    assert await settled(client, account) == Decimal("1000.00")


async def test_duplicate_submission_returns_existing(client, account):
    tx = new_id()
    body = payload(tx, account, "DEBIT", "100.00")

    first = await client.post("/transactions", json=body)
    second = await client.post("/transactions", json=body)

    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["transaction_id"] == second.json()["transaction_id"]

    assert await wait_for_terminal(client, tx) == "COMPLETED"
    assert await settled(client, account) == Decimal("900.00")


async def test_unknown_account_is_404(client):
    r = await client.post("/transactions", json=payload(new_id(), 99999999))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


async def test_currency_mismatch_is_422(client, account):
    r = await client.post(
        "/transactions",
        json=payload(new_id(), account, "DEBIT", "10.00", currency="USD"),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CURRENCY_MISMATCH"


async def test_non_positive_amount_is_422(client, account):
    r = await client.post(
        "/transactions", json=payload(new_id(), account, "DEBIT", "0.00")
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_unknown_transaction_is_404(client):
    r = await client.get(f"/transactions/{new_id()}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"