"""Migration 030 — the missing lines of the financial statements

Revision ID: 030
Revises: 029
Create Date: 2026-11-26

The ratios and scores of a listing (issue #70, phase 4) need lines that
``financial_statements`` did not hold: the current assets and liabilities
(liquidity, Altman, Piotroski, Beneish, Ohlson), the net property, plant and
equipment (fixed-asset turnover, Beneish), the accounts payable (days payable,
cash conversion cycle) and the income before tax (effective tax rate, ROIC).

Five nullable columns are added; no row is rewritten. A bank or an insurer
publishes no current assets nor liabilities (an unclassified balance sheet):
those columns stay ``NULL``, never zero. The downgrade drops the five columns.
"""

import sqlalchemy as sa

from alembic import op

revision = "030"
down_revision = "029"

COLUMNS = (
    "total_current_assets",
    "total_current_liabilities",
    "ppe_net",
    "accounts_payable",
    "income_before_tax",
)


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("financial_statements", sa.Column(name, sa.Numeric(20, 2), nullable=True))


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("financial_statements", name)
