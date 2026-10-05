"""a reversal names the account it claims to affect

Revision ID: d7a2e91c4b06
Revises: c3e8b5a17f04
Create Date: 2026-10-05

Authorisation needs an answer to "whose money is this?" at the moment a request
arrives. For every other write that is easy -- the request names an account. For
a reversal it is not, and the reason is structural rather than an oversight.

Section 8 requires accepting a reversal BEFORE its original exists. Such a row
is thin by design (decision D9): no account_id, no amount, because neither is
knowable yet. So there is nothing to check a caller against, and "add
authentication" is not on its own a sufficient answer.

`claimed_account_id` is the caller's assertion about which account the reversal
concerns. The API authorises the caller against THAT account at accept time; the
worker compares it to the original's real account at settle time and terminates
the reversal as NOT_APPLICABLE on a mismatch. A claim that turns out to be false
therefore moves no money.

Deliberately a separate column rather than reusing `account_id`. `account_id`
means "the account money actually moved on" -- it is what the reconciliation
query sums and what the available-balance join reads. Overloading it would put
parked reversals into both.

See claude/phase2-design.md section 7.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd7a2e91c4b06'
down_revision: Union[str, Sequence[str], None] = 'c3e8b5a17f04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE transactions
            ADD COLUMN claimed_account_id BIGINT REFERENCES accounts(id)
    """)

    # A reversal that already settled affected a known account, so its claim is
    # recoverable. One still waiting for an original that never arrived has no
    # account anywhere -- nothing to recover from.
    op.execute("""
        UPDATE transactions r
           SET claimed_account_id = o.account_id
          FROM transactions o
         WHERE r.kind = 'REVERSAL'
           AND o.transaction_id = r.reverses_id
           AND r.claimed_account_id IS NULL
           AND o.account_id IS NOT NULL
    """)

    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n
              FROM transactions
             WHERE kind = 'REVERSAL' AND claimed_account_id IS NULL;

            IF n > 0 THEN
                RAISE EXCEPTION
                    'Refusing to migrate: % reversal(s) have no recoverable '
                    'account claim. They were accepted before their original '
                    'and the original never arrived, so nothing records which '
                    'account they concern. Resolve or delete them first.', n;
            END IF;
        END $$
    """)

    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT reversal_claims_an_account
            CHECK (kind <> 'REVERSAL' OR claimed_account_id IS NOT NULL)
    """)
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT claim_only_on_reversals
            CHECK (claimed_account_id IS NULL OR kind = 'REVERSAL')
    """)

    # A customer should see a reversal they requested while it is still parked,
    # and a parked reversal has no account_id -- so the account listing reads
    # account_id OR claimed_account_id, and this index serves the second half.
    op.execute("""
        CREATE INDEX idx_tx_claimed_account
            ON transactions (claimed_account_id)
         WHERE kind = 'REVERSAL'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tx_claimed_account")
    op.execute("ALTER TABLE transactions DROP CONSTRAINT claim_only_on_reversals")
    op.execute("ALTER TABLE transactions DROP CONSTRAINT reversal_claims_an_account")
    op.execute("ALTER TABLE transactions DROP COLUMN claimed_account_id")
