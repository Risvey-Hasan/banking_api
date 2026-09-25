from decimal import Decimal

from tests.helpers import (
    new_id,
    payload,
    settled,
    wait_for_status,
    wait_for_terminal,
)


async def _completed_debit(client, account, amount="250.75") -> str:
    tx = new_id()
    await client.post("/transactions", json=payload(tx, account, "DEBIT", amount))
    assert await wait_for_terminal(client, tx) == "COMPLETED"
    return tx


async def test_reversal_restores_the_balance(client, account):
    tx = await _completed_debit(client, account)
    assert await settled(client, account) == Decimal("749.25")

    r = await client.post(f"/transactions/{tx}/reverse")
    assert r.status_code == 202

    assert await wait_for_terminal(client, f"REV-{tx}") == "COMPLETED"
    await wait_for_status(client, tx, "REVERSED", 5.0)
    assert await settled(client, account) == Decimal("1000.00")


async def test_duplicate_reversal_credits_once(client, account):
    tx = await _completed_debit(client, account)

    first = await client.post(f"/transactions/{tx}/reverse")
    second = await client.post(f"/transactions/{tx}/reverse")
    third = await client.post(f"/transactions/{tx}/reverse")

    assert first.status_code == 202
    assert second.status_code == 200
    assert third.status_code == 200

    assert await wait_for_terminal(client, f"REV-{tx}") == "COMPLETED"
    assert await settled(client, account) == Decimal("1000.00")


async def test_out_of_order_converges_to_zero_effect(client, account):
    """Section 8: REVERSAL, DEBIT, DEBIT, REVERSAL, DEBIT -> net effect 0."""
    tx = new_id()
    body = payload(tx, account, "DEBIT", "250.75")

    await client.post(f"/transactions/{tx}/reverse")   # before the original exists
    await client.post("/transactions", json=body)
    await client.post("/transactions", json=body)
    await client.post(f"/transactions/{tx}/reverse")
    await client.post("/transactions", json=body)

    # COMPLETED is terminal for the debit but intermediate for one about to be
    # reversed. Wait on the reversal: it settles the original in the same
    # database transaction, so REVERSED is guaranteed once REV- is COMPLETED.
    assert await wait_for_terminal(client, f"REV-{tx}", 60.0) == "COMPLETED"
    await wait_for_status(client, tx, "REVERSED", 5.0)
    assert await settled(client, account) == Decimal("1000.00")


async def test_reversing_a_rejected_transaction_is_not_applicable(client, account):
    tx = new_id()
    await client.post("/transactions", json=payload(tx, account, "DEBIT", "9999.00"))
    assert await wait_for_terminal(client, tx) == "REJECTED"

    await client.post(f"/transactions/{tx}/reverse")
    assert await wait_for_terminal(client, f"REV-{tx}", 60.0) == "NOT_APPLICABLE"
    assert await settled(client, account) == Decimal("1000.00")


async def test_a_reversal_cannot_be_reversed(client, account):
    tx = await _completed_debit(client, account)
    await client.post(f"/transactions/{tx}/reverse")
    assert await wait_for_terminal(client, f"REV-{tx}") == "COMPLETED"

    r = await client.post(f"/transactions/REV-{tx}/reverse")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "NOT_REVERSIBLE"