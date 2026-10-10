"""Migration 021 — euro reference exchange rates of the ECB

Revision ID: 021
Revises: 020
Create Date: 2026-10-10

``fx_rates`` keeps, day by day since 1999, the ECB reference rate of the euro
against other currencies (``per_eur``: units of the currency for one euro), so
that a listing quoted in euros or pounds can be compared with the factor returns
of the Kenneth French library, which are in US dollars. ``fx_rate_loads`` records
the last refresh of each currency.
"""

import sqlalchemy as sa

from alembic import op

revision = "021"
down_revision = "020"


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("rate_date", sa.Date(), primary_key=True),
        sa.Column("per_eur", sa.Numeric(18, 8), nullable=False),
    )
    op.create_table(
        "fx_rate_loads",
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_day", sa.Date()),
        sa.Column("last_day", sa.Date()),
    )


def downgrade() -> None:
    op.drop_table("fx_rate_loads")
    op.drop_table("fx_rates")
