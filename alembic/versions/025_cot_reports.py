"""Migration 025 — Commitments of Traders (CFTC)

Revision ID: 025
Revises: 024
Create Date: 2026-10-18

Every week the CFTC publishes the positions held on the Tuesday in the US futures
markets, by type of trader: the Legacy report since 1986, Traders in Financial
Futures (TFF) and Disaggregated since June 2006. One zip per report and year,
the years up to 2016 in one zip per report.

- ``cot_weeks``: one line of a report, a market (its CFTC contract market code,
  which stays when the name changes) on a report date: the name of the market
  that week, its contract units and its open interest.
- ``cot_positions``: the positions of one type of trader on that line, in
  contracts: long, short and, for the types that have them, spreading. A
  non-reportable position may be negative in the Legacy history. The rows of a
  line go with it (``ON DELETE CASCADE``): a file read again replaces its years.
- ``cot_loads``: the last reading of each year of a report, and the file that
  held it, so that a past year read after its end is not asked again.

The index ``ix_cot_weeks_report_date`` serves the latest report date of a report
and the markets of one date. The downgrade drops the three tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "025"
down_revision = "024"


def upgrade() -> None:
    op.create_table(
        "cot_weeks",
        sa.Column("report", sa.String(16), primary_key=True),
        sa.Column("market_code", sa.String(6), primary_key=True),
        sa.Column("report_date", sa.Date(), primary_key=True),
        sa.Column("market_name", sa.String(200), nullable=False),
        sa.Column("contract_units", sa.String(200)),
        sa.Column("open_interest", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cot_weeks_report_date", "cot_weeks", ["report", "report_date"])
    op.create_table(
        "cot_positions",
        sa.Column("report", sa.String(16), primary_key=True),
        sa.Column("market_code", sa.String(6), primary_key=True),
        sa.Column("report_date", sa.Date(), primary_key=True),
        sa.Column("category", sa.String(24), primary_key=True),
        sa.Column("long", sa.BigInteger(), nullable=False),
        sa.Column("short", sa.BigInteger(), nullable=False),
        sa.Column("spreading", sa.BigInteger()),
        sa.ForeignKeyConstraint(
            ["report", "market_code", "report_date"],
            ["cot_weeks.report", "cot_weeks.market_code", "cot_weeks.report_date"],
            name="fk_cot_positions_week",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "cot_loads",
        sa.Column("report", sa.String(16), primary_key=True),
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("file", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weeks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_day", sa.Date()),
        sa.Column("last_day", sa.Date()),
    )


def downgrade() -> None:
    op.drop_table("cot_loads")
    op.drop_table("cot_positions")
    op.drop_index("ix_cot_weeks_report_date", table_name="cot_weeks")
    op.drop_table("cot_weeks")
