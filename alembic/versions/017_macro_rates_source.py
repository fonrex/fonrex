"""Migration 017 — macro rates from several sources

Revision ID: 017
Revises: 016
Create Date: 2026-10-10

``macro_rates_cache`` held FRED series only (``DGS10``). It now also holds
series of the European Central Bank, named by their flow and key
(``YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y``, 35 characters): ``series_id`` is widened
from 30 to 60 characters, and a ``source`` column says where a row was read
(``fred`` or ``ecb``). The rows already stored come from FRED.

The downgrade drops the rows whose name no longer fits in 30 characters, then
the column.
"""

import sqlalchemy as sa

from alembic import op

revision = "017"
down_revision = "016"


def upgrade() -> None:
    op.alter_column(
        "macro_rates_cache",
        "series_id",
        existing_type=sa.String(30),
        type_=sa.String(60),
        existing_nullable=False,
    )
    op.add_column("macro_rates_cache", sa.Column("source", sa.String(10), nullable=True))
    op.execute("UPDATE macro_rates_cache SET source = 'fred' WHERE source IS NULL")


def downgrade() -> None:
    op.execute("DELETE FROM macro_rates_cache WHERE length(series_id) > 30")
    op.drop_column("macro_rates_cache", "source")
    op.alter_column(
        "macro_rates_cache",
        "series_id",
        existing_type=sa.String(60),
        type_=sa.String(30),
        existing_nullable=False,
    )
