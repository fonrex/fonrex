"""Migration 010 — Table news_articles

Revision ID: 010
Revises: 009
Create Date: 2026-05-25

Description :
    Crée la table news_articles pour stocker les articles
    financiers agrégés depuis les providers de news.
    Politique de rétention : 90 jours (articles plus anciens supprimés).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=True),
        # Contenu
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("url", sa.String(1000), nullable=False, unique=True),
        sa.Column("image_url", sa.String(1000), nullable=True),
        # Source
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("author", sa.String(200), nullable=True),
        # Dates
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # Sentiment (optionnel — calculé en Phase suivante)
        sa.Column("sentiment", sa.String(10), nullable=True),
        sa.Column("sentiment_score", sa.Numeric(4, 3), nullable=True),
        # Tickers associés (un article peut concerner plusieurs actifs)
        sa.Column("related_tickers", postgresql.ARRAY(sa.String(20)), nullable=True),
        sa.Column("related_isin", postgresql.ARRAY(sa.String(12)), nullable=True),
        # Langue
        sa.Column("language", sa.String(5), nullable=True, server_default="en"),
    )

    # Index sur asset_id + published_at pour la requête principale
    op.create_index("ix_news_asset_published", "news_articles", ["asset_id", "published_at"])

    # Index sur published_at pour le feed global
    op.create_index("ix_news_published_at", "news_articles", ["published_at"])

    # Index sur provider pour les stats
    op.create_index("ix_news_provider", "news_articles", ["provider"])

    # Politique de rétention 90 jours (TimescaleDB si disponible)
    op.execute("""
        DO $$
        BEGIN
            BEGIN
                PERFORM add_retention_policy(
                    'news_articles',
                    INTERVAL '90 days',
                    if_not_exists => TRUE
                );
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;
        END $$;
    """)


def downgrade() -> None:
    op.drop_index("ix_news_provider")
    op.drop_index("ix_news_published_at")
    op.drop_index("ix_news_asset_published")
    op.drop_table("news_articles")
