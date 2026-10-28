"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-28

Every index below has a comment saying which query needs it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # pg_trgm makes "description ILIKE '%coffee%'" (the q filter) use a GIN index.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("token_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        # unique: login looks users up by email; also gives us 409 on duplicate register.
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("jti", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("revoked_reason", sa.String(16)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        # unique: /refresh and /logout find the token by jti.
        sa.UniqueConstraint("jti", name="uq_refresh_tokens_jti"),
    )
    # "revoke all tokens of this user" after reuse is detected.
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    op.create_table(
        "categories",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("monthly_budget", MONEY),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("monthly_budget IS NULL OR monthly_budget > 0", name="ck_categories_budget_positive"),
    )
    # Case-insensitive unique name per user ("Food" vs "food" -> 409). It also serves
    # "categories of this user" lookups and the CSV import's name -> id map (leading user_id).
    op.create_index("uq_categories_user_lower_name", "categories", ["user_id", sa.text("lower(name)")], unique=True)

    op.create_table(
        "expenses",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category_id", sa.BigInteger, sa.ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("spent_on", sa.Date, nullable=False),
        sa.Column("receipt_key", sa.Text),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("import_key", sa.String(80)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="ck_expenses_amount_positive"),
    )
    # GET /expenses: WHERE user_id = ? ORDER BY spent_on DESC, id DESC LIMIT n, and the
    # keyset condition (spent_on, id) < (?, ?). Same column order as the ORDER BY, so
    # Postgres walks the index and stops after n rows - no sort, no scan of old pages.
    op.create_index("ix_expenses_user_spent_id", "expenses", ["user_id", sa.text("spent_on DESC"), sa.text("id DESC")])
    # Reports / category list / budget worker: one user's expenses of one category in a
    # date range. Also covers the FK on category_id for the RESTRICT check.
    op.create_index("ix_expenses_user_category_spent", "expenses", ["user_id", "category_id", "spent_on"])
    # CSV import dedupe: a row of a given file can only be inserted once (partial: manual
    # expenses have no import_key and cost nothing in this index).
    op.create_index(
        "uq_expenses_user_import_key",
        "expenses",
        ["user_id", "import_key"],
        unique=True,
        postgresql_where=sa.text("import_key IS NOT NULL"),
    )
    # q filter: ILIKE '%text%' cannot use a b-tree; a trigram GIN index can.
    op.create_index(
        "ix_expenses_description_trgm",
        "expenses",
        ["description"],
        postgresql_using="gin",
        postgresql_ops={"description": "gin_trgm_ops"},
    )

    op.create_table(
        "exchange_rates",
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("valid_on", sa.Date, nullable=False),
        sa.Column("rate_to_usd", sa.Numeric(20, 10), nullable=False),
        # PK (currency, valid_on): one row per currency per day, AND the index for
        # "latest rate on or before date X" (backward index scan, LIMIT 1).
        sa.PrimaryKeyConstraint("currency", "valid_on", name="pk_exchange_rates"),
        sa.CheckConstraint("rate_to_usd > 0", name="ck_exchange_rates_positive"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer),
        sa.Column("response_body", postgresql.JSONB),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TS, nullable=False),
        # The PK is what makes parallel requests with the same key safe.
        sa.PrimaryKeyConstraint("user_id", "key", name="pk_idempotency_keys"),
    )
    # worker purge of expired keys
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", TS),
    )
    # Worker: WHERE processed_at IS NULL ORDER BY id LIMIT n. Partial index = only the
    # small set of pending events, stays tiny even with millions of processed rows.
    op.create_index("ix_outbox_unprocessed", "outbox_events", ["id"], postgresql_where=sa.text("processed_at IS NULL"))

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category_id", sa.BigInteger, sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period", sa.Date, nullable=False),
        sa.Column("level", sa.SmallInteger, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("spent", MONEY, nullable=False),
        sa.Column("budget", MONEY, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        # "only once per category per month per level" - enforced by the database.
        sa.UniqueConstraint("user_id", "category_id", "period", "level", name="uq_notifications_once"),
    )
    # GET /notifications: newest first for one user
    op.create_index("ix_notifications_user_id_desc", "notifications", ["user_id", sa.text("id DESC")])

    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("rows_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rows_done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rows_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TS),
        sa.Column("heartbeat_at", TS),
        sa.Column("finished_at", TS),
        # same file uploaded twice by the same user -> same job, no second import
        sa.UniqueConstraint("user_id", "file_hash", name="uq_import_jobs_user_file"),
    )
    # worker: oldest pending job
    op.create_index(
        "ix_import_jobs_pending", "import_jobs", ["created_at"], postgresql_where=sa.text("status = 'pending'")
    )

    op.create_table(
        "s3_deletions",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("next_attempt_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    # worker: rows due for a retry
    op.create_index("ix_s3_deletions_next_attempt_at", "s3_deletions", ["next_attempt_at"])


def downgrade() -> None:
    for table in (
        "s3_deletions",
        "import_jobs",
        "notifications",
        "outbox_events",
        "idempotency_keys",
        "exchange_rates",
        "expenses",
        "categories",
        "refresh_tokens",
        "users",
    ):
        op.drop_table(table)
