from fastapi import APIRouter, status

from app import db
from app.errors import AppError
from app.schemas import AccountCreate, AccountOut

router = APIRouter(prefix="/accounts", tags=["accounts"])

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

        account_id = await conn.fetchval(
            "INSERT INTO accounts (currency, balance) VALUES ($1, $2) RETURNING id",
            payload.currency,
            payload.initial_balance,
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