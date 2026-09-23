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
              account_id, amount, reverses_id, parked_since
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

# The reversal's claim on its original: answers "has it landed?" and
# "have I already reversed it?" in one atomic statement.
_MARK_REVERSED = """
    UPDATE transactions
       SET status = 'REVERSED'
     WHERE transaction_id = $1
       AND status = 'COMPLETED'
    RETURNING account_id, amount, currency
"""

_SETTLE_REVERSAL = """
    UPDATE transactions
       SET status = 'COMPLETED', processed_at = now(),
           account_id = $2, amount = $3, currency = $4
     WHERE transaction_id = $1
"""

_PARK = """
    UPDATE transactions
       SET status = 'PENDING',
           parked_since = COALESCE(parked_since, now()),
           next_attempt_at = now() + make_interval(secs => $2)
     WHERE transaction_id = $1
"""

_STATUS_OF = "SELECT status::text FROM transactions WHERE transaction_id = $1"


async def apply_transaction(conn: asyncpg.Connection, transaction_id: str) -> str | None:
    async with conn.transaction():
        claimed = await conn.fetchrow(_CLAIM, transaction_id)
        if claimed is None:
            return None
        if claimed["kind"] == "REVERSAL":
            return await _apply_reversal(conn, claimed)
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


async def _apply_reversal(conn: asyncpg.Connection, claimed) -> str:
    tx = claimed["transaction_id"]
    target = claimed["reverses_id"]

    original = await conn.fetchrow(_MARK_REVERSED, target)

    if original is None:
        state = await conn.fetchval(_STATUS_OF, target)

        if state in ("REJECTED", "NOT_APPLICABLE"):
            await conn.execute(_FINALISE, tx, "NOT_APPLICABLE", "ORIGINAL_NEVER_APPLIED")
            return "NOT_APPLICABLE"

        parked_since = claimed["parked_since"]
        horizon = timedelta(hours=settings.reversal_park_horizon_hours)
        if parked_since and datetime.now(timezone.utc) - parked_since > horizon:
            await conn.execute(_FINALISE, tx, "DEAD_LETTER", "ORIGINAL_NEVER_ARRIVED")
            return "DEAD_LETTER"

        await conn.execute(_PARK, tx, settings.reversal_park_seconds)
        return "PARKED"

    await conn.fetchval(_CREDIT, original["account_id"], original["amount"])
    await conn.execute(
        _SETTLE_REVERSAL, tx,
        original["account_id"], original["amount"], original["currency"],
    )
    return "COMPLETED"