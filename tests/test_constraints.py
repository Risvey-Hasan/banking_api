from decimal import Decimal

import asyncpg
import pytest

from tests.helpers import new_id


async def _account(conn, balance: str = "0.00"):
    """A zero-balance account.

    These tests bypass the API, so they also bypass the genesis credit that
    gives an opening balance a recorded origin. A funded account created here
    would hold money the ledger cannot explain, and would fail the
    reconciliation check in conftest forever. None of these tests need funds.
    """
    return await conn.fetchval(
        "INSERT INTO accounts (currency, balance) VALUES ('SAR', $1) RETURNING id",
        Decimal(balance),
    )


async def test_balance_cannot_go_negative(conn):
    account_id = await _account(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "UPDATE accounts SET balance = -1 WHERE id = $1", account_id
        )


async def test_currency_must_exist(conn):
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            "INSERT INTO accounts (currency, balance) VALUES ('ZZZ', 0)"
        )


async def test_amount_must_be_positive(conn):
    account_id = await _account(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO transactions
                (transaction_id, type, account_id, amount, currency,
                 device_id, client_ts)
            VALUES ($1, 'DEBIT', $2, 0, 'SAR', 'D', now())
            """,
            new_id(), account_id,
        )


async def test_original_must_be_complete(conn):
    """An ordinary transaction cannot be accepted without account or amount."""
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO transactions (transaction_id, type) VALUES ($1, 'DEBIT')",
            new_id(),
        )


async def test_reversal_must_have_a_target(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO transactions (transaction_id, kind, type)
            VALUES ($1, 'REVERSAL', 'CREDIT')
            """,
            new_id(),
        )


async def test_reversal_cannot_debit(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO transactions (transaction_id, kind, reverses_id, type)
            VALUES ($1, 'REVERSAL', $2, 'DEBIT')
            """,
            new_id(), new_id(),
        )


async def test_transaction_id_is_unique(conn):
    account_id = await _account(conn)
    tx = new_id()
    sql = """
        INSERT INTO transactions
            (transaction_id, type, account_id, amount, currency,
             device_id, client_ts)
        VALUES ($1, 'DEBIT', $2, 10, 'SAR', 'D', now())
    """
    await conn.execute(sql, tx, account_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(sql, tx, account_id)


async def test_settled_requires_money_details(conn):
    """A thin reversal cannot reach COMPLETED without learning what it settled."""
    tx = new_id()
    await conn.execute(
        """
        INSERT INTO transactions (transaction_id, kind, reverses_id, type)
        VALUES ($1, 'REVERSAL', $2, 'CREDIT')
        """,
        tx, new_id(),
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "UPDATE transactions SET status = 'COMPLETED' WHERE transaction_id = $1",
            tx,
        )