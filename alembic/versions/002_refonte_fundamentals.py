"""Refactor fundamentals table — add normalized tables

Revision ID: 002
Revises: 001
Create Date: 2026-05-14

Description:
    - Creates the tables: fundamentals_highlights, financial_statements,
      earnings_history, analyst_ratings, etf_details, etf_holdings
    - The fundamentals (legacy) table is kept intact
    - Adds a SQL view fundamentals_v2 for backward compatibility
    - Migration of existing data from extra_metrics JSON
"""

import sqlalchemy as sa

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names()

    # ── 1. fundamentals_highlights ────────────────────────────────────────────
    if "fundamentals_highlights" not in existing_tables:
        op.create_table(
            "fundamentals_highlights",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "asset_id",
                sa.Integer(),
                sa.ForeignKey("assets.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "fetched_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                onupdate=sa.text("now()"),
            ),
            sa.Column("source", sa.String(50), server_default="yfinance"),
            # Valorisation
            sa.Column("market_cap", sa.Numeric(20, 2)),
            sa.Column("enterprise_value", sa.Numeric(20, 2)),
            sa.Column("pe_ratio", sa.Numeric(10, 4)),
            sa.Column("pe_forward", sa.Numeric(10, 4)),
            sa.Column("pb_ratio", sa.Numeric(10, 4)),
            sa.Column("ps_ratio", sa.Numeric(10, 4)),
            sa.Column("peg_ratio", sa.Numeric(10, 4)),
            sa.Column("ev_ebitda", sa.Numeric(10, 4)),
            sa.Column("ev_revenue", sa.Numeric(10, 4)),
            # Rentabilité
            sa.Column("roe", sa.Numeric(10, 6)),
            sa.Column("roa", sa.Numeric(10, 6)),
            sa.Column("roic", sa.Numeric(10, 6)),
            sa.Column("net_margin", sa.Numeric(10, 6)),
            sa.Column("operating_margin", sa.Numeric(10, 6)),
            sa.Column("gross_margin", sa.Numeric(10, 6)),
            # Per share
            sa.Column("eps_trailing", sa.Numeric(10, 4)),
            sa.Column("eps_forward", sa.Numeric(10, 4)),
            sa.Column("book_value_per_share", sa.Numeric(10, 4)),
            sa.Column("revenue_per_share", sa.Numeric(10, 4)),
            # Dividende
            sa.Column("dividend_yield", sa.Numeric(10, 6)),
            sa.Column("dividend_rate", sa.Numeric(10, 4)),
            sa.Column("dividend_ex_date", sa.Date()),
            sa.Column("dividend_pay_date", sa.Date()),
            sa.Column("payout_ratio", sa.Numeric(10, 6)),
            # Technique
            sa.Column("beta", sa.Numeric(8, 4)),
            sa.Column("week_52_high", sa.Numeric(14, 4)),
            sa.Column("week_52_low", sa.Numeric(14, 4)),
            sa.Column("ma_50", sa.Numeric(14, 4)),
            sa.Column("ma_200", sa.Numeric(14, 4)),
            # Ownership
            sa.Column("shares_outstanding", sa.BigInteger()),
            sa.Column("float_shares", sa.BigInteger()),
            sa.Column("pct_insiders", sa.Numeric(8, 6)),
            sa.Column("pct_institutions", sa.Numeric(8, 6)),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_fund_highlights_asset_id", "fundamentals_highlights", ["asset_id"])
        op.create_index("ix_fund_highlights_fetched_at", "fundamentals_highlights", ["fetched_at"])

    # ── 2. financial_statements ───────────────────────────────────────────────
    if "financial_statements" not in existing_tables:
        op.create_table(
            "financial_statements",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "asset_id",
                sa.Integer(),
                sa.ForeignKey("assets.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("statement_type", sa.String(20), nullable=False),
            sa.Column("period_type", sa.String(10), nullable=False),
            sa.Column("period_end", sa.Date(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("currency", sa.String(3), server_default="USD"),
            # Compte de résultat
            sa.Column("revenue", sa.Numeric(20, 2)),
            sa.Column("gross_profit", sa.Numeric(20, 2)),
            sa.Column("ebitda", sa.Numeric(20, 2)),
            sa.Column("ebit", sa.Numeric(20, 2)),
            sa.Column("operating_income", sa.Numeric(20, 2)),
            sa.Column("net_income", sa.Numeric(20, 2)),
            sa.Column("eps_basic", sa.Numeric(10, 4)),
            sa.Column("eps_diluted", sa.Numeric(10, 4)),
            sa.Column("shares_basic", sa.BigInteger()),
            sa.Column("shares_diluted", sa.BigInteger()),
            sa.Column("rd_expense", sa.Numeric(20, 2)),
            sa.Column("sga_expense", sa.Numeric(20, 2)),
            sa.Column("interest_expense", sa.Numeric(20, 2)),
            sa.Column("tax_provision", sa.Numeric(20, 2)),
            # Bilan
            sa.Column("total_assets", sa.Numeric(20, 2)),
            sa.Column("total_liabilities", sa.Numeric(20, 2)),
            sa.Column("total_equity", sa.Numeric(20, 2)),
            sa.Column("total_debt", sa.Numeric(20, 2)),
            sa.Column("net_debt", sa.Numeric(20, 2)),
            sa.Column("cash_and_equivalents", sa.Numeric(20, 2)),
            sa.Column("short_term_investments", sa.Numeric(20, 2)),
            sa.Column("accounts_receivable", sa.Numeric(20, 2)),
            sa.Column("inventory", sa.Numeric(20, 2)),
            sa.Column("goodwill", sa.Numeric(20, 2)),
            sa.Column("intangible_assets", sa.Numeric(20, 2)),
            sa.Column("long_term_debt", sa.Numeric(20, 2)),
            sa.Column("retained_earnings", sa.Numeric(20, 2)),
            # Cash Flow
            sa.Column("operating_cashflow", sa.Numeric(20, 2)),
            sa.Column("investing_cashflow", sa.Numeric(20, 2)),
            sa.Column("financing_cashflow", sa.Numeric(20, 2)),
            sa.Column("free_cashflow", sa.Numeric(20, 2)),
            sa.Column("capex", sa.Numeric(20, 2)),
            sa.Column("dividends_paid", sa.Numeric(20, 2)),
            sa.Column("stock_repurchases", sa.Numeric(20, 2)),
            sa.Column("depreciation_amortization", sa.Numeric(20, 2)),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "asset_id",
                "statement_type",
                "period_type",
                "period_end",
                name="uq_financial_statement",
            ),
        )
        op.create_index(
            "ix_financial_stmt_asset_period", "financial_statements", ["asset_id", "period_end"]
        )

    # ── 3. earnings_history ───────────────────────────────────────────────────
    if "earnings_history" not in existing_tables:
        op.create_table(
            "earnings_history",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "asset_id",
                sa.Integer(),
                sa.ForeignKey("assets.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("period", sa.String(10), nullable=False),
            sa.Column("period_end", sa.Date()),
            sa.Column("eps_actual", sa.Numeric(10, 4)),
            sa.Column("eps_estimate", sa.Numeric(10, 4)),
            sa.Column("surprise", sa.Numeric(10, 4)),
            sa.Column("surprise_pct", sa.Numeric(8, 4)),
            sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("asset_id", "period", name="uq_earnings_period"),
        )
        op.create_index("ix_earnings_asset_id", "earnings_history", ["asset_id"])

    # ── 4. analyst_ratings ────────────────────────────────────────────────────
    if "analyst_ratings" not in existing_tables:
        op.create_table(
            "analyst_ratings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "asset_id",
                sa.Integer(),
                sa.ForeignKey("assets.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "fetched_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                onupdate=sa.text("now()"),
            ),
            sa.Column("consensus", sa.String(20)),
            sa.Column("target_mean", sa.Numeric(14, 4)),
            sa.Column("target_low", sa.Numeric(14, 4)),
            sa.Column("target_high", sa.Numeric(14, 4)),
            sa.Column("target_median", sa.Numeric(14, 4)),
            sa.Column("nb_analysts", sa.Integer()),
            sa.Column("strong_buy", sa.Integer(), server_default="0"),
            sa.Column("buy", sa.Integer(), server_default="0"),
            sa.Column("hold", sa.Integer(), server_default="0"),
            sa.Column("sell", sa.Integer(), server_default="0"),
            sa.Column("strong_sell", sa.Integer(), server_default="0"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_analyst_ratings_asset_id", "analyst_ratings", ["asset_id"])

    # ── 5. etf_details ────────────────────────────────────────────────────────
    if "etf_details" not in existing_tables:
        op.create_table(
            "etf_details",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "asset_id",
                sa.Integer(),
                sa.ForeignKey("assets.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "fetched_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                onupdate=sa.text("now()"),
            ),
            sa.Column("inception_date", sa.Date()),
            sa.Column("net_expense_ratio", sa.Numeric(8, 6)),
            sa.Column("annual_holdings_turnover", sa.Numeric(8, 4)),
            sa.Column("total_net_assets", sa.Numeric(20, 2)),
            sa.Column("average_market_cap", sa.Numeric(20, 2)),
            sa.Column("is_ucits", sa.Boolean(), server_default="false"),
            sa.Column("domicile", sa.String(5)),
            sa.Column("replication_method", sa.String(20)),
            sa.Column("distribution_policy", sa.String(20)),
            sa.Column("ytd_return", sa.Numeric(8, 6)),
            sa.Column("return_1y", sa.Numeric(8, 6)),
            sa.Column("return_3y", sa.Numeric(8, 6)),
            sa.Column("return_5y", sa.Numeric(8, 6)),
            sa.Column("volatility_1y", sa.Numeric(8, 6)),
            sa.Column("sharpe_ratio", sa.Numeric(8, 4)),
            sa.Column("tracking_error", sa.Numeric(8, 6)),
            sa.Column("alloc_cash", sa.Numeric(6, 4)),
            sa.Column("alloc_stock_us", sa.Numeric(6, 4)),
            sa.Column("alloc_stock_non_us", sa.Numeric(6, 4)),
            sa.Column("alloc_bond", sa.Numeric(6, 4)),
            sa.Column("alloc_other", sa.Numeric(6, 4)),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_etf_details_asset_id", "etf_details", ["asset_id"])

    # ── 6. etf_holdings ───────────────────────────────────────────────────────
    if "etf_holdings" not in existing_tables:
        op.create_table(
            "etf_holdings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "etf_asset_id",
                sa.Integer(),
                sa.ForeignKey("assets.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("holding_ticker", sa.String(20)),
            sa.Column("holding_isin", sa.String(12)),
            sa.Column("holding_name", sa.String(200)),
            sa.Column("weight", sa.Numeric(8, 6)),
            sa.Column("sector", sa.String(100)),
            sa.Column("country", sa.String(5)),
            sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("etf_asset_id", "holding_ticker", name="uq_etf_holding"),
        )
        op.create_index("ix_etf_holdings_etf_id", "etf_holdings", ["etf_asset_id"])

    # ── 7. Migrate existing data from fundamentals.extra_metrics ──────────────
    # Only runs if fundamentals table exists and fundamentals_highlights is empty
    try:
        # Use a savepoint to protect the main transaction
        with conn.begin_nested():
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'fundamentals')"
                )
            )
            fundamentals_exists = result.scalar()

            if fundamentals_exists:
                result = conn.execute(sa.text("SELECT COUNT(*) FROM fundamentals_highlights"))
                highlights_count = result.scalar()

                if highlights_count == 0:
                    # Check which columns exist in fundamentals to be robust
                    result = conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns WHERE table_name = 'fundamentals'"
                        )
                    )
                    cols = {r[0] for r in result.fetchall()}

                    time_col = "time" if "time" in cols else "timestamp"
                    data_col = "data" if "data" in cols else "extra_metrics"

                    # For market_cap, pe_ratio, dividend_yield, they might be columns or in the json
                    mc_col = (
                        "market_cap"
                        if "market_cap" in cols
                        else f"({data_col}->>'market_cap')::numeric"
                    )
                    pe_col = (
                        "pe_ratio" if "pe_ratio" in cols else f"({data_col}->>'pe_ratio')::numeric"
                    )
                    dy_col = (
                        "dividend_yield"
                        if "dividend_yield" in cols
                        else f"({data_col}->>'dividend_yield')::numeric"
                    )

                    conn.execute(
                        sa.text(f"""
                        INSERT INTO fundamentals_highlights (
                            asset_id, fetched_at, source,
                            market_cap, pe_ratio, dividend_yield,
                            roe, roa, pe_forward, pb_ratio, ps_ratio,
                            ev_ebitda, net_margin, beta,
                            week_52_high, week_52_low
                        )
                        SELECT DISTINCT ON (asset_id)
                            asset_id,
                            {time_col}           AS fetched_at,
                            'legacy_migration'  AS source,
                            {mc_col},
                            {pe_col},
                            {dy_col},
                            ({data_col}->>'roe')::numeric,
                            ({data_col}->>'roa')::numeric,
                            ({data_col}->>'forward_pe')::numeric,
                            ({data_col}->>'pb_ratio')::numeric,
                            ({data_col}->>'ps_ratio')::numeric,
                            ({data_col}->>'ev_ebitda')::numeric,
                            ({data_col}->>'net_margin')::numeric,
                            ({data_col}->>'beta')::numeric,
                            ({data_col}->>'52_week_high')::numeric,
                            ({data_col}->>'52_week_low')::numeric
                        FROM fundamentals
                        WHERE {data_col} IS NOT NULL
                        ORDER BY asset_id, {time_col} DESC
                        ON CONFLICT DO NOTHING
                    """)
                    )
    except Exception as e:
        # Non-fatal: migration of legacy data is best-effort
        import warnings

        warnings.warn(
            f"Could not migrate data from fundamentals.extra_metrics: {e}. "
            "This is non-fatal — data can be re-enriched via yfinance.",
            stacklevel=2,
        )

    # ── 8. Create compatibility view ──────────────────────────────────────────
    conn.execute(
        sa.text("""
        CREATE OR REPLACE VIEW fundamentals_v2 AS
        SELECT
            fh.fetched_at               AS timestamp,
            fh.asset_id,
            fh.market_cap,
            fh.pe_ratio,
            fh.dividend_yield,
            jsonb_build_object(
                'roe',          fh.roe,
                'roa',          fh.roa,
                'forward_pe',   fh.pe_forward,
                'pb_ratio',     fh.pb_ratio,
                'ps_ratio',     fh.ps_ratio,
                'ev_ebitda',    fh.ev_ebitda,
                'net_margin',   fh.net_margin,
                'beta',         fh.beta,
                '52_week_high', fh.week_52_high,
                '52_week_low',  fh.week_52_low
            ) AS extra_metrics
        FROM fundamentals_highlights fh
    """)
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Drop the compatibility view first
    conn.execute(sa.text("DROP VIEW IF EXISTS fundamentals_v2"))

    # Drop tables in reverse dependency order
    for table in (
        "etf_holdings",
        "etf_details",
        "analyst_ratings",
        "earnings_history",
        "financial_statements",
        "fundamentals_highlights",
    ):
        inspector = sa.inspect(conn)
        if table in inspector.get_table_names():
            op.drop_table(table)
