"""Migration 024 — daily yield curves (US Treasury)

Revision ID: 024
Revises: 023
Create Date: 2026-10-17

``macro_rates_cache`` keeps the latest value of one rate (the 10-year rate of
FRED, of the ECB). A yield curve is every maturity, day by day: the US Treasury
publishes its par curve since 1990 (1 month to 30 years) and its real curve
(TIPS) since 2003 (5 to 30 years), one file per year.

- ``yield_curve_points``: one rate per source, curve, day and maturity, as a
  ratio (``0.0509`` for 5.09 %; real yields may be negative). ``tenor`` is the
  code of the maturity (``1M``, ``6W``, ``10Y``) and ``tenor_months`` its
  length, to sort a curve. A maturity not published on a day has no row. A file
  read again replaces the rates of its days. The index on the tenor serves the
  history of one maturity.
- ``yield_curve_loads``: the last reading of each year of a curve, so that a
  past year read after its end is not asked again.

The downgrade drops both tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "024"
down_revision = "023"


def upgrade() -> None:
    op.create_table(
        "yield_curve_points",
        sa.Column("source", sa.String(10), primary_key=True),
        sa.Column("curve", sa.String(10), primary_key=True),
        sa.Column("rate_date", sa.Date(), primary_key=True),
        sa.Column("tenor", sa.String(4), primary_key=True),
        sa.Column("tenor_months", sa.Numeric(6, 2), nullable=False),
        sa.Column("value", sa.Numeric(10, 6), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_yield_curve_points_tenor",
        "yield_curve_points",
        ["source", "curve", "tenor", "rate_date"],
    )
    op.create_table(
        "yield_curve_loads",
        sa.Column("source", sa.String(10), primary_key=True),
        sa.Column("curve", sa.String(10), primary_key=True),
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_day", sa.Date()),
        sa.Column("last_day", sa.Date()),
    )


def downgrade() -> None:
    op.drop_table("yield_curve_loads")
    op.drop_index("ix_yield_curve_points_tenor", table_name="yield_curve_points")
    op.drop_table("yield_curve_points")
