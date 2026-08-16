"""Migration 006 — Enrichissement prices_eod pour multi-résolution

Revision ID: 006
Revises: 005
Create Date: 2026-05-20

Modifications :
    1. Ajouter colonnes resolution, adjusted, source à prices_eod
    2. Index composite : (asset_id, resolution, timestamp)
    3. Créer table ingest_log pour tracking des ingestions
"""

import sqlalchemy as sa

from alembic import op

revision = "006"
down_revision = "005"


def upgrade() -> None:

    # ── 1. Colonnes sur prices_eod ───────────────────────────────────────────
    op.add_column(
        "prices_eod", sa.Column("resolution", sa.String(3), nullable=False, server_default="1D")
    )
    op.add_column(
        "prices_eod", sa.Column("adjusted", sa.Boolean(), nullable=False, server_default="true")
    )
    op.add_column("prices_eod", sa.Column("source", sa.String(20), nullable=True))

    # ── 2. Index composite ───────────────────────────────────────────────────
    op.create_index(
        "ix_prices_eod_asset_resolution_time",
        "prices_eod",
        ["asset_id", "resolution", "time"],
        postgresql_using="btree",
    )

    # ── 3. Table ingest_log ──────────────────────────────────────────────────
    op.create_table(
        "ingest_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("resolution", sa.String(3), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),  # "success"|"failed"|"partial"
        sa.Column("records_added", sa.Integer(), nullable=True),
        sa.Column("from_date", sa.Date(), nullable=True),
        sa.Column("to_date", sa.Date(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ingest_log_asset_id", "ingest_log", ["asset_id"])
    op.create_index("ix_ingest_log_created_at", "ingest_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("ingest_log")
    op.drop_index("ix_prices_eod_asset_resolution_time", "prices_eod")
    op.drop_column("prices_eod", "source")
    op.drop_column("prices_eod", "adjusted")
    op.drop_column("prices_eod", "resolution")
