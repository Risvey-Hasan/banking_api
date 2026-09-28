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
            "INSERT INTO transactions (transaction_id, kind) VALUES ($1, 'REVERSAL')",
            new_id(),
        )


async def test_reversal_cannot_repeat_the_original_direction(conn):
    """Decision D12, enforced by the data model rather than by the worker.

    A reversal that records the same direction as the transaction it reverses
    would apply that transaction's effect twice. The schema refuses to store
    such a row, so the defect cannot return through a code path that forgets --
    which is exactly how it arrived the first time.
    """
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO transactions
                (transaction_id, kind, reverses_id, type, reverses_type,
                 status, account_id, amount, currency)
            VALUES ($1, 'REVERSAL', $2, 'CREDIT', 'CREDIT',
                    'COMPLETED', $3, 10, 'SAR')
            """,
            new_id(), new_id(), await _account(conn),
        )


async def test_reversal_may_debit_when_it_reverses_a_credit(conn):
    """The mirror of the above: the opposing direction is accepted.

    Note the deliberate arithmetic. This row settles as a CREDIT of 10 against
    an account opened with 10.00, so the account's balance equals the sum of
    its ledger entries and the session-end reconciliation still passes. A
    constraint test that inserts a settled row has to balance its own books.
    """
    await conn.execute(
        """
        INSERT INTO transactions
            (transaction_id, kind, reverses_id, type, reverses_type,
             status, account_id, amount, currency)
        VALUES ($1, 'REVERSAL', $2, 'CREDIT', 'DEBIT',
                'COMPLETED', $3, 10, 'SAR')
        """,
        new_id(), new_id(), await _account(conn, "10.00"),
    )


async def test_settled_reversal_must_record_its_direction(conn):
    """A reversal cannot reach COMPLETED without saying which way money moved."""
    tx = new_id()
    await conn.execute(
        "INSERT INTO transactions (transaction_id, kind, reverses_id) "
        "VALUES ($1, 'REVERSAL', $2)",
        tx, new_id(),
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            UPDATE transactions
               SET status = 'COMPLETED', account_id = $2, amount = 10,
                   currency = 'SAR'
             WHERE transaction_id = $1
            """,
            tx, await _account(conn),
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
        "INSERT INTO transactions (transaction_id, kind, reverses_id) "
        "VALUES ($1, 'REVERSAL', $2)",
        tx, new_id(),
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "UPDATE transactions SET status = 'COMPLETED' WHERE transaction_id = $1",
            tx,
        )