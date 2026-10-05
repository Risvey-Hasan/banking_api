"""transfers between accounts

Revision ID: e5f1c8d3a927
Revises: d7a2e91c4b06
Create Date: 2026-10-05

The sender's debit leg IS the transfer row: a third `tx_kind` plus a
`to_account_id`. The recipient's credit row is written by the worker at settle
time, in the same database transaction as both balance moves.

Doing it this way means the existing claim, queue, retry and sweeper machinery
work unchanged, `available_balance` needs no edit (the transfer row is a PENDING
DEBIT on the sender, which the existing partial index already reserves), and the
reconciliation query needs no edit either -- the sender's -amount is this row and
the recipient's +amount is the derived row.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'e5f1c8d3a927'
down_revision: Union[str, Sequence[str], None] = 'd7a2e91c4b06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL allows ADD VALUE inside a transaction but forbids USING the new
    # value until that transaction commits -- and the CHECK constraints below
    # reference 'TRANSFER'. autocommit_block steps outside Alembic's transaction
    # so the value is committed before anything uses it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE tx_kind ADD VALUE IF NOT EXISTS 'TRANSFER'")

    op.execute("""
        ALTER TABLE transactions
            ADD COLUMN to_account_id BIGINT REFERENCES accounts(id)
    """)

    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT transfer_has_a_destination
            CHECK (kind <> 'TRANSFER' OR to_account_id IS NOT NULL)
    """)
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT destination_only_on_transfers
            CHECK (to_account_id IS NULL OR kind = 'TRANSFER')
    """)
    # The transfer row is the money LEAVING. Its direction is not a choice.
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT transfer_is_a_debit
            CHECK (kind <> 'TRANSFER' OR type = 'DEBIT')
    """)
    # A transfer to yourself would need the same row locked twice and means
    # nothing. Refused in the schema so no code path can invent it.
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT no_self_transfer
            CHECK (to_account_id IS NULL OR to_account_id <> account_id)
    """)


def downgrade() -> None:
    for name in ("no_self_transfer", "transfer_is_a_debit",
                 "destination_only_on_transfers", "transfer_has_a_destination"):
        op.execute(f"ALTER TABLE transactions DROP CONSTRAINT {name}")
    op.execute("ALTER TABLE transactions DROP COLUMN to_account_id")
    # An enum value cannot be removed in PostgreSQL. Rows using it are gone with
    # the column, so the orphaned label is harmless.
