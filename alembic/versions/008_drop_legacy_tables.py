"""Migration 008 — Suppression des tables EOD legacy

Revision ID: 008
Revises: 007
Create Date: 2026-05-23

Description :
    Supprime les tables legacy du chemin EOD historique :
    - stock_data, cache_status, data_requests
    - daily_stock_records, weekly_stock_records,
      monthly_stock_records, yearly_stock_records

    Ces tables sont remplacées par prices_eod (TimescaleDB hypertable)
    alimentée par HistoricalIngestionService.

    ⚠️  MIGRATION DESTRUCTIVE — Données non récupérables après exécution.
    Effectuer une sauvegarde PostgreSQL avant d'appliquer :
    pg_dump -Fc fonrex > backup_pre_008.dump

    Le downgrade recrée les tables VIDES (sans données).
    La restauration des données nécessite le backup.
"""

import sqlalchemy as sa

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Supprime toutes les tables legacy dans le bon ordre
    (respecter les FK si elles existent).

    Utiliser IF EXISTS pour rendre la migration idempotente
    (safe à rejouer si partiellement appliquée).
    """

    # Tables de records historiques (pas de FK vers d'autres tables legacy)
    op.execute("DROP TABLE IF EXISTS daily_stock_records CASCADE")
    op.execute("DROP TABLE IF EXISTS weekly_stock_records CASCADE")
    op.execute("DROP TABLE IF EXISTS monthly_stock_records CASCADE")
    op.execute("DROP TABLE IF EXISTS yearly_stock_records CASCADE")

    # Tables EOD legacy (data_requests peut avoir FK vers stock_data)
    op.execute("DROP TABLE IF EXISTS data_requests CASCADE")
    op.execute("DROP TABLE IF EXISTS cache_status CASCADE")
    op.execute("DROP TABLE IF EXISTS stock_data CASCADE")

    # Supprimer aussi les séquences orphelines si elles existent
    op.execute("DROP SEQUENCE IF EXISTS stock_data_id_seq CASCADE")
    op.execute("DROP SEQUENCE IF EXISTS data_requests_id_seq CASCADE")


def downgrade() -> None:
    """
    Recrée les tables legacy VIDES pour permettre un rollback de schéma.
    ⚠️  Les données ne sont PAS restaurées — utiliser le backup PostgreSQL.

    Recrée dans l'ordre inverse : parents avant enfants.
    """

    # stock_data (table principale legacy)
    op.create_table(
        "stock_data",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(14, 4)),
        sa.Column("high", sa.Numeric(14, 4)),
        sa.Column("low", sa.Numeric(14, 4)),
        sa.Column("close", sa.Numeric(14, 4)),
        sa.Column("adj_close", sa.Numeric(14, 4)),
        sa.Column("volume", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker", "date", name="uq_stock_data_ticker_date"),
    )
    op.create_index("ix_stock_data_ticker", "stock_data", ["ticker"])
    op.create_index("ix_stock_data_date", "stock_data", ["date"])

    # cache_status
    op.create_table(
        "cache_status",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(20), nullable=False, unique=True),
        sa.Column("last_updated", sa.DateTime(timezone=True)),
        sa.Column("is_fresh", sa.Boolean(), default=False),
    )

    # data_requests
    op.create_table(
        "data_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(20)),
        sa.Column("period", sa.String(10)),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("status", sa.String(20)),
        sa.Column("source", sa.String(20)),
    )

    # Tables de records (structure minimale)
    for table_name in [
        "daily_stock_records",
        "weekly_stock_records",
        "monthly_stock_records",
        "yearly_stock_records",
    ]:
        op.create_table(
            table_name,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(20), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("open", sa.Numeric(14, 4)),
            sa.Column("high", sa.Numeric(14, 4)),
            sa.Column("low", sa.Numeric(14, 4)),
            sa.Column("close", sa.Numeric(14, 4)),
            sa.Column("volume", sa.BigInteger()),
        )
