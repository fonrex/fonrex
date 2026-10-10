"""Migration 013 — Solvency ratios + macro rate cache

Revision ID: 013
Revises: 012
Create Date: 2026-09-08

Changes:
    1. Add 4 solvency ratio columns to fundamentals_highlights
    2. Add 2 actual cost of debt columns
    3. Create the macro_rates_cache table for the FRED rates
"""
import sqlalchemy as sa

from alembic import op

revision = '013'
down_revision = '012'


def upgrade() -> None:

    # ── 1. Solvency ratios on fundamentals_highlights ────────────────────────
    op.add_column('fundamentals_highlights', sa.Column(
        'debt_to_equity_ratio', sa.Numeric(10, 4), nullable=True))
    op.add_column('fundamentals_highlights', sa.Column(
        'debt_to_assets_ratio', sa.Numeric(10, 6), nullable=True))
    op.add_column('fundamentals_highlights', sa.Column(
        'net_debt_to_ebitda', sa.Numeric(10, 4), nullable=True))
    op.add_column('fundamentals_highlights', sa.Column(
        'interest_coverage_ratio', sa.Numeric(10, 4), nullable=True))

    # ── 2. Actual cost of debt ────────────────────────────────────────────────
    op.add_column('fundamentals_highlights', sa.Column(
        'actual_cost_of_debt', sa.Numeric(8, 6), nullable=True))
    # Computed cost of debt: interest_expense / total_debt (weighted average
    # over the last 3 fiscal years available)
    op.add_column('fundamentals_highlights', sa.Column(
        'cost_of_debt_source', sa.String(20), nullable=True))
    # "calculated" (from the actual financial statements) | "sector_estimate"
    # (repli si interest_expense ou total_debt indisponible/nul)

    # ── 3. Macro rate cache (FRED) ────────────────────────────────────────────
    op.create_table(
        'macro_rates_cache',
        sa.Column('id',           sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column('series_id',    sa.String(30), nullable=False),
        # ex: "DGS10" (10Y Treasury), "DFF" (Fed Funds), "ECBDFR" (BCE)
        sa.Column('label',        sa.String(100), nullable=True),
        sa.Column('value',        sa.Numeric(10, 6), nullable=False),
        sa.Column('unit',         sa.String(10), nullable=True),  # "percent"
        sa.Column('observation_date', sa.Date(), nullable=False),
        sa.Column('fetched_at',   sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('series_id', 'observation_date',
                            name='uq_macro_rate_series_date'),
    )
    op.create_index('ix_macro_rates_series_date',
                    'macro_rates_cache', ['series_id', 'observation_date'])


def downgrade() -> None:
    op.drop_table('macro_rates_cache')
    for col in ['cost_of_debt_source', 'actual_cost_of_debt',
               'interest_coverage_ratio', 'net_debt_to_ebitda',
               'debt_to_assets_ratio', 'debt_to_equity_ratio']:
        op.drop_column('fundamentals_highlights', col)
