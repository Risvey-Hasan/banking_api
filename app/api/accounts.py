from fastapi import APIRouter, status, Query

from app import db
from app.errors import AppError
from app.schemas import AccountCreate, AccountOut, TransactionOut
from app.sql import TX_COLUMNS

router = APIRouter(prefix="/accounts", tags=["accounts"])


_ACCOUNT_TX = f"""
    SELECT {TX_COLUMNS}
      FROM transactions
     WHERE account_id = $1
     ORDER BY accepted_at DESC
     LIMIT $2 OFFSET $3
"""

_OPEN_ACCOUNT = """
    INSERT INTO transactions
        (transaction_id, kind, type, status, account_id, amount, currency,
         device_id, client_ts, accepted_at, processed_at, next_attempt_at)
    VALUES ($1, 'TRANSACTION', 'CREDIT', 'COMPLETED', $2, $3, $4,
            'SYSTEM', now(), now(), now(), now())
"""

_ACCOUNT_VIEW = """
    SELECT a.id,
           a.currency,
           a.balance                                AS settled_balance,
           a.balance - COALESCE(SUM(t.amount), 0)   AS available_balance
      FROM accounts a
      LEFT JOIN transactions t
             ON t.account_id = a.id
            AND t.type   = 'DEBIT'
            AND t.status = 'PENDING'
     WHERE a.id = $1
     GROUP BY a.id, a.currency, a.balance
"""


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AccountOut)
async def create_account(payload: AccountCreate) -> AccountOut:
    async with db.acquire() as conn:
        known = await conn.fetchval(
            "SELECT code FROM currencies WHERE code = $1", payload.currency
        )
        if known is None:
            raise AppError(
                "UNKNOWN_CURRENCY",
                f"Currency {payload.currency} is not supported",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"currency": payload.currency},
            )

        async with conn.transaction():
            account_id = await conn.fetchval(
                "INSERT INTO accounts (currency, balance) VALUES ($1, $2) RETURNING id",
                payload.currency,
                payload.initial_balance,
            )
            if payload.initial_balance > 0:
                await conn.execute(
                    _OPEN_ACCOUNT,
                    f"OPEN-{account_id}",
                    account_id,
                    payload.initial_balance,
                    payload.currency,
                )

        row = await conn.fetchrow(_ACCOUNT_VIEW, account_id)

    return AccountOut(**dict(row))


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(account_id: int) -> AccountOut:
    async with db.acquire() as conn:
        row = await conn.fetchrow(_ACCOUNT_VIEW, account_id)

    if row is None:
        raise AppError(
            "ACCOUNT_NOT_FOUND",
            f"Account {account_id} does not exist",
            status.HTTP_404_NOT_FOUND,
            {"account_id": account_id},
        )

    return AccountOut(**dict(row))


@router.get("/{account_id}/transactions", response_model=list[TransactionOut])
async def list_account_transactions(
    account_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[TransactionOut]:
    async with db.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM accounts WHERE id = $1", account_id
        )
        if exists is None:
            raise AppError(
                "ACCOUNT_NOT_FOUND",
                f"Account {account_id} does not exist",
                status.HTTP_404_NOT_FOUND,
                {"account_id": account_id},
            )
        rows = await conn.fetch(_ACCOUNT_TX, account_id, limit, offset)

    return [TransactionOut(**dict(r)) for r in rows]