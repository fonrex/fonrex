"""Migration 020 — factor returns of the Kenneth French library

Revision ID: 020
Revises: 019
Create Date: 2026-10-10

``factor_returns`` holds the factor returns (Fama/French 3 and 5 factors,
momentum) of the US, Europe and developed markets datasets, monthly and daily,
one row per dataset, frequency, period and factor, as ratios. ``factor_dataset_loads``
records the last download of each file: the library revises past values, so a
file is always stored whole, replacing what was there.
"""

import sqlalchemy as sa

from alembic import op

revision = "020"
down_revision = "019"


def upgrade() -> None:
    op.create_table(
        "factor_returns",
        sa.Column("dataset", sa.String(20), primary_key=True),
        sa.Column("frequency", sa.String(10), primary_key=True),
        sa.Column("period_end", sa.Date(), primary_key=True),
        sa.Column("factor", sa.String(10), primary_key=True),
        sa.Column("value", sa.Numeric(12, 8), nullable=False),
    )
    op.create_table(
        "factor_dataset_loads",
        sa.Column("dataset", sa.String(20), primary_key=True),
        sa.Column("frequency", sa.String(10), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_period", sa.Date()),
        sa.Column("last_period", sa.Date()),
        sa.Column("periods", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_note", sa.String(40)),
    )


def downgrade() -> None:
    op.drop_table("factor_dataset_loads")
    op.drop_table("factor_returns")
