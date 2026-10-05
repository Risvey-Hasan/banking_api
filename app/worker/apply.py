import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from app.config import settings

log = logging.getLogger(__name__)

_CLAIM = """
    UPDATE transactions
       SET status = 'PROCESSING'
     WHERE transaction_id = $1
       AND status = 'PENDING'
    RETURNING transaction_id,
              kind::text AS kind,
              type::text AS type,
              account_id, to_account_id, claimed_account_id,
              amount, currency, reverses_id, parked_since
"""

_DEBIT = """
    UPDATE accounts SET balance = balance - $2, updated_at = now()
     WHERE id = $1 AND balance >= $2
    RETURNING balance
"""

_CREDIT = """
    UPDATE accounts SET balance = balance + $2, updated_at = now()
     WHERE id = $1
    RETURNING balance
"""

_FINALISE = """
    UPDATE transactions
       SET status = $2::tx_status, processed_at = now(), last_error = $3
     WHERE transaction_id = $1
"""

# Locked for the remainder of this database transaction, BEFORE any balance
# changes. A reversal that turns out to be unable to complete must leave the
# original's status untouched, because a row marked REVERSED has to mean the
# money actually came back. Lock order is reversal row (the claim) -> original
# row -> account row; the original's own apply path takes original -> account,
# so no cycle exists.
_ORIGINAL_FOR_REVERSAL = """
    SELECT status::text AS status, kind::text AS kind, type::text AS type,
           account_id, to_account_id, amount, currency
      FROM transactions
     WHERE transaction_id = $1
       FOR UPDATE
"""

_MARK_REVERSED = """
    UPDATE transactions
       SET status = 'REVERSED'
     WHERE transaction_id = $1
       AND status = 'COMPLETED'
    RETURNING transaction_id
"""

# The reversal learns, at settle time, both the direction it moved money and
# the direction it opposed. CHECK reversal_compensates then enforces that they
# differ -- see decision D12.
_SETTLE_REVERSAL = """
    UPDATE transactions
       SET status = 'COMPLETED', processed_at = now(),
           account_id = $2, amount = $3, currency = $4,
           type = $5::tx_type, reverses_type = $6::tx_type
     WHERE transaction_id = $1
"""

_PARK = """
    UPDATE transactions
       SET status = 'PENDING',
           parked_since = COALESCE(parked_since, now()),
           next_attempt_at = now() + make_interval(secs => $2),
           last_error = $3
     WHERE transaction_id = $1
"""

# A reversal compensates; it does not repeat. This mapping is the entire rule,
# and the schema refuses to store a reversal that disagrees with it.
_COMPENSATION = {"DEBIT": "CREDIT", "CREDIT": "DEBIT"}

# Lock BOTH accounts, in ascending id order, before touching either.
#
# This is the one new invariant transfers introduce. A transfer needs two row
# locks, and two locks taken in request order deadlock the moment A->B and B->A
# run at the same time: each transaction holds the row the other is waiting for.
# Ordering by id means every transfer in the system queues for the same two rows
# in the same sequence, so a cycle cannot form. PostgreSQL applies FOR UPDATE
# after the sort, so the ORDER BY is what fixes the acquisition order.
_LOCK_PAIR = """
    SELECT id FROM accounts
     WHERE id = $1 OR id = $2
     ORDER BY id
       FOR UPDATE
"""

# The other side's ledger row, written in the same transaction as the money.
# Its id is derived from the transfer's, so it is idempotent for free.
_LEG = """
    INSERT INTO transactions
        (transaction_id, kind, type, status, account_id, amount, currency,
         device_id, client_ts, accepted_at, processed_at, next_attempt_at)
    VALUES ($1, 'TRANSACTION', $5::tx_type, 'COMPLETED', $2, $3, $4,
            'SYSTEM', now(), now(), now(), now())
    ON CONFLICT (transaction_id) DO NOTHING
"""


async def apply_transaction(conn: asyncpg.Connection, transaction_id: str) -> str | None:
    async with conn.transaction():
        claimed = await conn.fetchrow(_CLAIM, transaction_id)
        if claimed is None:
            return None
        if claimed["kind"] == "REVERSAL":
            return await _apply_reversal(conn, claimed)
        if claimed["kind"] == "TRANSFER":
            return await _apply_transfer(conn, claimed)
        return await _apply_original(conn, claimed)


async def _apply_original(conn: asyncpg.Connection, claimed) -> str:
    tx = claimed["transaction_id"]

    if claimed["type"] == "DEBIT":
        balance = await conn.fetchval(_DEBIT, claimed["account_id"], claimed["amount"])
        if balance is None:
            await conn.execute(_FINALISE, tx, "REJECTED", "INSUFFICIENT_FUNDS")
            return "REJECTED"
    else:
        await conn.fetchval(_CREDIT, claimed["account_id"], claimed["amount"])

    await conn.execute(_FINALISE, tx, "COMPLETED", None)
    return "COMPLETED"


async def _apply_transfer(conn: asyncpg.Connection, claimed) -> str:
    """Move money out of one account and into another, atomically.

    The transfer row IS the sender's debit leg; the recipient's credit row is
    created here, in the same database transaction as both balance changes. So
    either the money left and arrived and both ledger rows exist, or none of it
    happened. There is no state in which it is in flight.
    """
    tx = claimed["transaction_id"]
    sender, recipient = claimed["account_id"], claimed["to_account_id"]
    amount, currency = claimed["amount"], claimed["currency"]

    await conn.fetch(_LOCK_PAIR, sender, recipient)

    # Guarded exactly like any other debit, so a transfer can no more overdraw
    # an account than a plain withdrawal can.
    if await conn.fetchval(_DEBIT, sender, amount) is None:
        await conn.execute(_FINALISE, tx, "REJECTED", "INSUFFICIENT_FUNDS")
        return "REJECTED"

    await conn.fetchval(_CREDIT, recipient, amount)
    await conn.execute(_LEG, f"{tx}-IN", recipient, amount, currency, "CREDIT")
    await conn.execute(_FINALISE, tx, "COMPLETED", None)
    return "COMPLETED"


async def _reverse_transfer(conn: asyncpg.Connection, claimed, original) -> str:
    """Undo a transfer: claw back from the recipient, return it to the sender.

    Two-sided, because the transfer was. Compensating only the sender -- which
    is what the generic path would do, since a transfer's type is DEBIT -- would
    credit money into existence. That is the D12 mistake in a new costume.
    """
    tx = claimed["transaction_id"]
    target = claimed["reverses_id"]
    sender, recipient = original["account_id"], original["to_account_id"]
    amount, currency = original["amount"], original["currency"]

    await conn.fetch(_LOCK_PAIR, sender, recipient)

    # The clawback is guarded. If the recipient has spent it, the reversal waits
    # rather than overdrawing them: invariant I2 outranks the reversal.
    if await conn.fetchval(_DEBIT, recipient, amount) is None:
        return await _park_or_dead_letter(
            conn, claimed,
            "RECIPIENT_FUNDS_UNAVAILABLE",
            "RECIPIENT_FUNDS_NEVER_AVAILABLE",
        )

    await conn.fetchval(_CREDIT, sender, amount)
    await conn.execute(_LEG, f"{tx}-OUT", recipient, amount, currency, "DEBIT")
    await conn.execute(_MARK_REVERSED, target)
    await conn.execute(
        _SETTLE_REVERSAL, tx, sender, amount, currency, "CREDIT", "DEBIT",
    )
    return "COMPLETED"


async def _apply_reversal(conn: asyncpg.Connection, claimed) -> str:
    """Compensate the transaction this reversal targets.

    The direction is derived from the original, never assumed: reversing a
    DEBIT credits the account, reversing a CREDIT debits it. Getting this wrong
    in the other direction created money -- see decision D12.
    """
    tx = claimed["transaction_id"]
    target = claimed["reverses_id"]

    original = await conn.fetchrow(_ORIGINAL_FOR_REVERSAL, target)

    if original is None or original["status"] != "COMPLETED":
        return await _cannot_settle_yet(conn, claimed, original)

    # The accept path authorised the caller against the account they CLAIMED
    # this reversal concerns -- it could not check the original, which section 8
    # allows not to exist yet. This is where the claim meets reality. A caller
    # who asked to reverse somebody else's transaction gets a reversal that
    # terminates without moving a cent.
    if original["account_id"] != claimed["claimed_account_id"]:
        log.warning(
            "reversal %s claimed account %s but %s belongs to %s",
            tx, claimed["claimed_account_id"], target, original["account_id"],
        )
        await conn.execute(_FINALISE, tx, "NOT_APPLICABLE", "ACCOUNT_MISMATCH")
        return "NOT_APPLICABLE"

    # A transfer touched two accounts, so undoing it must touch both. Only the
    # sender can get here: the claim check above compares against the
    # ORIGINAL's account_id, which for a transfer is the sender.
    if original["kind"] == "TRANSFER":
        return await _reverse_transfer(conn, claimed, original)

    direction = _COMPENSATION[original["type"]]
    account_id, amount = original["account_id"], original["amount"]

    if direction == "DEBIT":
        # Reversing a credit claws money back, so it is guarded exactly like an
        # ordinary debit. Invariant I2 -- no negative balance -- outranks the
        # reversal: if the money has since been spent, the reversal waits
        # rather than overdrawing the account.
        if await conn.fetchval(_DEBIT, account_id, amount) is None:
            return await _park_or_dead_letter(
                conn, claimed,
                "REVERSAL_FUNDS_UNAVAILABLE",
                "REVERSAL_FUNDS_NEVER_AVAILABLE",
            )
    else:
        await conn.fetchval(_CREDIT, account_id, amount)

    # Only now, with the compensation actually applied, does the original
    # become REVERSED. Everything below is in the same transaction as the
    # balance change above, so the two cannot disagree.
    await conn.execute(_MARK_REVERSED, target)
    await conn.execute(
        _SETTLE_REVERSAL, tx,
        account_id, amount, original["currency"],
        direction, original["type"],
    )
    return "COMPLETED"


async def _cannot_settle_yet(conn: asyncpg.Connection, claimed, original) -> str:
    """The original has not applied. It may never, or it may not have arrived."""
    tx = claimed["transaction_id"]
    state = original["status"] if original is not None else None

    if state in ("REJECTED", "NOT_APPLICABLE"):
        await conn.execute(_FINALISE, tx, "NOT_APPLICABLE", "ORIGINAL_NEVER_APPLIED")
        return "NOT_APPLICABLE"

    return await _park_or_dead_letter(
        conn, claimed, "ORIGINAL_NOT_YET_APPLIED", "ORIGINAL_NEVER_ARRIVED"
    )


async def _park_or_dead_letter(conn: asyncpg.Connection, claimed,
                               park_reason: str, dead_reason: str) -> str:
    """Retry for a bounded time, then hand the problem to a human.

    `parked_since` is set once and never reset, so the horizon bounds the total
    time a reversal spends waiting, whatever it is waiting for.
    """
    tx = claimed["transaction_id"]
    parked_since = claimed["parked_since"]
    horizon = timedelta(hours=settings.reversal_park_horizon_hours)

    if parked_since and datetime.now(timezone.utc) - parked_since > horizon:
        await conn.execute(_FINALISE, tx, "DEAD_LETTER", dead_reason)
        return "DEAD_LETTER"

    await conn.execute(_PARK, tx, settings.reversal_park_seconds, park_reason)
    return "PARKED"
