"""users, sessions, one account per user, and account numbers

Revision ID: c3e8b5a17f04
Revises: a1c4f7e2b9d3
Create Date: 2026-10-01

Introduces identity: who a caller is, how a logged-in session is recognised,
which user an account belongs to, and the public number a human types to send
money to it.

No users are seeded here. Customers arrive through public sign-up, and the
admin is upserted by the API at boot from the environment -- so this migration
creates only the shapes, never the people.

See claude/phase2-design.md, sections 2 and 3.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c3e8b5a17f04'
down_revision: Union[str, Sequence[str], None] = 'a1c4f7e2b9d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- refuse to run where accounts already exist ----------------------
    #
    # Ownership becomes mandatory below. There is no defensible way to invent
    # an owner for an account that already holds money -- picking one would be
    # assigning somebody else's balance to a stranger. A schema migration
    # cannot answer that question; only the business can, and in a real system
    # this would be a separate agreed data migration.
    #
    # Every account in this project's databases is test data, so the answer
    # here is to reset the volume. The migration says so instead of guessing.
    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n FROM accounts;
            IF n > 0 THEN
                RAISE EXCEPTION
                    'Refusing to migrate: % existing account(s) have no owner '
                    'and no owner can be invented for them. Reset the volume '
                    '(docker compose down -v) or write a data migration that '
                    'assigns ownership deliberately.', n;
            END IF;
        END $$
    """)

    op.execute("CREATE TYPE user_role AS ENUM ('ADMIN', 'CUSTOMER')")

    op.execute("""
        CREATE TABLE users (
            id            BIGSERIAL PRIMARY KEY,
            full_name     TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          user_role NOT NULL DEFAULT 'CUSTOMER',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            disabled_at   TIMESTAMPTZ,

            -- Normalisation is a property of the data, not a habit of the
            -- code. Without this, UNIQUE means "one row per SPELLING", and
            -- Abdullah@x.com and abdullah@x.com can both sign up.
            CONSTRAINT email_is_normalised CHECK (email = lower(email)),

            -- Deliberately not a regex. No correct regular expression for an
            -- email address exists, and every attempt rejects valid ones. The
            -- schema's job is to refuse the absurd; format validation belongs
            -- at the edge, where it can return a useful error message.
            CONSTRAINT email_has_a_shape CHECK (email LIKE '%_@_%'),

            CONSTRAINT full_name_is_not_blank CHECK (btrim(full_name) <> '')
        )
    """)

    # The cookie carries a 256-bit random token; this table stores only its
    # SHA-256. A database dump, a backup on a disk or a logged query must not
    # be replayable as somebody's login -- the same reason password_hash exists.
    #
    # SHA-256 rather than scrypt, deliberately. A password is low-entropy and
    # human-chosen, so an attacker holding the hash can guess, and the defence
    # is to make each guess expensive. A CSPRNG token cannot be guessed at all,
    # so there is nothing to slow down -- a slow hash here would only add
    # latency to every authenticated request. Different threat, different tool.
    op.execute("""
        CREATE TABLE sessions (
            token_sha256 TEXT PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ NOT NULL,
            revoked_at   TIMESTAMPTZ,

            CONSTRAINT session_outlives_its_creation CHECK (expires_at > created_at)
        )
    """)

    # The expiry sweep reads only live sessions, so the index holds only live
    # sessions -- the same partial-index reasoning as idx_tx_due.
    op.execute("""
        CREATE INDEX idx_sessions_expiry
            ON sessions (expires_at)
         WHERE revoked_at IS NULL
    """)

    # --- account ownership ----------------------------------------------
    #
    # NOT NULL and UNIQUE go on in one step, which is only possible because the
    # guard above proved the table is empty. UNIQUE enforces one account per
    # user and, because a UNIQUE constraint is backed by a btree index, it also
    # serves every owner-scoped lookup -- no second index needed.
    #
    # No ON DELETE CASCADE: deleting a user must never delete an account that
    # holds money. The foreign key's job here is to refuse the deletion.
    op.execute("""
        ALTER TABLE accounts
            ADD COLUMN owner_id BIGINT NOT NULL UNIQUE REFERENCES users(id)
    """)

    # --- the public account number ---------------------------------------
    #
    # accounts.id stays the surrogate key: internal, sequential, and what every
    # section-3 endpoint already uses, so those contracts do not change.
    # account_number is the business key -- what a human types and reads aloud.
    #
    # Stored bare; grouping (1234 5678 9012) is presentation, and input is
    # normalised before it reaches here. [0-9] rather than \\d on purpose: \\d in
    # a non-raw Python string is an invalid escape sequence, and POSIX classes
    # are clearer in SQL anyway.
    #
    # The value is random with a Luhn check digit, generated by the application
    # (app/accounts.py) with a retry on unique violation. Random because a
    # sequential number plus the payee lookup would let anyone count from 1 and
    # harvest every customer's name.
    op.execute("ALTER TABLE accounts ADD COLUMN account_number TEXT NOT NULL UNIQUE")
    op.execute("""
        ALTER TABLE accounts ADD CONSTRAINT account_number_is_twelve_digits
            CHECK (account_number ~ '^[0-9]{12}$')
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP CONSTRAINT account_number_is_twelve_digits")
    op.execute("ALTER TABLE accounts DROP COLUMN account_number")
    op.execute("ALTER TABLE accounts DROP COLUMN owner_id")
    op.execute("DROP TABLE IF EXISTS sessions")
    op.execute("DROP TABLE IF EXISTS users")
    op.execute("DROP TYPE IF EXISTS user_role")
