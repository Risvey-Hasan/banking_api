"""initial schema

Revision ID: bbdaadff8278
Revises: 
Create Date: 2026-09-23 02:00:55.430801

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bbdaadff8278'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- reference data -------------------------------------------------
    op.execute("""
        CREATE TABLE currencies (
            code        CHAR(3) PRIMARY KEY CHECK (code ~ '^[A-Z]{3}$'),
            minor_units SMALLINT NOT NULL CHECK (minor_units BETWEEN 0 AND 4)
        )
    """)

    op.execute("""
        INSERT INTO currencies (code, minor_units) VALUES
            ('SAR', 2), ('USD', 2), ('EUR', 2), ('GBP', 2),
            ('KWD', 3), ('BHD', 3), ('OMR', 3), ('JPY', 0)
    """)

    # --- accounts -------------------------------------------------------
    op.execute("""
        CREATE TABLE accounts (
            id         BIGSERIAL PRIMARY KEY,
            currency   CHAR(3) NOT NULL REFERENCES currencies(code),
            balance    NUMERIC(20,4) NOT NULL DEFAULT 0 CHECK (balance >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # --- enumerated types -----------------------------------------------
    op.execute("CREATE TYPE tx_kind AS ENUM ('TRANSACTION', 'REVERSAL')")
    op.execute("CREATE TYPE tx_type AS ENUM ('DEBIT', 'CREDIT')")
    op.execute("""
        CREATE TYPE tx_status AS ENUM (
            'PENDING', 'PROCESSING', 'COMPLETED',
            'REJECTED', 'REVERSED', 'NOT_APPLICABLE', 'DEAD_LETTER'
        )
    """)

    # --- transactions ---------------------------------------------------
    op.execute("""
        CREATE TABLE transactions (
            transaction_id  TEXT PRIMARY KEY,
            kind            tx_kind   NOT NULL DEFAULT 'TRANSACTION',
            reverses_id     TEXT,
            type            tx_type   NOT NULL,
            status          tx_status NOT NULL DEFAULT 'PENDING',

            account_id      BIGINT REFERENCES accounts(id),
            amount          NUMERIC(20,4) CHECK (amount > 0),
            currency        CHAR(3) REFERENCES currencies(code),
            device_id       TEXT,
            client_ts       TIMESTAMPTZ,

            failed_attempts INT NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            parked_since    TIMESTAMPTZ,
            last_error      TEXT,
            accepted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at    TIMESTAMPTZ,

            CONSTRAINT original_is_complete CHECK (
                kind = 'REVERSAL'
                OR (account_id IS NOT NULL AND amount    IS NOT NULL
                AND currency   IS NOT NULL AND device_id IS NOT NULL
                AND client_ts  IS NOT NULL)),

            CONSTRAINT settled_is_complete CHECK (
                status NOT IN ('COMPLETED', 'REVERSED')
                OR (account_id IS NOT NULL AND amount IS NOT NULL
                AND currency   IS NOT NULL)),

            CONSTRAINT reversal_has_target CHECK (
                (reverses_id IS NOT NULL) = (kind = 'REVERSAL')),

            CONSTRAINT reversal_only_credits CHECK (
                kind = 'TRANSACTION' OR type = 'CREDIT')
        )
    """)

    # --- indexes --------------------------------------------------------
    op.execute("""
        CREATE INDEX idx_tx_pending_debits_by_account
            ON transactions (account_id) INCLUDE (amount)
         WHERE status = 'PENDING' AND type = 'DEBIT'
    """)
    op.execute("""
        CREATE INDEX idx_tx_due
            ON transactions (next_attempt_at)
         WHERE status = 'PENDING'
    """)
    op.execute("""
        CREATE INDEX idx_tx_by_account_time
            ON transactions (account_id, accepted_at DESC)
    """)
    op.execute("""
        CREATE INDEX idx_tx_reversals_by_target
            ON transactions (reverses_id)
         WHERE kind = 'REVERSAL' AND status = 'PENDING'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS transactions")
    op.execute("DROP TYPE IF EXISTS tx_status")
    op.execute("DROP TYPE IF EXISTS tx_type")
    op.execute("DROP TYPE IF EXISTS tx_kind")
    op.execute("DROP TABLE IF EXISTS accounts")
    op.execute("DROP TABLE IF EXISTS currencies")
