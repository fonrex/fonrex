"""Migration 003 — Table index_constituents

Revision ID: 003
Revises: 002
Create Date: 2026-05-14

Creates the table storing the constituents of stock indices:
  S&P 500, CAC 40, NASDAQ 100, DAX, EURO STOXX 50.

Unique constraint: (index_name, ticker) — no duplicate within an index.
Extra index      : (index_name, sector) for the filters by sector.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "index_constituents",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("index_name", sa.String(32), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("isin", sa.String(12), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sector", sa.String(128), nullable=True),
        sa.Column("sub_sector", sa.String(128), nullable=True),
        sa.Column("weight", sa.Numeric(8, 6), nullable=True),  # ex: 0.048500
        sa.Column("country", sa.String(4), nullable=True),  # ISO 2 letters
        sa.Column("cik", sa.String(12), nullable=True),  # US only
        sa.Column("fetched_at", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
        sa.Column("source", sa.String(64), nullable=False, server_default="Wikipedia"),
        # Constraints
        sa.UniqueConstraint("index_name", "ticker", name="uq_index_ticker"),
    )

    # Index for the queries filtered by index + sector
    op.create_index(
        "ix_index_constituents_index_sector",
        "index_constituents",
        ["index_name", "sector"],
    )

    # Index for the queries by ISIN
    op.create_index(
        "ix_index_constituents_isin",
        "index_constituents",
        ["isin"],
    )


def downgrade() -> None:
    op.drop_index("ix_index_constituents_isin", table_name="index_constituents")
    op.drop_index("ix_index_constituents_index_sector", table_name="index_constituents")
    op.drop_table("index_constituents")
