"""Fix assets table missing columns

Revision ID: 004
Revises: 003
Create Date: 2026-05-14

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # On vérifie si les colonnes existent déjà pour éviter les erreurs
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("assets")]

    if "currency" not in columns:
        op.add_column("assets", sa.Column("currency", sa.String(length=10), nullable=True))
    if "sector" not in columns:
        op.add_column("assets", sa.Column("sector", sa.String(length=100), nullable=True))
    if "industry" not in columns:
        op.add_column("assets", sa.Column("industry", sa.String(length=100), nullable=True))
    if "is_active" not in columns:
        op.add_column(
            "assets", sa.Column("is_active", sa.Boolean(), server_default="true", nullable=True)
        )
    if "isin" not in columns:
        op.add_column("assets", sa.Column("isin", sa.String(length=20), nullable=True))
    if "quote_type" not in columns:
        op.add_column("assets", sa.Column("quote_type", sa.String(length=20), nullable=True))
    if "fund_family" not in columns:
        op.add_column("assets", sa.Column("fund_family", sa.String(length=100), nullable=True))
    if "long_business_summary" not in columns:
        op.add_column("assets", sa.Column("long_business_summary", sa.Text(), nullable=True))
    if "display_name" not in columns:
        op.add_column("assets", sa.Column("display_name", sa.String(length=255), nullable=True))
    if "official_symbol" not in columns:
        op.add_column("assets", sa.Column("official_symbol", sa.String(length=50), nullable=True))
    if "ir_website" not in columns:
        op.add_column("assets", sa.Column("ir_website", sa.String(length=255), nullable=True))
    if "logo_path" not in columns:
        op.add_column("assets", sa.Column("logo_path", sa.String(length=255), nullable=True))
    if "country" not in columns:
        op.add_column("assets", sa.Column("country", sa.String(length=100), nullable=True))
    if "country_code" not in columns:
        op.add_column("assets", sa.Column("country_code", sa.String(length=10), nullable=True))
    if "profile" not in columns:
        op.add_column(
            "assets", sa.Column("profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
        )

    # Index ISIN (001 peut déjà les avoir créés sur une base fraîche)
    indexes = {index["name"] for index in inspector.get_indexes("assets")}
    if "idx_assets_isin" not in indexes:
        op.create_index("idx_assets_isin", "assets", ["isin"], unique=False)
    if "uq_assets_isin_not_null" not in indexes:
        op.create_index(
            "uq_assets_isin_not_null",
            "assets",
            ["isin"],
            unique=True,
            postgresql_where=sa.text("isin IS NOT NULL"),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_assets_isin_not_null", table_name="assets", postgresql_where=sa.text("isin IS NOT NULL")
    )
    op.drop_index("idx_assets_isin", table_name="assets")
    op.drop_column("assets", "profile")
    op.drop_column("assets", "country_code")
    op.drop_column("assets", "country")
    op.drop_column("assets", "logo_path")
    op.drop_column("assets", "ir_website")
    op.drop_column("assets", "official_symbol")
    op.drop_column("assets", "display_name")
    op.drop_column("assets", "long_business_summary")
    op.drop_column("assets", "fund_family")
    op.drop_column("assets", "quote_type")
    op.drop_column("assets", "isin")
    op.drop_column("assets", "is_active")
    op.drop_column("assets", "industry")
    op.drop_column("assets", "sector")
    op.drop_column("assets", "currency")
