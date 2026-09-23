import logging

import asyncpg

log = logging.getLogger(__name__)

_CLAIM = """
    UPDATE transactions
       SET status = 'PROCESSING'
     WHERE transaction_id = $1
       AND status = 'PENDING'
    RETURNING transaction_id,
              kind::text AS kind,
              type::text AS type,
              account_id, amount
"""

_DEBIT = """
    UPDATE accounts
       SET balance = balance - $2, updated_at = now()
     WHERE id = $1
       AND balance >= $2
    RETURNING balance
"""

_CREDIT = """
    UPDATE accounts
       SET balance = balance + $2, updated_at = now()
     WHERE id = $1
    RETURNING balance
"""

_FINALISE = """
    UPDATE transactions
       SET status = $2::tx_status,
           processed_at = now(),
           last_error = $3
     WHERE transaction_id = $1
"""


async def apply_transaction(conn: asyncpg.Connection, transaction_id: str) -> str | None:
    """Apply one transaction. Returns its terminal status, or None if there
    was nothing to do (already claimed, already finished, or unknown)."""
    async with conn.transaction():
        claimed = await conn.fetchrow(_CLAIM, transaction_id)
        if claimed is None:
            return None

        if claimed["type"] == "DEBIT":
            balance = await conn.fetchval(
                _DEBIT, claimed["account_id"], claimed["amount"]
            )
            if balance is None:
                await conn.execute(
                    _FINALISE, transaction_id, "REJECTED", "INSUFFICIENT_FUNDS"
                )
                return "REJECTED"
        else:
            await conn.fetchval(_CREDIT, claimed["account_id"], claimed["amount"])

        await conn.execute(_FINALISE, transaction_id, "COMPLETED", None)
        return "COMPLETED"