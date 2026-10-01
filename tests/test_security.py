"""Unit tests for the two pure modules: password hashing and account numbers.

These are the first tests in the project that exercise no database, no HTTP and
no containers -- they are plain function calls. Worth noticing as a design
signal: the logic that is easiest to get subtly wrong is also the logic with no
I/O in it, so it can be tested exhaustively and in milliseconds.
"""

from app import account_number as acct
from app.security import (
    hash_password,
    needs_rehash,
    new_session_token,
    session_token_hash,
    verify_password,
)


# --------------------------------------------------------------- passwords

def test_hash_then_verify_round_trip():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)


def test_wrong_password_fails():
    encoded = hash_password("the real one")
    assert not verify_password("the real one ", encoded)      # trailing space
    assert not verify_password("The real one", encoded)       # case
    assert not verify_password("", encoded)


def test_the_same_password_hashes_differently_every_time():
    """A fresh salt per password. Without this, one precomputed table cracks
    every account that shares a password, and identical hashes in the table
    would reveal which users chose the same one."""
    a = hash_password("same input")
    b = hash_password("same input")
    assert a != b
    assert verify_password("same input", a)
    assert verify_password("same input", b)


def test_encoded_format_carries_its_parameters():
    scheme, n, r, p, salt, derived = hash_password("x").split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (2 ** 14, 8, 1)
    assert salt and derived


def test_malformed_hashes_return_false_rather_than_raising():
    """A verifier that raises invites callers to distinguish 'corrupt hash'
    from 'wrong password', and that difference is observable by an attacker."""
    for junk in ("", "not-a-hash", "scrypt$1$2$3", "scrypt$a$b$c$d$e",
                 "bcrypt$16384$8$1$AAAA$BBBB", "scrypt$16384$8$1$!!!$!!!",
                 "scrypt$999999999999$8$1$AAAA$BBBB"):
        assert verify_password("anything", junk) is False


def test_needs_rehash_detects_stale_parameters():
    assert not needs_rehash(hash_password("x"))
    assert needs_rehash("scrypt$1024$8$1$AAAA$BBBB")   # weaker n
    assert needs_rehash("bcrypt$12$AAAA")              # another scheme
    assert needs_rehash("garbage")


# ---------------------------------------------------------- session tokens

def test_session_tokens_are_unique_and_hashed_deterministically():
    tokens = {new_session_token() for _ in range(1000)}
    assert len(tokens) == 1000

    token = new_session_token()
    assert session_token_hash(token) == session_token_hash(token)
    assert len(session_token_hash(token)) == 64          # sha256 hex
    assert token not in session_token_hash(token)        # not recoverable


# --------------------------------------------------------- account numbers

def test_luhn_matches_the_published_vector():
    """7992739871 -> check digit 3 is the textbook Luhn example.

    This is the important test in this file. Checking generate() against
    is_valid() only proves the two agree with each other -- an off-by-one in the
    doubling would produce a checksum that is perfectly self-consistent and
    wrong everywhere else. An external vector is what makes it correct rather
    than merely coherent.
    """
    assert acct.luhn_check_digit("7992739871") == "3"


def test_generated_numbers_are_well_formed():
    numbers = [acct.generate() for _ in range(1000)]
    assert all(len(n) == acct.LENGTH for n in numbers)
    assert all(acct.is_valid(n) for n in numbers)
    assert len(set(numbers)) == 1000                    # no obvious repetition
    # Leading zeros occur ~10% of the time and must survive, which is why the
    # column is TEXT and not an integer type.
    assert any(n.startswith("0") for n in numbers)


def test_a_single_mistyped_digit_is_caught():
    number = acct.generate()
    for i in range(acct.LENGTH):
        for replacement in "0123456789":
            if replacement == number[i]:
                continue
            typo = number[:i] + replacement + number[i + 1:]
            assert not acct.is_valid(typo), typo


def test_adjacent_transpositions_are_caught_except_the_known_gap():
    """Luhn catches every transposition of adjacent digits except 0 <-> 9.
    Stating the gap in a test is how it stays a known limitation rather than a
    surprise. ISO 7064 mod-97 would close it, at the cost of familiarity."""
    number = acct.generate()
    missed = 0
    for i in range(acct.LENGTH - 1):
        a, b = number[i], number[i + 1]
        if a == b:
            continue
        swapped = number[:i] + b + a + number[i + 2:]
        if acct.is_valid(swapped):
            assert {a, b} == {"0", "9"}, (number, swapped, a, b)
            missed += 1
    assert missed <= 1


def test_validation_agrees_with_the_database_constraint():
    """The column's CHECK is '^[0-9]{12}$'. str.isdigit() would accept
    Arabic-Indic digits, which would then fail the INSERT -- validation that
    disagrees with the schema just relocates the error somewhere harder to
    read."""
    arabic_indic = "١٢٣٤٥٦٧٨٩٠١٢"
    assert arabic_indic.isdigit()               # the trap
    assert not acct.is_valid(arabic_indic)      # the guard

    assert not acct.is_valid("12345678901")     # too short
    assert not acct.is_valid("1234567890123")   # too long
    assert not acct.is_valid("12345678901x")
    assert not acct.is_valid("")


def test_normalise_strips_what_people_type():
    number = acct.generate()
    assert acct.normalise(acct.grouped(number)) == number
    assert acct.normalise("1234-5678-9012") == "123456789012"
    assert acct.normalise(" 1234 5678 9012 ") == "123456789012"
    assert acct.grouped("123456789012") == "1234 5678 9012"
