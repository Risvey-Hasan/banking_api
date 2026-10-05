from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends, status, Query

from app import account_number, auth, db
from app.errors import AppError
from app.schemas import AccountOut, TransactionOut
from app.sql import TX_COLUMNS

router = APIRouter(prefix="/accounts", tags=["accounts"])


_ACCOUNT_TX = f"""
    SELECT {TX_COLUMNS}
      FROM transactions
     WHERE account_id = $1
        OR claimed_account_id = $1
     ORDER BY accepted_at DESC
     LIMIT $2 OFFSET $3
"""

# $1 NULL means "every account" -- that is the admin. Passing the scope as a
# nullable parameter keeps one query instead of two that can drift apart.
ACCOUNT_LIST = """
    SELECT a.id,
           a.account_number,
           a.currency,
           u.full_name                              AS owner_name,
           a.balance                                AS settled_balance,
           a.balance - COALESCE(SUM(t.amount), 0)   AS available_balance
      FROM accounts a
      JOIN users u ON u.id = a.owner_id
      LEFT JOIN transactions t
             ON t.account_id = a.id
            AND t.type   = 'DEBIT'
            AND t.status = 'PENDING'
     WHERE ($1::bigint IS NULL OR a.owner_id = $1)
     GROUP BY a.id, a.account_number, a.currency, u.full_name, a.balance
     ORDER BY a.id
     LIMIT $2 OFFSET $3
"""

_OPEN_ACCOUNT = """
    INSERT INTO transactions
        (transaction_id, kind, type, status, account_id, amount, currency,
         device_id, client_ts, accepted_at, processed_at, next_attempt_at)
    VALUES ($1, 'TRANSACTION', 'CREDIT', 'COMPLETED', $2, $3, $4,
            'SYSTEM', now(), now(), now(), now())
"""

ACCOUNT_VIEW = """
    SELECT a.id,
           a.account_number,
           a.currency,
           u.full_name                              AS owner_name,
           a.balance                                AS settled_balance,
           a.balance - COALESCE(SUM(t.amount), 0)   AS available_balance
      FROM accounts a
      JOIN users u ON u.id = a.owner_id
      LEFT JOIN transactions t
             ON t.account_id = a.id
            AND t.type   = 'DEBIT'
            AND t.status = 'PENDING'
     WHERE a.id = $1
     GROUP BY a.id, a.account_number, a.currency, u.full_name, a.balance
"""

_INSERT_ACCOUNT = """
    INSERT INTO accounts (currency, balance, owner_id, account_number)
    VALUES ($1, $2, $3, $4)
    RETURNING id
"""

# A 12-digit space is 10**11 wide once the check digit is fixed, so a clash is
# vanishingly unlikely -- but "unlikely" is not "impossible", and the code has
# to be correct rather than lucky. Five attempts is plenty.
_NUMBER_ATTEMPTS = 5


async def open_account_for_user(
    conn: asyncpg.Connection,
    owner_id: int,
    currency: str,
    opening_balance: Decimal,
) -> int:
    """Open the one account a user owns, with its genesis credit.

    The caller is already inside a transaction -- sign-up creates the user and
    the account as one atomic act, because a user with no account is a state
    this system has no meaning for.

    The retry loop needs its own nested `conn.transaction()`. In PostgreSQL a
    failed statement poisons the whole transaction: every later statement gets
    "current transaction is aborted" until a rollback. A nested transaction in
    asyncpg is a SAVEPOINT, so a failed attempt rolls back to just before it
    and the outer transaction survives. Without the savepoint this loop would
    appear to retry and in fact be dead after the first collision.
    """
    account_id = None
    for _ in range(_NUMBER_ATTEMPTS):
        number = account_number.generate()
        try:
            async with conn.transaction():
                account_id = await conn.fetchval(
                    _INSERT_ACCOUNT, currency, opening_balance, owner_id, number
                )
            break
        except asyncpg.UniqueViolationError as exc:
            # Narrowly: only a clash on the NUMBER is retryable. The same
            # exception type also means "this user already owns an account",
            # which retrying would turn into five pointless attempts and then
            # a misleading error. Catching the condition is not the same as
            # catching the exception class.
            if exc.constraint_name != "accounts_account_number_key":
                raise
            continue

    if account_id is None:
        raise AppError(
            "ACCOUNT_NUMBER_UNAVAILABLE",
            "Could not allocate an account number",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if opening_balance > 0:
        await conn.execute(
            _OPEN_ACCOUNT, f"OPEN-{account_id}", account_id,
            opening_balance, currency,
        )

    return account_id


async def authorise_account(conn, caller: auth.Caller, account_id: int) -> None:
    """Raise unless this caller may see this account.

    404, never 403. A 403 would confirm the account exists, which turns every
    read endpoint into an existence oracle -- and account ids are small
    integers, so an attacker would simply count. "Not yours" and "not there"
    must be indistinguishable from outside.
    """
    owner_id = await conn.fetchval(
        "SELECT owner_id FROM accounts WHERE id = $1", account_id
    )
    if owner_id is None or (not caller.is_admin and owner_id != caller.id):
        raise AppError(
            "ACCOUNT_NOT_FOUND",
            f"Account {account_id} does not exist",
            status.HTTP_404_NOT_FOUND,
            {"account_id": account_id},
        )


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    caller: auth.Caller = Depends(auth.current_caller),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[AccountOut]:
    """Every account for an administrator; your own for a customer."""
    scope = None if caller.is_admin else caller.id
    async with db.acquire() as conn:
        rows = await conn.fetch(ACCOUNT_LIST, scope, limit, offset)
    return [AccountOut(**dict(r)) for r in rows]


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: int,
    caller: auth.Caller = Depends(auth.current_caller),
) -> AccountOut:
    async with db.acquire() as conn:
        await authorise_account(conn, caller, account_id)
        row = await conn.fetchrow(ACCOUNT_VIEW, account_id)

    return AccountOut(**dict(row))


@router.get("/{account_id}/transactions", response_model=list[TransactionOut])
async def list_account_transactions(
    account_id: int,
    caller: auth.Caller = Depends(auth.current_caller),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[TransactionOut]:
    async with db.acquire() as conn:
        await authorise_account(conn, caller, account_id)
        rows = await conn.fetch(_ACCOUNT_TX, account_id, limit, offset)

    return [TransactionOut(**dict(r)) for r in rows]