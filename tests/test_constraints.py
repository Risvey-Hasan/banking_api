"""What the schema refuses, regardless of what the application does.

Every test here names the constraint it expects. That is not pedantry: when
`reversal_claims_an_account` was added, three tests in this file kept passing
while testing nothing -- they asserted `CheckViolationError` and got one, from a
constraint they were not about. A test that cannot tell which rule fired cannot
notice when the rule it cares about stops existing.
"""

import asyncpg
import pytest

from tests.conftest import raw_account, raw_user
from tests.helpers import new_id
from app import account_number


def violates(constraint: str):
    """Expect one specific CHECK to fire, not merely that something did."""
    return pytest.raises(asyncpg.CheckViolationError, match=constraint)


async def _account(conn, balance: str = "0.00"):
    """An account created by direct SQL, bypassing the API.

    A funded account made this way holds money the ledger cannot explain and
    would fail the session-end reconciliation forever -- so a caller passing a
    balance must also write the matching entry, as
    test_reversal_may_debit_when_it_reverses_a_credit does.
    """
    return await raw_account(conn, balance)


async def test_balance_cannot_go_negative(conn):
    account_id = await _account(conn)
    with violates("accounts_balance_check"):
        await conn.execute(
            "UPDATE accounts SET balance = -1 WHERE id = $1", account_id
        )


async def test_currency_must_exist(conn):
    """The FK on currency, reached past the NOT NULLs that now guard accounts.

    owner_id and account_number became NOT NULL, so an insert omitting them
    fails before it ever reaches the currency check -- which is why this test
    now supplies both. It was asserting ForeignKeyViolationError and getting
    NotNullViolationError.
    """
    owner_id = await raw_user(conn)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            "INSERT INTO accounts (currency, balance, owner_id, account_number) "
            "VALUES ('ZZZ', 0, $1, $2)",
            owner_id, account_number.generate(),
        )


async def test_account_number_must_be_twelve_digits(conn):
    owner_id = await raw_user(conn)
    with violates("account_number_is_twelve_digits"):
        await conn.execute(
            "INSERT INTO accounts (currency, balance, owner_id, account_number) "
            "VALUES ('SAR', 0, $1, '12345')",
            owner_id,
        )


async def test_one_account_per_user(conn):
    owner_id = await raw_user(conn)
    sql = ("INSERT INTO accounts (currency, balance, owner_id, account_number) "
           "VALUES ('SAR', 0, $1, $2)")
    await conn.execute(sql, owner_id, account_number.generate())
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(sql, owner_id, account_number.generate())


async def test_email_must_be_lowercase(conn):
    with violates("email_is_normalised"):
        await conn.execute(
            "INSERT INTO users (full_name, email, password_hash) "
            "VALUES ('Shouty', 'SHOUTY@example.com', 'x')"
        )


async def test_amount_must_be_positive(conn):
    account_id = await _account(conn)
    with violates("transactions_amount_check"):
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
    with violates("original_is_complete"):
        await conn.execute(
            "INSERT INTO transactions (transaction_id, type) VALUES ($1, 'DEBIT')",
            new_id(),
        )


async def test_reversal_must_have_a_target(conn):
    with violates("reversal_has_target"):
        await conn.execute(
            "INSERT INTO transactions (transaction_id, kind, claimed_account_id) "
            "VALUES ($1, 'REVERSAL', $2)",
            new_id(), await _account(conn),
        )


async def test_reversal_must_claim_an_account(conn):
    """A reversal with no claim cannot be authorised, so it cannot exist."""
    with violates("reversal_claims_an_account"):
        await conn.execute(
            "INSERT INTO transactions (transaction_id, kind, reverses_id) "
            "VALUES ($1, 'REVERSAL', $2)",
            new_id(), new_id(),
        )


async def test_reversal_cannot_repeat_the_original_direction(conn):
    """Decision D12, enforced by the data model rather than by the worker.

    A reversal recording the same direction as the transaction it reverses would
    apply that transaction's effect twice. The schema refuses to store such a
    row, so the defect cannot return through a code path that forgets -- which is
    exactly how it arrived the first time.
    """
    account_id = await _account(conn)
    with violates("reversal_compensates"):
        await conn.execute(
            """
            INSERT INTO transactions
                (transaction_id, kind, reverses_id, claimed_account_id,
                 type, reverses_type, status, account_id, amount, currency)
            VALUES ($1, 'REVERSAL', $2, $3, 'CREDIT', 'CREDIT',
                    'COMPLETED', $3, 10, 'SAR')
            """,
            new_id(), new_id(), account_id,
        )


async def test_reversal_may_debit_when_it_reverses_a_credit(conn):
    """The mirror of the above: the opposing direction is accepted.

    Note the deliberate arithmetic. This row settles as a CREDIT of 10 against
    an account opened with 10.00, so the balance equals the sum of its ledger
    entries and the session-end reconciliation still passes. A constraint test
    that inserts a SETTLED row has to balance its own books.
    """
    account_id = await _account(conn, "10.00")
    await conn.execute(
        """
        INSERT INTO transactions
            (transaction_id, kind, reverses_id, claimed_account_id,
             type, reverses_type, status, account_id, amount, currency)
        VALUES ($1, 'REVERSAL', $2, $3, 'CREDIT', 'DEBIT',
                'COMPLETED', $3, 10, 'SAR')
        """,
        new_id(), new_id(), account_id,
    )


async def test_settled_reversal_must_record_its_direction(conn):
    """A reversal cannot reach COMPLETED without saying which way money moved."""
    account_id = await _account(conn)
    tx = new_id()
    await conn.execute(
        "INSERT INTO transactions "
        "(transaction_id, kind, reverses_id, claimed_account_id) "
        "VALUES ($1, 'REVERSAL', $2, $3)",
        tx, new_id(), account_id,
    )
    with violates("settled_is_complete"):
        await conn.execute(
            """
            UPDATE transactions
               SET status = 'COMPLETED', account_id = $2, amount = 10,
                   currency = 'SAR'
             WHERE transaction_id = $1
            """,
            tx, account_id,
        )


async def test_settled_requires_money_details(conn):
    """A thin reversal cannot reach COMPLETED without learning what it settled."""
    account_id = await _account(conn)
    tx = new_id()
    await conn.execute(
        "INSERT INTO transactions "
        "(transaction_id, kind, reverses_id, claimed_account_id) "
        "VALUES ($1, 'REVERSAL', $2, $3)",
        tx, new_id(), account_id,
    )
    with violates("settled_is_complete"):
        await conn.execute(
            "UPDATE transactions SET status = 'COMPLETED' WHERE transaction_id = $1",
            tx,
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


# ---------------------------------------------------------------- transfers

async def test_transfer_must_name_a_destination(conn):
    account_id = await _account(conn)
    with violates("transfer_has_a_destination"):
        await conn.execute(
            """
            INSERT INTO transactions
                (transaction_id, kind, type, account_id, amount, currency,
                 device_id, client_ts)
            VALUES ($1, 'TRANSFER', 'DEBIT', $2, 10, 'SAR', 'D', now())
            """,
            new_id(), account_id,
        )


async def test_only_a_transfer_may_name_a_destination(conn):
    a, b = await _account(conn), await _account(conn)
    with violates("destination_only_on_transfers"):
        await conn.execute(
            """
            INSERT INTO transactions
                (transaction_id, kind, type, account_id, to_account_id,
                 amount, currency, device_id, client_ts)
            VALUES ($1, 'TRANSACTION', 'DEBIT', $2, $3, 10, 'SAR', 'D', now())
            """,
            new_id(), a, b,
        )


async def test_a_transfer_is_always_a_debit(conn):
    a, b = await _account(conn), await _account(conn)
    with violates("transfer_is_a_debit"):
        await conn.execute(
            """
            INSERT INTO transactions
                (transaction_id, kind, type, account_id, to_account_id,
                 amount, currency, device_id, client_ts)
            VALUES ($1, 'TRANSFER', 'CREDIT', $2, $3, 10, 'SAR', 'D', now())
            """,
            new_id(), a, b,
        )


async def test_a_transfer_cannot_target_its_own_account(conn):
    account_id = await _account(conn)
    with violates("no_self_transfer"):
        await conn.execute(
            """
            INSERT INTO transactions
                (transaction_id, kind, type, account_id, to_account_id,
                 amount, currency, device_id, client_ts)
            VALUES ($1, 'TRANSFER', 'DEBIT', $2, $2, 10, 'SAR', 'D', now())
            """,
            new_id(), account_id,
        )
