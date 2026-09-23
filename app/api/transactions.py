from fastapi import APIRouter, Response, status

from app import db, queue
from app.errors import AppError
from app.schemas import TransactionCreate, TransactionOut

router = APIRouter(prefix="/transactions", tags=["transactions"])

_COLUMNS = """
    transaction_id,
    kind::text   AS kind,
    type::text   AS type,
    status::text AS status,
    reverses_id  AS reverses_transaction_id,
    account_id, amount, currency, device_id,
    accepted_at, processed_at, failed_attempts
"""

_INSERT = f"""
    INSERT INTO transactions
        (transaction_id, kind, type, status,
         account_id, amount, currency, device_id, client_ts)
    VALUES ($1, 'TRANSACTION', $2::tx_type, 'PENDING', $3, $4, $5, $6, $7)
    ON CONFLICT (transaction_id) DO NOTHING
    RETURNING {_COLUMNS}
"""

_INSERT_REVERSAL = f"""
    INSERT INTO transactions
        (transaction_id, kind, reverses_id, type, status)
    VALUES ($1, 'REVERSAL', $2, 'CREDIT', 'PENDING')
    ON CONFLICT (transaction_id) DO NOTHING
    RETURNING {_COLUMNS}
"""

_SELECT = f"SELECT {_COLUMNS} FROM transactions WHERE transaction_id = $1"


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=TransactionOut)
async def create_transaction(
    payload: TransactionCreate, response: Response
) -> TransactionOut:
    async with db.pool().acquire() as conn:
        account = await conn.fetchrow(
            "SELECT id, currency FROM accounts WHERE id = $1", payload.account_id
        )
        if account is None:
            raise AppError(
                "ACCOUNT_NOT_FOUND",
                f"Account {payload.account_id} does not exist",
                status.HTTP_404_NOT_FOUND,
                {"account_id": payload.account_id},
            )
        if account["currency"] != payload.currency:
            raise AppError(
                "CURRENCY_MISMATCH",
                f"Transaction currency {payload.currency} does not match "
                f"account currency {account['currency']}",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "account_id": payload.account_id,
                    "expected": account["currency"],
                    "received": payload.currency,
                },
            )

        row = await conn.fetchrow(
            _INSERT,
            payload.transaction_id,
            payload.type,
            payload.account_id,
            payload.amount,
            payload.currency,
            payload.device_id,
            payload.timestamp,
        )
        created = row is not None
        if not created:
            row = await conn.fetchrow(_SELECT, payload.transaction_id)

    if created:
        await queue.nudge(payload.transaction_id)
    else:
        response.status_code = status.HTTP_200_OK

    return TransactionOut(**dict(row))


@router.post(
    "/{transaction_id}/reverse",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TransactionOut,
)
async def reverse_transaction(transaction_id: str, response: Response) -> TransactionOut:
    if transaction_id.startswith("REV-"):
        raise AppError(
            "NOT_REVERSIBLE",
            "A reversal cannot itself be reversed",
            status.HTTP_400_BAD_REQUEST,
            {"transaction_id": transaction_id},
        )

    reversal_id = f"REV-{transaction_id}"

    async with db.pool().acquire() as conn:
        original_kind = await conn.fetchval(
            "SELECT kind::text FROM transactions WHERE transaction_id = $1",
            transaction_id,
        )
        if original_kind == "REVERSAL":
            raise AppError(
                "NOT_REVERSIBLE",
                "A reversal cannot itself be reversed",
                status.HTTP_400_BAD_REQUEST,
                {"transaction_id": transaction_id},
            )

        row = await conn.fetchrow(_INSERT_REVERSAL, reversal_id, transaction_id)
        created = row is not None
        if not created:
            row = await conn.fetchrow(_SELECT, reversal_id)

    if created:
        await queue.nudge(reversal_id)
    else:
        response.status_code = status.HTTP_200_OK

    return TransactionOut(**dict(row))


@router.get("/{transaction_id}", response_model=TransactionOut)
async def get_transaction(transaction_id: str) -> TransactionOut:
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(_SELECT, transaction_id)

    if row is None:
        raise AppError(
            "TRANSACTION_NOT_FOUND",
            f"Transaction {transaction_id} does not exist",
            status.HTTP_404_NOT_FOUND,
            {"transaction_id": transaction_id},
        )

    return TransactionOut(**dict(row))