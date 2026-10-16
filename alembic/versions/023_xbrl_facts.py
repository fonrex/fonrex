"""Migration 023 — facts of the XBRL filings of the SEC

Revision ID: 023
Revises: 022
Create Date: 2026-10-16

The SEC publishes the financial statements filed by a company as XBRL facts:
a concept of a taxonomy (``us-gaap:Revenues``, ``ifrs-full:Revenue``), a unit
(``USD``, ``EUR``, ``USD/shares``), a period and a value, with the filing it
comes from. They are kept apart from ``financial_statements`` (Yahoo), so that
the two sources can be compared:

- ``xbrl_facts``: one value per company (CIK), concept, unit and period. A
  period is its end and its length in days (0 for a balance at a date). The
  value of the latest filing is kept: a restatement replaces the earlier one,
  and ``accession`` names the filing it was read from.
- ``sec_company_loads``: the last reading of each company.

The downgrade drops both tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "023"
down_revision = "022"


def upgrade() -> None:
    op.create_table(
        "xbrl_facts",
        sa.Column("cik", sa.String(10), primary_key=True),
        sa.Column("taxonomy", sa.String(20), primary_key=True),
        sa.Column("concept", sa.String(200), primary_key=True),
        sa.Column("unit", sa.String(30), primary_key=True),
        sa.Column("period_end", sa.Date(), primary_key=True),
        sa.Column("duration_days", sa.Integer(), primary_key=True),
        sa.Column("period_start", sa.Date()),
        sa.Column("value", sa.Numeric(28, 6), nullable=False),
        sa.Column("accession", sa.String(25), nullable=False),
        sa.Column("form", sa.String(10), nullable=False),
        sa.Column("fiscal_year", sa.Integer()),
        sa.Column("fiscal_period", sa.String(4)),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("frame", sa.String(12)),
    )
    op.create_table(
        "sec_company_loads",
        sa.Column("cik", sa.String(10), primary_key=True),
        sa.Column("entity_name", sa.String(255)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_filed", sa.Date()),
        sa.Column("facts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("sec_company_loads")
    op.drop_table("xbrl_facts")
