"""reversal compensation must oppose the original's direction

Revision ID: a1c4f7e2b9d3
Revises: bbdaadff8278
Create Date: 2026-09-28

A reversal compensates; it does not repeat. Reversing a DEBIT credits the
account, reversing a CREDIT debits it.

The initial schema enforced the wrong rule. `reversal_only_credits` required
every reversal to be a CREDIT, so reversing a CREDIT credited the account a
SECOND time and created money out of nothing. The constraint did not merely
fail to catch the bug -- it mandated it, and the worker obeyed.

The reconciliation invariant could not see it either: the bogus credit was
recorded as a ledger entry consistent with the bogus balance, so
`txnsvc_balance_mismatches` stayed at 0. That check asks whether the balance
and the ledger AGREE, not whether the ledger is RIGHT.

This migration moves the real rule into the data model. Because a reversal may
be accepted before its original exists (section 8), the compensating direction
is not knowable at insert time -- so `type` becomes nullable for a reversal and
is written when the reversal settles, alongside `reverses_type`, the direction
it compensated. Storing both is what lets a row-local CHECK assert the
invariant: they must differ. A CHECK cannot read another row, so the reversal
row has to carry what it opposed. That follows the pattern already established
by `_SETTLE_REVERSAL`, which copies account, amount and currency onto the
reversal at settle time.

See docs/DESIGN.md, decision D12.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a1c4f7e2b9d3'
down_revision: Union[str, Sequence[str], None] = 'bbdaadff8278'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- refuse to migrate over money the bug created ------------------
    #
    # A settled reversal of a CREDIT credited instead of debiting. Its account
    # holds funds that no real transaction produced. Blessing those rows with a
    # constraint that now claims they are well-formed would launder the error,
    # so this migration stops and says what to look at. Failing loudly on
    # corrupt financial data is the whole point of having the constraint.
    op.execute("""
        DO $$
        DECLARE bad bigint;
        BEGIN
            SELECT count(*) INTO bad
              FROM transactions r
              JOIN transactions o ON o.transaction_id = r.reverses_id
             WHERE r.kind = 'REVERSAL'
               AND r.status = 'COMPLETED'
               AND o.type = 'CREDIT';

            IF bad > 0 THEN
                RAISE EXCEPTION
                    'Refusing to migrate: % settled reversal(s) of CREDIT '
                    'transactions exist. Each credited the account instead of '
                    'debiting it and created money. Identify them with the '
                    'query in this migration, correct the affected balances, '
                    'then re-run. See docs/DESIGN.md decision D12.', bad;
            END IF;
        END $$
    """)

    # --- the wrong rule goes --------------------------------------------
    op.execute("ALTER TABLE transactions DROP CONSTRAINT reversal_only_credits")

    # --- the reversal records what it opposed ---------------------------
    op.execute("ALTER TABLE transactions ADD COLUMN reverses_type tx_type")
    op.execute("ALTER TABLE transactions ALTER COLUMN type DROP NOT NULL")

    # A reversal that already settled legitimately (it reversed a DEBIT) learns
    # the direction it opposed. One that never moved money -- PENDING,
    # NOT_APPLICABLE, DEAD_LETTER -- has no direction yet, and says so.
    op.execute("""
        UPDATE transactions r
           SET reverses_type = o.type
          FROM transactions o
         WHERE r.kind = 'REVERSAL'
           AND o.transaction_id = r.reverses_id
           AND r.status IN ('COMPLETED', 'REVERSED')
    """)
    op.execute("""
        UPDATE transactions
           SET type = NULL
         WHERE kind = 'REVERSAL'
           AND status NOT IN ('COMPLETED', 'REVERSED')
    """)

    # --- `type` is now nullable, so the completeness rules must say so ---
    op.execute("ALTER TABLE transactions DROP CONSTRAINT original_is_complete")
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT original_is_complete CHECK (
            kind = 'REVERSAL'
            OR (type       IS NOT NULL AND account_id IS NOT NULL
            AND amount     IS NOT NULL AND currency   IS NOT NULL
            AND device_id  IS NOT NULL AND client_ts  IS NOT NULL))
    """)

    op.execute("ALTER TABLE transactions DROP CONSTRAINT settled_is_complete")
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT settled_is_complete CHECK (
            status NOT IN ('COMPLETED', 'REVERSED')
            OR (type IS NOT NULL AND account_id IS NOT NULL
            AND amount IS NOT NULL AND currency IS NOT NULL))
    """)

    # --- the new rules ---------------------------------------------------
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT reverses_type_only_on_reversals
            CHECK (kind = 'REVERSAL' OR reverses_type IS NULL)
    """)

    # The invariant this whole migration exists for. A reversal either has not
    # settled (neither direction known) or it settled against a known direction
    # and moved money the OTHER way. Same-direction is unrepresentable.
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT reversal_compensates CHECK (
            kind <> 'REVERSAL'
            OR (type IS NULL     AND reverses_type IS NULL)
            OR (type IS NOT NULL AND reverses_type IS NOT NULL
            AND type <> reverses_type))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE transactions DROP CONSTRAINT reversal_compensates")
    op.execute("ALTER TABLE transactions DROP CONSTRAINT reverses_type_only_on_reversals")

    op.execute("ALTER TABLE transactions DROP CONSTRAINT settled_is_complete")
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT settled_is_complete CHECK (
            status NOT IN ('COMPLETED', 'REVERSED')
            OR (account_id IS NOT NULL AND amount IS NOT NULL
            AND currency   IS NOT NULL))
    """)

    op.execute("ALTER TABLE transactions DROP CONSTRAINT original_is_complete")
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT original_is_complete CHECK (
            kind = 'REVERSAL'
            OR (account_id IS NOT NULL AND amount    IS NOT NULL
            AND currency   IS NOT NULL AND device_id IS NOT NULL
            AND client_ts  IS NOT NULL))
    """)

    # Restoring NOT NULL means inventing a direction for reversals that have
    # none. 'CREDIT' is what the old schema forced, bug included.
    op.execute("UPDATE transactions SET type = 'CREDIT' WHERE type IS NULL")
    op.execute("ALTER TABLE transactions ALTER COLUMN type SET NOT NULL")
    op.execute("ALTER TABLE transactions DROP COLUMN reverses_type")
    op.execute("""
        ALTER TABLE transactions ADD CONSTRAINT reversal_only_credits
            CHECK (kind = 'TRANSACTION' OR type = 'CREDIT')
    """)
