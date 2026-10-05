from fastapi import APIRouter, Depends, Response, status

from app import auth, db, queue
from app.api.accounts import authorise_account
from app.config import settings
from app.errors import AppError
from app.sql import TX_COLUMNS
from app.schemas import ReverseIn, TransactionCreate, TransactionOut

router = APIRouter(prefix="/transactions", tags=["transactions"])

_INSERT = f"""
    INSERT INTO transactions
        (transaction_id, kind, type, status, account_id, amount,
         currency, device_id, client_ts, next_attempt_at)
    VALUES ($1, 'TRANSACTION', $2::tx_type, 'PENDING', $3, $4, $5, $6, $7,
            now() + make_interval(secs => $8))
    ON CONFLICT (transaction_id) DO NOTHING
    RETURNING {TX_COLUMNS}
"""

# No `type`. The compensating direction is the OPPOSITE of the transaction
# being reversed, and section 8 requires accepting a reversal before its
# original exists -- so at this point the direction is genuinely unknown. The
# worker writes it, with the direction it opposed, when the reversal settles.
_INSERT_REVERSAL = f"""
    INSERT INTO transactions
        (transaction_id, kind, reverses_id, claimed_account_id, status,
         next_attempt_at)
    VALUES ($1, 'REVERSAL', $2, $3, 'PENDING',
            now() + make_interval(secs => $4))
    ON CONFLICT (transaction_id) DO NOTHING
    RETURNING {TX_COLUMNS}
"""

# $2 is a USER id and NULL means "any account" -- the admin. Scoping inside the
# query rather than fetching and then checking means "not yours" and "does not
# exist" produce the same 404 from the same code path, so they cannot drift
# apart later.
#
# The subqueries are not decoration. transactions.account_id is an ACCOUNT id
# and $2 is a USER id: comparing them directly is a type error that Python and
# PostgreSQL both accept, because both values are integers. It also happens to
# be CORRECT for the first user in an empty database, where user 1 owns account
# 1 -- so it passes every single-user test and fails in production.
#
# claimed_account_id is in the predicate because a parked reversal has no
# account_id yet, and the customer who asked for it must still be able to read
# it back.
_SELECT_SCOPED = f"""
    SELECT {TX_COLUMNS}
      FROM transactions
     WHERE transaction_id = $1
       AND ($2::bigint IS NULL
            OR account_id         IN (SELECT id FROM accounts WHERE owner_id = $2)
            OR claimed_account_id IN (SELECT id FROM accounts WHERE owner_id = $2))
"""


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=TransactionOut)
async def create_transaction(
    payload: TransactionCreate,
    response: Response,
    caller: auth.Caller = Depends(auth.require_customer),
) -> TransactionOut:
    async with db.acquire() as conn:
        # Ownership first, and it answers existence too: an account belonging to
        # someone else is reported exactly as one that does not exist.
        await authorise_account(conn, caller, payload.account_id)

        account = await conn.fetchrow(
            "SELECT id, currency FROM accounts WHERE id = $1", payload.account_id
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
            settings.accept_grace_seconds,
        )
        created = row is not None
        if not created:
            # Scoped. Before authorisation existed this re-read was harmless;
            # now it is a disclosure. transaction_id is a GLOBAL primary key, so
            # a caller could submit a transaction on their own account using an
            # id another customer already used, hit ON CONFLICT, and be handed
            # that customer's row -- account, amount, currency and all.
            row = await conn.fetchrow(
                _SELECT_SCOPED, payload.transaction_id, caller.id
            )
            if row is None:
                # The id exists but is not theirs. 409 rather than 404: a
                # legitimate client retrying must be able to tell "already done"
                # from "cannot have this id", and the message reveals nothing
                # about whose it is.
                raise AppError(
                    "TRANSACTION_ID_UNAVAILABLE",
                    "That transaction_id is already in use",
                    status.HTTP_409_CONFLICT,
                    {"transaction_id": payload.transaction_id},
                )

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
async def reverse_transaction(
    transaction_id: str,
    payload: ReverseIn,
    response: Response,
    caller: auth.Caller = Depends(auth.require_customer),
) -> TransactionOut:
    if transaction_id.startswith("REV-"):
        raise AppError(
            "NOT_REVERSIBLE",
            "A reversal cannot itself be reversed",
            status.HTTP_400_BAD_REQUEST,
            {"transaction_id": transaction_id},
        )

    # Derived from the CLAIMED ACCOUNT as well as the transaction.
    #
    # REV-{transaction_id} alone was a land-grab. The id is a global primary
    # key, so whoever asked first owned the only reversal row that could ever
    # exist for that transaction -- letting anyone who can guess an id
    # permanently prevent its owner from reversing it. The worker stopped the
    # theft (ACCOUNT_MISMATCH) but could not give the row back.
    #
    # Including the claimed account gives each claimant their own row, and
    # idempotency is unchanged: the same caller claiming the same account for
    # the same transaction always derives the same id, so a retry is still a
    # 200 with the existing row.
    reversal_id = f"REV-{payload.account_id}-{transaction_id}"

    async with db.acquire() as conn:
        # The caller's claim about whose money this is. It cannot be checked
        # against the original here -- section 8 allows the original not to
        # exist yet -- so the API authorises the CLAIM and the worker verifies
        # it against reality at settle time, terminating the reversal as
        # NOT_APPLICABLE if the two disagree. A false claim moves no money.
        await authorise_account(conn, caller, payload.account_id)

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

        row = await conn.fetchrow(
            _INSERT_REVERSAL,
            reversal_id,
            transaction_id,
            payload.account_id,
            settings.accept_grace_seconds,
        )
        created = row is not None
        if not created:
            row = await conn.fetchrow(_SELECT_SCOPED, reversal_id, caller.id)
            if row is None:
                raise AppError(
                    "TRANSACTION_ID_UNAVAILABLE",
                    "That reversal already exists and is not yours",
                    status.HTTP_409_CONFLICT,
                    {"transaction_id": reversal_id},
                )

    if created:
        await queue.nudge(reversal_id)
    else:
        response.status_code = status.HTTP_200_OK

    return TransactionOut(**dict(row))


@router.get("/{transaction_id}", response_model=TransactionOut)
async def get_transaction(
    transaction_id: str,
    caller: auth.Caller = Depends(auth.current_caller),
) -> TransactionOut:
    scope = None if caller.is_admin else caller.id
    async with db.acquire() as conn:
        row = await conn.fetchrow(_SELECT_SCOPED, transaction_id, scope)

    if row is None:
        raise AppError(
            "TRANSACTION_NOT_FOUND",
            f"Transaction {transaction_id} does not exist",
            status.HTTP_404_NOT_FOUND,
            {"transaction_id": transaction_id},
        )

    return TransactionOut(**dict(row))