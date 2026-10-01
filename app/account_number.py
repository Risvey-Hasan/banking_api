"""The public account number: twelve digits, the last one a Luhn check digit.

`accounts.id` is the surrogate key -- internal, sequential, and what every
section-3 endpoint already uses. This module owns the BUSINESS key: the number a
human types to send money, and reads aloud over the phone. Real banks keep both
for the same reason: the internal key is free to be boring because nobody sees
it, and the public one is free to be designed for people because nothing joins
on it.

Two properties it must have, each for a concrete reason:

  RANDOM, not sequential. The payee lookup turns an account number into a
  person's name. A sequential number would let anyone count from 1 and harvest
  every customer in the bank; randomness over 10**12 makes that a search rather
  than a loop.

  A CHECK DIGIT. A mistyped number then fails in the browser, before any money
  moves. Without one, the only defence against a typo is the payee name -- and a
  typo that happens to land on a real account shows a plausible stranger's name
  to someone who may not read it carefully.
"""

import secrets

LENGTH = 12
_DIGITS = "0123456789"
_DIGIT_SET = frozenset(_DIGITS)
_SEPARATORS = frozenset(" - \t–—")


def luhn_check_digit(body: str) -> str:
    """The digit which makes `body + digit` satisfy the Luhn checksum.

    Positions are counted from the right of the FINAL number, so the rightmost
    digit of `body` sits one place left of the check digit and is therefore one
    of the doubled positions -- which is why the test below is `i % 2 == 0`
    rather than `i % 2 == 1`. Off-by-one here produces a checksum that is
    perfectly self-consistent and disagrees with every other implementation on
    earth, which is why this is tested against a published vector and not only
    against our own generator.
    """
    total = 0
    for i, char in enumerate(reversed(body)):
        digit = int(char)
        if i % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - total % 10) % 10)


def generate() -> str:
    """A fresh random account number.

    secrets.choice, not random.choice: the latter is a Mersenne Twister seeded
    from the clock, and observing a few outputs predicts the rest.

    A leading zero is perfectly fine and will happen about a tenth of the time.
    That is precisely why the column is TEXT and not BIGINT -- an integer column
    would silently eat it and turn a 12-digit number into an 11-digit one.
    """
    body = "".join(secrets.choice(_DIGITS) for _ in range(LENGTH - 1))
    return body + luhn_check_digit(body)


def normalise(raw: str) -> str:
    """Strip the separators humans type. Validation is a separate question."""
    return "".join(c for c in raw if c not in _SEPARATORS)


def is_valid(number: str) -> bool:
    """Length, strictly ASCII digits, and a correct check digit.

    Note what is NOT used here: str.isdigit(). It returns True for Unicode
    digits -- '١٢٣' is three Arabic-Indic digits and isdigit() accepts
    them -- but the column's CHECK is '^[0-9]{12}$', so such a value would pass
    validation and then fail the INSERT. Validation that disagrees with the
    schema is worse than no validation, because it moves the error somewhere
    harder to read.
    """
    return (
        len(number) == LENGTH
        and all(c in _DIGIT_SET for c in number)
        and luhn_check_digit(number[:-1]) == number[-1]
    )


def grouped(number: str) -> str:
    """`1234 5678 9012` -- presentation only; never what gets stored."""
    return " ".join(number[i:i + 4] for i in range(0, len(number), 4))
