"""Migration 022 — macro-economic series by period (Eurostat)

Revision ID: 022
Revises: 021
Create Date: 2026-10-15

``macro_rates_cache`` keeps the latest value of a daily rate (FRED, ECB). The
indicators of Eurostat are of another kind: monthly or quarterly series, one per
country, whose recent periods are revised after their first release. They get
their own table:

- ``macro_observations``: one value per source, indicator, geographic area and
  period, dated by the first day of the period (``2026-Q2`` is ``2026-04-01``).
  A revision replaces the value of its period. ``status`` keeps the flag of the
  source (``e`` for an estimate, such as the flash inflation of the last month).
- ``macro_series_loads``: the last refresh of each series, so that a series
  read a moment ago is not asked again.

The downgrade drops both tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "022"
down_revision = "021"


def upgrade() -> None:
    op.create_table(
        "macro_observations",
        sa.Column("source", sa.String(10), primary_key=True),
        sa.Column("indicator", sa.String(30), primary_key=True),
        sa.Column("geo", sa.String(10), primary_key=True),
        sa.Column("period", sa.Date(), primary_key=True),
        sa.Column("frequency", sa.String(10), nullable=False),
        sa.Column("value", sa.Numeric(18, 8), nullable=False),
        sa.Column("unit", sa.String(10), nullable=False),
        sa.Column("status", sa.String(8)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "macro_series_loads",
        sa.Column("source", sa.String(10), primary_key=True),
        sa.Column("indicator", sa.String(30), primary_key=True),
        sa.Column("geo", sa.String(10), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_period", sa.Date()),
        sa.Column("last_period", sa.Date()),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("macro_series_loads")
    op.drop_table("macro_observations")
