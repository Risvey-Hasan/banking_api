import asyncio
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


async def _completed_credit(client, account, amount="500.00") -> str:
    tx = new_id()
    await client.post("/transactions", json=payload(tx, account, "CREDIT", amount))
    assert await wait_for_terminal(client, tx) == "COMPLETED"
    return tx


async def test_reversing_a_credit_debits_and_creates_no_money(client, account):
    """Regression test for decision D12.

    A reversal compensates; it does not repeat. The original schema required
    every reversal to be a CREDIT, so reversing a CREDIT credited the account a
    second time and created money out of nothing.

    Nothing caught it: every other reversal test reverses a debit, and the
    ledger reconciliation stayed at zero mismatches because the bogus credit
    was recorded consistently with the bogus balance. That check asks whether
    the balance and the ledger agree, not whether the ledger is right.
    """
    tx = await _completed_credit(client, account, "500.00")
    assert await settled(client, account) == Decimal("1500.00")

    r = await client.post(f"/transactions/{tx}/reverse")
    assert r.status_code == 202

    assert await wait_for_terminal(client, f"REV-{tx}") == "COMPLETED"
    await wait_for_status(client, tx, "REVERSED", 5.0)

    # The balance the bug produced here was 2000.00.
    assert await settled(client, account) == Decimal("1000.00")

    reversal = (await client.get(f"/transactions/REV-{tx}")).json()
    assert reversal["type"] == "DEBIT"            # the direction it moved money
    assert reversal["reverses_type"] == "CREDIT"  # the direction it opposed


async def test_reversing_a_credit_waits_rather_than_overdrawing(client, conn, account):
    """Invariant I2 outranks the reversal.

    If the credited money has since been spent, the compensating debit cannot
    apply without driving the balance negative. It parks and retries instead,
    and the original stays COMPLETED -- a row marked REVERSED has to mean the
    money actually came back.
    """
    credit = await _completed_credit(client, account, "500.00")
    assert await settled(client, account) == Decimal("1500.00")

    spend = new_id()
    await client.post("/transactions", json=payload(spend, account, "DEBIT", "1500.00"))
    assert await wait_for_terminal(client, spend) == "COMPLETED"
    assert await settled(client, account) == Decimal("0.00")

    await client.post(f"/transactions/{credit}/reverse")
    await asyncio.sleep(2.0)   # several park cycles at 250 ms

    reversal = (await client.get(f"/transactions/REV-{credit}")).json()
    assert reversal["status"] == "PENDING"
    assert reversal["type"] is None          # it has not moved money
    assert await settled(client, account) == Decimal("0.00")

    original = (await client.get(f"/transactions/{credit}")).json()
    assert original["status"] == "COMPLETED"  # NOT 'REVERSED'

    reason = await conn.fetchval(
        "SELECT last_error FROM transactions WHERE transaction_id = $1",
        f"REV-{credit}",
    )
    assert reason == "REVERSAL_FUNDS_UNAVAILABLE"

    # It stays parked until the 24-hour horizon and then dead-letters, counted
    # in /metrics. That is deliberate: a visible piece of work for a human
    # beats a silently wrong balance.


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