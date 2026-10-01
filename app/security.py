"""Password hashing and session tokens.

Two jobs with two different threat models, kept in one file so the difference
between them stays visible:

  A PASSWORD is low-entropy and human-chosen. An attacker holding the hash can
  guess, so each guess must be made expensive -- scrypt, deliberately slow and
  memory-hard.

  A SESSION TOKEN is 256 bits from a CSPRNG. It cannot be guessed at all, so
  there is nothing to slow an attacker down from; a slow hash here would only
  add latency to every authenticated request. SHA-256 is correct, and using
  scrypt would be a mistake dressed up as caution.

Nothing outside this module needs to know the encoded format. The migration
that creates `users` deliberately seeds nobody, so this module owns the format
alone -- no second implementation to keep in agreement.
"""

import base64
import binascii
import hashlib
import hmac
import secrets

# Memory cost is 128 * n * r = 16 MiB here, which fits under OpenSSL's default
# 32 MiB maxmem. Raising n past 2**14 requires passing maxmem explicitly or
# hashlib.scrypt raises.
#
# These are today's parameters, not forever's. They are written INTO each hash,
# so they can be raised later and old passwords keep verifying -- see
# needs_rehash(), which is how a login upgrades a hash in place.
_N, _R, _P = 2 ** 14, 8, 1
_DKLEN = 32
_SALT_BYTES = 16
_SCHEME = "scrypt"

# An upper bound on a parsed n. The encoded hashes we parse come from our own
# database, so this is not strictly load-bearing -- but a parser that trusts
# its input to be sane is a habit worth not having. Without it, a row with
# n = 2**40 would try to allocate terabytes.
_MAX_N = 2 ** 20


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=n, r=r, p=p, dklen=_DKLEN)


def hash_password(password: str) -> str:
    """Return `scrypt$n$r$p$salt$derived`, all base64 where binary.

    A fresh random salt per password is what stops one precomputed table from
    cracking every account at once, and is why hashing the same password twice
    gives two different strings.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _derive(password, salt, _N, _R, _P)
    return f"{_SCHEME}${_N}${_R}${_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of `password` against a stored hash.

    Returns False rather than raising on anything malformed. A verification
    function that can raise invites a caller to treat "corrupt hash" and "wrong
    password" differently, and the difference is visible to an attacker.
    """
    try:
        scheme, n_s, r_s, p_s, salt_b64, derived_b64 = encoded.split("$")
        if scheme != _SCHEME:
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        if not (0 < n <= _MAX_N and 0 < r <= 64 and 0 < p <= 16):
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(derived_b64, validate=True)
        actual = _derive(password, salt, n, r, p)
    except (ValueError, TypeError, binascii.Error):
        return False

    # Not ==. A plain comparison returns as soon as two bytes differ, and the
    # time it took leaks how many leading bytes were correct.
    return hmac.compare_digest(actual, expected)


def needs_rehash(encoded: str) -> bool:
    """True when a stored hash predates the current parameters.

    Call this after a SUCCESSFUL login -- that is the one moment the plaintext
    is in hand -- and re-hash. This is the payoff for storing n, r and p inside
    the hash instead of hard-coding them.
    """
    try:
        scheme, n_s, r_s, p_s, _, _ = encoded.split("$")
        return (scheme, int(n_s), int(r_s), int(p_s)) != (_SCHEME, _N, _R, _P)
    except (ValueError, TypeError):
        return True


def new_session_token() -> str:
    """32 bytes of CSPRNG entropy, URL-safe so it can be a cookie value."""
    return secrets.token_urlsafe(32)


def session_token_hash(token: str) -> str:
    """What `sessions.token_sha256` stores. The token itself is never stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
