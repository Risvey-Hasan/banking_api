from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_serializer


def _money(value: Decimal | None) -> str | None:
    """Render money as a plain decimal string, never scientific notation."""
    return None if value is None else format(value, "f")


class AccountCreate(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    initial_balance: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=20, decimal_places=4
    )


class AccountOut(BaseModel):
    id: int
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
    amount: Decimal | None = None
    currency: str | None = None
    device_id: str | None = None
    accepted_at: datetime
    processed_at: datetime | None = None
    failed_attempts: int

    @field_serializer("amount")
    def _ser(self, v: Decimal | None) -> str | None:
        return _money(v)