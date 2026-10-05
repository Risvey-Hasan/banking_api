"""Sending money to another account, and looking up who owns one."""

import logging

from fastapi import APIRouter, Depends, Response, status

from app import account_number, auth, db, queue
from app.config import settings
from app.errors import AppError
from app.schemas import PayeeOut, TransactionOut, TransferIn
from app.sql import TX_COLUMNS

log = logging.getLogger(__name__)

router = APIRouter(tags=["transfers"])


_MY_ACCOUNT = """
    SELECT id, currency FROM accounts WHERE owner_id = $1
"""

_BY_NUMBER = """
    SELECT a.id, a.currency, u.full_name AS owner_name
      FROM accounts a
      JOIN users u ON u.id = a.owner_id
     WHERE a.account_number = $1
"""

# Identical accept path to POST /transactions: one insert, one commit, and the
# commit is the acceptance. kind='TRANSFER' is what makes the worker move money
# twice instead of once.
_INSERT_TRANSFER = f"""
    INSERT INTO transactions
        (transaction_id, kind, type, status, account_id, to_account_id,
         amount, currency, device_id, client_ts, next_attempt_at)
    VALUES ($1, 'TRANSFER', 'DEBIT', 'PENDING', $2, $3, $4, $5, 'WEB', now(),
            now() + make_interval(secs => $6))
    ON CONFLICT (transaction_id) DO NOTHING
    RETURNING {TX_COLUMNS}
"""

_SELECT_MINE = f"""
    SELECT {TX_COLUMNS}
      FROM transactions
     WHERE transaction_id = $1
       AND account_id IN (SELECT id FROM accounts WHERE owner_id = $2)
"""


def _resolve_number(raw: str) -> str:
    number = account_number.normalise(raw)
    if not account_number.is_valid(number):
        # The check digit means most typos die here, before anything is looked
        # up -- so a mistyped number cannot accidentally match a real stranger.
        raise AppError(
            "INVALID_ACCOUNT_NUMBER",
            "That account number is not valid",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"account_number": raw},
        )
    return number


@router.get("/payees/{number}", response_model=PayeeOut)
async def lookup_payee(
    number: str,
    caller: auth.Caller = Depends(auth.current_caller),
) -> PayeeOut:
    """Confirm an account number and show whose it is, before money moves."""
    resolved = _resolve_number(number)
    async with db.acquire() as conn:
        row = await conn.fetchrow(_BY_NUMBER, resolved)

    if row is None:
        raise AppError(
            "ACCOUNT_NOT_FOUND",
            "No account has that number",
            status.HTTP_404_NOT_FOUND,
            {"account_number": resolved},
        )

    return PayeeOut(account_number=resolved, owner_name=row["owner_name"])


@router.post("/transfers", status_code=status.HTTP_202_ACCEPTED,
             response_model=TransactionOut)
async def create_transfer(
    payload: TransferIn,
    response: Response,
    caller: auth.Caller = Depends(auth.require_customer),
) -> TransactionOut:
    destination = _resolve_number(payload.to_account_number)

    async with db.acquire() as conn:
        sender = await conn.fetchrow(_MY_ACCOUNT, caller.id)
        if sender is None:
            raise AppError(
                "NO_ACCOUNT",
                "This user has no account to send from",
                status.HTTP_409_CONFLICT,
            )

        recipient = await conn.fetchrow(_BY_NUMBER, destination)
        if recipient is None:
            raise AppError(
                "ACCOUNT_NOT_FOUND",
                "No account has that number",
                status.HTTP_404_NOT_FOUND,
                {"account_number": destination},
            )

        if recipient["id"] == sender["id"]:
            raise AppError(
                "SELF_TRANSFER",
                "You cannot transfer to your own account",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if recipient["currency"] != sender["currency"]:
            # No FX in this system, so a cross-currency transfer has no defined
            # meaning rather than a hard implementation.
            raise AppError(
                "CURRENCY_MISMATCH",
                f"That account is held in {recipient['currency']}, "
                f"yours in {sender['currency']}",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        row = await conn.fetchrow(
            _INSERT_TRANSFER,
            payload.transfer_id,
            sender["id"],
            recipient["id"],
            payload.amount,
            sender["currency"],
            settings.accept_grace_seconds,
        )
        created = row is not None
        if not created:
            row = await conn.fetchrow(_SELECT_MINE, payload.transfer_id, caller.id)
            if row is None:
                raise AppError(
                    "TRANSACTION_ID_UNAVAILABLE",
                    "That transfer_id is already in use",
                    status.HTTP_409_CONFLICT,
                    {"transfer_id": payload.transfer_id},
                )

    if created:
        await queue.nudge(payload.transfer_id)
    else:
        response.status_code = status.HTTP_200_OK

    return TransactionOut(**dict(row))
