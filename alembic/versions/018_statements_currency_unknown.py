"""Migration 018 — the currency of the financial statements is recorded, or unknown

Revision ID: 018
Revises: 017
Create Date: 2026-10-10

``financial_statements.currency`` defaulted to ``USD`` and the enrichment never
wrote it: every row said USD, the statements of Airbus included. The enrichment
now stores Yahoo's ``financialCurrency``, and the valuation discounts the cash
flows with the risk-free rate of that currency.

The default is dropped and the rows already stored become NULL (unknown): the
valuation then takes the currency of the listing of the share price, until the
next enrichment of the instrument (``GET /fundamental/deep?ticker=...&refresh=true``)
records it.

The downgrade puts the default back and sets the unknown currencies to USD, as
they were.
"""

import sqlalchemy as sa

from alembic import op

revision = "018"
down_revision = "017"


def upgrade() -> None:
    op.alter_column(
        "financial_statements",
        "currency",
        existing_type=sa.String(3),
        server_default=None,
        existing_nullable=True,
    )
    op.execute("UPDATE financial_statements SET currency = NULL")


def downgrade() -> None:
    op.execute("UPDATE financial_statements SET currency = 'USD' WHERE currency IS NULL")
    op.alter_column(
        "financial_statements",
        "currency",
        existing_type=sa.String(3),
        server_default="USD",
        existing_nullable=True,
    )
