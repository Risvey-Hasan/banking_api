"""Sign-up, login, logout and self-identification.

Sign-up creates a user AND their account in one database transaction, because a
customer with no account is a state this system has no meaning for. The account
opens with a genesis credit, so the money it starts with has a recorded origin
(decision D10) and the ledger invariant holds from the first second.
"""

import asyncio
import logging
import secrets

from fastapi import APIRouter, Depends, Request, Response, status

from app import auth, db, security
from app.api import accounts
from app.config import settings
from app.errors import AppError
from app.schemas import (
    AccountOut,
    LoginIn,
    MeOut,
    SignupIn,
    SignupOut,
    TokenOut,
    UserOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


_INSERT_USER = """
    INSERT INTO users (full_name, email, password_hash)
    VALUES ($1, $2, $3)
    ON CONFLICT (email) DO NOTHING
    RETURNING id, full_name, email, role::text AS role
"""

_BY_EMAIL = """
    SELECT id, full_name, email, role::text AS role, password_hash, disabled_at
      FROM users
     WHERE email = $1
"""

_OWN_ACCOUNT_ID = "SELECT id FROM accounts WHERE owner_id = $1"

# Hashed once at import, against a password nobody has. An unknown email then
# costs the same ~50 ms as a known one. Without this, a fast 401 means "no such
# user" and a slow 401 means "wrong password" -- an email-enumeration oracle
# you can read off a stopwatch.
_ABSENT_USER_HASH = security.hash_password(secrets.token_urlsafe(32))


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    common = {
        "secure": settings.cookie_secure,
        "samesite": "strict",
        "max_age": settings.session_lifetime_hours * 3600,
        "path": "/",
    }
    # httpOnly: JavaScript cannot read it, so an XSS bug cannot exfiltrate the
    # session.
    response.set_cookie(settings.session_cookie_name, token, httponly=True, **common)
    # NOT httpOnly, on purpose -- the page has to read this one to echo it back
    # in the X-CSRF-Token header. It is not a credential; it is a value an
    # attacker's origin cannot read.
    response.set_cookie(settings.csrf_cookie_name, csrf, httponly=False, **common)


async def _authenticate(conn, email: str, password: str):
    row = await conn.fetchrow(_BY_EMAIL, email)
    stored = row["password_hash"] if row is not None else _ABSENT_USER_HASH

    # to_thread, not a direct call. scrypt here is ~50 ms of CPU holding 16 MiB,
    # and this process also serves the accept path -- the hot path this whole
    # system was tuned around. Blocking the event loop would stall every other
    # request in the worker for the duration. hashlib releases the GIL, so a
    # thread genuinely runs in parallel rather than merely looking tidy.
    ok = await asyncio.to_thread(security.verify_password, password, stored)

    if row is None or not ok or row["disabled_at"] is not None:
        raise AppError(
            "INVALID_CREDENTIALS",
            "Email or password is incorrect",
            status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return row


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=SignupOut)
async def signup(payload: SignupIn) -> SignupOut:
    email = payload.email.strip().lower()
    password_hash = await asyncio.to_thread(security.hash_password, payload.password)

    async with db.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                _INSERT_USER, payload.full_name.strip(), email, password_hash
            )
            # ON CONFLICT DO NOTHING returns no row when the email is taken --
            # the same race-free idempotent insert as the accept path. Note the
            # different answer though: a duplicate TRANSACTION returns 200 with
            # the existing row, because a retrying client should not see red
            # for work already done. A duplicate SIGN-UP returns 409, because
            # the person needs to know the address is not theirs to claim.
            if user is None:
                raise AppError(
                    "EMAIL_TAKEN",
                    "That email address is already registered",
                    status.HTTP_409_CONFLICT,
                    {"email": email},
                )

            account_id = await accounts.open_account_for_user(
                conn,
                user["id"],
                settings.signup_currency,
                settings.signup_opening_balance,
            )

        account = await conn.fetchrow(accounts.ACCOUNT_VIEW, account_id)

    log.info("signup user=%s account=%s", user["id"], account_id)
    return SignupOut(user=UserOut(**dict(user)), account=AccountOut(**dict(account)))


@router.post("/login", response_model=UserOut)
async def login(payload: LoginIn, response: Response) -> UserOut:
    """Log in for a browser: sets the session and CSRF cookies, returns no token."""
    email = payload.email.strip().lower()

    async with db.acquire() as conn:
        user = await _authenticate(conn, email, payload.password)
        token = await auth.create_session(conn, user["id"])

    _set_session_cookies(response, token, secrets.token_urlsafe(32))
    return UserOut(id=user["id"], full_name=user["full_name"],
                   email=user["email"], role=user["role"])


@router.post("/token", response_model=TokenOut)
async def issue_token(payload: LoginIn) -> TokenOut:
    """Log in for tooling: returns a bearer token and sets no cookie.

    Deliberately a separate endpoint rather than a flag on /auth/login. If one
    response both set an httpOnly cookie and handed the token to JavaScript,
    the httpOnly would be decorative -- the page could read the token from the
    response body and store it where an XSS could find it.
    """
    email = payload.email.strip().lower()

    async with db.acquire() as conn:
        user = await _authenticate(conn, email, payload.password)
        token = await auth.create_session(conn, user["id"])

    return TokenOut(
        access_token=token,
        expires_in=settings.session_lifetime_hours * 3600,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response,
                 caller: auth.Caller = Depends(auth.current_caller)) -> Response:
    """Revoke this session server-side, then clear the cookies.

    Revoking in the database is what makes logout mean something. Deleting the
    cookie alone would leave a token that still works for anyone who copied it
    -- which is the objection that ruled out a stateless signed cookie.
    """
    token, _ = auth._credential(request)
    if token:
        async with db.acquire() as conn:
            await auth.revoke_session(conn, token)

    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut)
async def me(caller: auth.Caller = Depends(auth.current_caller)) -> MeOut:
    async with db.acquire() as conn:
        account_id = await conn.fetchval(_OWN_ACCOUNT_ID, caller.id)
        account = (
            await conn.fetchrow(accounts.ACCOUNT_VIEW, account_id)
            if account_id is not None else None
        )

    return MeOut(
        user=UserOut(id=caller.id, full_name=caller.full_name,
                     email=caller.email, role=caller.role),
        account=AccountOut(**dict(account)) if account is not None else None,
    )
