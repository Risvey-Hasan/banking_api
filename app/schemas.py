from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_serializer


def _money(value: Decimal | None) -> str | None:
    """Render money as a plain decimal string, never scientific notation."""
    return None if value is None else format(value, "f")


# Accounts are no longer created through a request body. One is opened for a
# user by sign-up, so there is nothing for a client to specify -- which is why
# AccountCreate is gone.


class SignupIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    # Deliberately a pattern rather than pydantic's EmailStr, which would pull
    # in the email-validator package. Seven direct dependencies, each
    # defensible, is a property worth keeping. The database refuses the absurd
    # (CHECK email LIKE '%_@_%'); this refuses the obviously malformed and
    # returns a readable 422.
    email: str = Field(min_length=3, max_length=254,
                       pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    # The upper bound is not politeness. scrypt has no length limit of its own,
    # so without a cap a 10 MB password is a request that burns CPU on purpose.
    password: str = Field(min_length=8, max_length=200)


class TransferIn(BaseModel):
    """Send money to another account by its public number.

    No currency and no device: the currency is read from the sender's own
    account (so a mismatch is not a failure mode a caller can create), and the
    sender is the authenticated caller's one account rather than a field they
    could lie about.
    """
    transfer_id: str = Field(min_length=1, max_length=128)
    to_account_number: str = Field(min_length=1, max_length=32)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=4)


class PayeeOut(BaseModel):
    account_number: str
    owner_name: str


class ReverseIn(BaseModel):
    """Which account the caller says this reversal concerns.

    Required because a reversal may legitimately be accepted before its
    original exists, in which case the row has no account of its own and there
    is nothing to authorise the caller against. The worker checks the claim
    against the original's real account at settle time.
    """
    account_id: int = Field(gt=0)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: int
    full_name: str
    email: str
    role: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AccountOut(BaseModel):
    id: int
    account_number: str
    owner_name: str
    currency: str
    settled_balance: Decimal
    available_balance: Decimal

    @field_serializer("settled_balance", "available_balance")
    def _ser(self, v: Decimal) -> str | None:
        return _money(v)


class TransactionCreate(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    account_id: int = Field(gt=0)
    type: Literal["DEBIT", "CREDIT"]
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=4)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    timestamp: datetime


class TransactionOut(BaseModel):
    transaction_id: str
    kind: str
    # Nullable for a reversal that has not settled. The compensating direction
    # is the opposite of the transaction being reversed, and a reversal may be
    # accepted before that transaction exists -- so the direction is unknown
    # until it settles. See decision D12.
    type: str | None = None
    status: str
    reverses_transaction_id: str | None = None
    reverses_type: str | None = None
    account_id: int | None = None
    to_account_id: int | None = None
    # Which account the caller asserted a reversal concerns. Set at accept
    # time, when a premature reversal has no account_id of its own.
    claimed_account_id: int | None = None
    amount: Decimal | None = None
    currency: str | None = None
    device_id: str | None = None
    accepted_at: datetime
    processed_at: datetime | None = None
    failed_attempts: int

    @field_serializer("amount")
    def _ser(self, v: Decimal | None) -> str | None:
        return _money(v)

class MeOut(BaseModel):
    user: UserOut
    # An admin owns no account, so this is genuinely optional rather than
    # merely convenient.
    account: AccountOut | None = None


class SignupOut(BaseModel):
    user: UserOut
    account: AccountOut
