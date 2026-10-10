"""Migration 008 — Drop the legacy EOD tables

Revision ID: 008
Revises: 007
Create Date: 2026-05-23

Description:
    Drops the legacy tables of the historical EOD path:
    - stock_data, cache_status, data_requests
    - daily_stock_records, weekly_stock_records,
      monthly_stock_records, yearly_stock_records

    They are replaced by prices_eod (TimescaleDB hypertable), fed by
    HistoricalIngestionService.

    ⚠️  DESTRUCTIVE MIGRATION — the data cannot be recovered afterwards.
    Back up PostgreSQL before applying it:
    pg_dump -Fc fonrex > backup_pre_008.dump

    The downgrade recreates the tables EMPTY (without data).
    Restoring the data needs the backup.
"""

import sqlalchemy as sa

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Drop every legacy table in the right order
    (following the foreign keys, if any).

    IF EXISTS makes the migration idempotent
    (safe to replay when partly applied).
    """

    # Historical record tables (no foreign key to other legacy tables)
    op.execute("DROP TABLE IF EXISTS daily_stock_records CASCADE")
    op.execute("DROP TABLE IF EXISTS weekly_stock_records CASCADE")
    op.execute("DROP TABLE IF EXISTS monthly_stock_records CASCADE")
    op.execute("DROP TABLE IF EXISTS yearly_stock_records CASCADE")

    # Legacy EOD tables (data_requests may have a foreign key to stock_data)
    op.execute("DROP TABLE IF EXISTS data_requests CASCADE")
    op.execute("DROP TABLE IF EXISTS cache_status CASCADE")
    op.execute("DROP TABLE IF EXISTS stock_data CASCADE")

    # Drop the orphan sequences too, if any
    op.execute("DROP SEQUENCE IF EXISTS stock_data_id_seq CASCADE")
    op.execute("DROP SEQUENCE IF EXISTS data_requests_id_seq CASCADE")


def downgrade() -> None:
    """
    Recreate the legacy tables EMPTY, for a schema rollback.
    ⚠️  The data is NOT restored — use the PostgreSQL backup.

    Recreated in reverse order: parents before children.
    """

    # stock_data (table principale legacy)
    op.create_table(
        "stock_data",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(14, 4)),
        sa.Column("high", sa.Numeric(14, 4)),
        sa.Column("low", sa.Numeric(14, 4)),
        sa.Column("close", sa.Numeric(14, 4)),
        sa.Column("adj_close", sa.Numeric(14, 4)),
        sa.Column("volume", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker", "date", name="uq_stock_data_ticker_date"),
    )
    op.create_index("ix_stock_data_ticker", "stock_data", ["ticker"])
    op.create_index("ix_stock_data_date", "stock_data", ["date"])

    # cache_status
    op.create_table(
        "cache_status",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(20), nullable=False, unique=True),
        sa.Column("last_updated", sa.DateTime(timezone=True)),
        sa.Column("is_fresh", sa.Boolean(), default=False),
    )

    # data_requests
    op.create_table(
        "data_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(20)),
        sa.Column("period", sa.String(10)),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("status", sa.String(20)),
        sa.Column("source", sa.String(20)),
    )

    # Tables de records (structure minimale)
    for table_name in [
        "daily_stock_records",
        "weekly_stock_records",
        "monthly_stock_records",
        "yearly_stock_records",
    ]:
        op.create_table(
            table_name,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(20), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("open", sa.Numeric(14, 4)),
            sa.Column("high", sa.Numeric(14, 4)),
            sa.Column("low", sa.Numeric(14, 4)),
            sa.Column("close", sa.Numeric(14, 4)),
            sa.Column("volume", sa.BigInteger()),
        )
