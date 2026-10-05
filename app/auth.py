"""Who is calling, and may they do this.

Two credential transports, each correct for its client:

  A COOKIE for the browser -- httpOnly, so no JavaScript can read it and an XSS
  bug cannot steal a session. The browser attaches it automatically, which is
  convenient and is also exactly what makes CSRF possible, so cookie-carried
  writes must prove they came from our own page.

  A BEARER TOKEN for tests, scripts and the load generator. Sent explicitly on
  every request, never attached by the browser on its own -- and therefore
  needing no CSRF protection at all. That is the whole reason the two paths are
  treated differently: CSRF is a tax on ambient credentials.

Authorisation here is the first line only. Admin's inability to write is
additionally enforced by a read-only PostgreSQL role, so a route that forgets
its dependency still cannot change anything. One line of defence is how D12
happened.
"""

import hmac
import logging
from dataclasses import dataclass

from fastapi import Depends, Request, status

from app import db, security
from app.config import settings
from app.errors import AppError

log = logging.getLogger(__name__)

# Methods that change state. A GET carrying a cookie is not a CSRF risk worth
# a token; a POST is.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class Caller:
    """The authenticated identity behind a request. Frozen so a route cannot
    quietly rewrite who it is acting as."""
    id: int
    full_name: str
    email: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"


_RESOLVE = """
    SELECT u.id, u.full_name, u.email, u.role::text AS role
      FROM sessions s
      JOIN users u ON u.id = s.user_id
     WHERE s.token_sha256 = $1
       AND s.revoked_at IS NULL
       AND s.expires_at > now()
       AND u.disabled_at IS NULL
"""

_CREATE_SESSION = """
    INSERT INTO sessions (token_sha256, user_id, expires_at)
    VALUES ($1, $2, now() + make_interval(hours => $3))
"""

_REVOKE_SESSION = """
    UPDATE sessions SET revoked_at = now()
     WHERE token_sha256 = $1 AND revoked_at IS NULL
"""


async def create_session(conn, user_id: int) -> str:
    """Issue a session and return the token. Only the hash is stored."""
    token = security.new_session_token()
    await conn.execute(
        _CREATE_SESSION,
        security.session_token_hash(token),
        user_id,
        settings.session_lifetime_hours,
    )
    return token


async def revoke_session(conn, token: str) -> None:
    await conn.execute(_REVOKE_SESSION, security.session_token_hash(token))


def _credential(request: Request) -> tuple[str | None, bool]:
    """The caller's token, and whether it arrived as a cookie.

    A bearer header wins over a cookie. An explicit credential should always
    beat an ambient one: a script run in a browser-authenticated context must
    be able to act as the identity it names rather than the one the browser
    happens to be carrying.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip(), False

    cookie = request.cookies.get(settings.session_cookie_name)
    return (cookie, True) if cookie else (None, False)


def _not_authenticated() -> AppError:
    # One message for "no credential", "unknown token", "expired" and
    # "revoked". Telling a caller which of those it was is free
    # reconnaissance.
    return AppError(
        "NOT_AUTHENTICATED",
        "Authentication required",
        status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _check_csrf(request: Request) -> None:
    """Double-submit: the header must match the non-httpOnly CSRF cookie.

    This works because of the same-origin policy. An attacker's page can make
    the browser SEND our cookies with a request, but cannot READ them -- so it
    cannot discover the CSRF value to put in the header. A forged request
    therefore arrives with the session cookie and without a matching token.

    SameSite=Strict on both cookies already blocks most of this; the token is
    the belt to that braces, because SameSite is a browser behaviour and not a
    check we perform.
    """
    sent = request.headers.get("x-csrf-token", "")
    expected = request.cookies.get(settings.csrf_cookie_name, "")
    if not sent or not expected or not hmac.compare_digest(sent, expected):
        raise AppError(
            "CSRF_FAILED",
            "Missing or mismatched CSRF token",
            status.HTTP_403_FORBIDDEN,
        )


async def current_caller(request: Request) -> Caller:
    """Resolve the request's identity, or refuse it."""
    token, via_cookie = _credential(request)
    if token is None:
        raise _not_authenticated()

    if via_cookie and request.method in _UNSAFE_METHODS:
        _check_csrf(request)

    async with db.acquire() as conn:
        row = await conn.fetchrow(_RESOLVE, security.session_token_hash(token))

    if row is None:
        raise _not_authenticated()

    return Caller(**dict(row))


async def require_customer(
    caller: Caller = Depends(current_caller),
) -> Caller:
    """A write path. Admin is refused here, and refused again by the database."""
    if caller.is_admin:
        raise AppError(
            "ADMIN_IS_READ_ONLY",
            "An administrator cannot create or modify financial records",
            status.HTTP_403_FORBIDDEN,
        )
    return caller


async def require_admin(caller: Caller = Depends(current_caller)) -> Caller:
    if not caller.is_admin:
        raise AppError(
            "FORBIDDEN",
            "This resource is restricted to administrators",
            status.HTTP_403_FORBIDDEN,
        )
    return caller
