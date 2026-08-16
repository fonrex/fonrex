"""Migration 004 — Champs Premium parité EODHD

Revision ID: 004
Revises: 003
Create Date: 2026-05-14

Modifications :
    1. Table fundamentals_highlights :
       - Ajouter colonnes short selling
       - Ajouter colonnes GICS classification
       - Ajouter colonnes TTM et croissance
    2. Nouvelles tables :
       - earnings_trend (estimations futures)
       - esg_scores
       - outstanding_shares_history
    3. Table assets :
       - Ajouter colonnes GICS
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── 1. Colonnes sur fundamentals_highlights ─────────────────────────────

    # Short selling
    op.add_column(
        "fundamentals_highlights", sa.Column("shares_short", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "fundamentals_highlights", sa.Column("shares_short_prior", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "fundamentals_highlights", sa.Column("short_ratio", sa.Numeric(8, 4), nullable=True)
    )
    op.add_column(
        "fundamentals_highlights", sa.Column("short_percent_float", sa.Numeric(8, 6), nullable=True)
    )
    op.add_column(
        "fundamentals_highlights",
        sa.Column("short_percent_outstanding", sa.Numeric(8, 6), nullable=True),
    )
    op.add_column(
        "fundamentals_highlights", sa.Column("shares_short_date", sa.Date(), nullable=True)
    )

    # TTM et croissance
    op.add_column(
        "fundamentals_highlights", sa.Column("gross_profit_ttm", sa.Numeric(20, 2), nullable=True)
    )
    op.add_column(
        "fundamentals_highlights", sa.Column("diluted_eps_ttm", sa.Numeric(10, 4), nullable=True)
    )
    op.add_column(
        "fundamentals_highlights",
        sa.Column("return_on_assets_ttm", sa.Numeric(10, 6), nullable=True),
    )
    op.add_column(
        "fundamentals_highlights",
        sa.Column("return_on_equity_ttm", sa.Numeric(10, 6), nullable=True),
    )
    op.add_column(
        "fundamentals_highlights",
        sa.Column("quarterly_revenue_growth_yoy", sa.Numeric(10, 6), nullable=True),
    )
    op.add_column(
        "fundamentals_highlights",
        sa.Column("quarterly_earnings_growth_yoy", sa.Numeric(10, 6), nullable=True),
    )
    op.add_column(
        "fundamentals_highlights", sa.Column("revenue_ttm", sa.Numeric(20, 2), nullable=True)
    )
    op.add_column(
        "fundamentals_highlights", sa.Column("ebitda_ttm", sa.Numeric(20, 2), nullable=True)
    )

    # ── 2. Colonnes GICS sur assets ──────────────────────────────────────────
    op.add_column("assets", sa.Column("gic_sector", sa.String(100), nullable=True))
    op.add_column("assets", sa.Column("gic_group", sa.String(100), nullable=True))
    op.add_column("assets", sa.Column("gic_industry", sa.String(100), nullable=True))
    op.add_column("assets", sa.Column("gic_sub_industry", sa.String(100), nullable=True))

    # ── 3. Table earnings_trend ──────────────────────────────────────────────
    op.create_table(
        "earnings_trend",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),  # "0q" | "+1q" | "0y" | "+1y"
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # Revenue estimates
        sa.Column("revenue_avg", sa.Numeric(20, 2), nullable=True),
        sa.Column("revenue_low", sa.Numeric(20, 2), nullable=True),
        sa.Column("revenue_high", sa.Numeric(20, 2), nullable=True),
        sa.Column("revenue_nb_analysts", sa.Integer(), nullable=True),
        sa.Column("revenue_growth", sa.Numeric(10, 6), nullable=True),
        # EPS estimates
        sa.Column("eps_avg", sa.Numeric(10, 4), nullable=True),
        sa.Column("eps_low", sa.Numeric(10, 4), nullable=True),
        sa.Column("eps_high", sa.Numeric(10, 4), nullable=True),
        sa.Column("eps_nb_analysts", sa.Integer(), nullable=True),
        sa.Column("eps_growth", sa.Numeric(10, 6), nullable=True),
        # Contrainte unicité
        sa.UniqueConstraint("asset_id", "period", name="uq_earnings_trend_period"),
    )
    op.create_index("ix_earnings_trend_asset_id", "earnings_trend", ["asset_id"])

    # ── 4. Table esg_scores ──────────────────────────────────────────────────
    op.create_table(
        "esg_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False, unique=True
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.Column("rating_date", sa.Date(), nullable=True),
        # Scores globaux (sur 100 ou percentile)
        sa.Column("total_esg", sa.Numeric(6, 2), nullable=True),
        sa.Column("environment_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("social_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("governance_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("controversy_level", sa.Integer(), nullable=True),  # 0-5
        sa.Column("controversy_score", sa.Numeric(6, 2), nullable=True),
        # Percentiles dans l'industrie
        sa.Column("percentile", sa.Numeric(6, 2), nullable=True),
        sa.Column("peer_group", sa.String(200), nullable=True),
        sa.Column("peer_count", sa.Integer(), nullable=True),
        sa.Column("peer_esg_score_perf_min", sa.Numeric(6, 2), nullable=True),
        sa.Column("peer_esg_score_perf_avg", sa.Numeric(6, 2), nullable=True),
        sa.Column("peer_esg_score_perf_max", sa.Numeric(6, 2), nullable=True),
        # Catégories de risque
        sa.Column(
            "esg_risk_rating", sa.String(50), nullable=True
        ),  # "Negligible"|"Low"|"Medium"|"High"|"Severe"
        sa.Column("highest_controversy", sa.Integer(), nullable=True),
        sa.Column("adult", sa.Boolean(), nullable=True),
        sa.Column("alcoholic", sa.Boolean(), nullable=True),
        sa.Column("animal_testing", sa.Boolean(), nullable=True),
        sa.Column("catholic", sa.Boolean(), nullable=True),
        sa.Column("controversial_weapons", sa.Boolean(), nullable=True),
        sa.Column("small_arms", sa.Boolean(), nullable=True),
        sa.Column("fur_leather", sa.Boolean(), nullable=True),
        sa.Column("gambling", sa.Boolean(), nullable=True),
        sa.Column("gmo", sa.Boolean(), nullable=True),
        sa.Column("military_contract", sa.Boolean(), nullable=True),
        sa.Column("nuclear", sa.Boolean(), nullable=True),
        sa.Column("pesticides", sa.Boolean(), nullable=True),
        sa.Column("palm_oil", sa.Boolean(), nullable=True),
        sa.Column("coal", sa.Boolean(), nullable=True),
        sa.Column("tobacco", sa.Boolean(), nullable=True),
    )
    op.create_index("ix_esg_scores_asset_id", "esg_scores", ["asset_id"])

    # ── 5. Table outstanding_shares_history ──────────────────────────────────
    op.create_table(
        "outstanding_shares_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("shares", sa.BigInteger(), nullable=False),
        sa.Column("period_type", sa.String(10), nullable=True),  # "annual" | "quarterly"
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("asset_id", "date", name="uq_outstanding_shares_date"),
    )
    op.create_index("ix_outstanding_shares_asset_id", "outstanding_shares_history", ["asset_id"])


def downgrade() -> None:
    op.drop_table("outstanding_shares_history")
    op.drop_table("esg_scores")
    op.drop_table("earnings_trend")
    # Supprimer les colonnes GICS de assets
    for col in ["gic_sector", "gic_group", "gic_industry", "gic_sub_industry"]:
        op.drop_column("assets", col)
    # Supprimer les colonnes de fundamentals_highlights
    for col in [
        "shares_short",
        "shares_short_prior",
        "short_ratio",
        "short_percent_float",
        "short_percent_outstanding",
        "shares_short_date",
        "gross_profit_ttm",
        "diluted_eps_ttm",
        "return_on_assets_ttm",
        "return_on_equity_ttm",
        "quarterly_revenue_growth_yoy",
        "quarterly_earnings_growth_yoy",
        "revenue_ttm",
        "ebitda_ttm",
    ]:
        op.drop_column("fundamentals_highlights", col)
