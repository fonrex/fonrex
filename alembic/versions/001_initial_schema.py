"""Initial schema snapshot — represents the database state before Alembic was introduced.

Revision ID: 001
Revises:
Create Date: 2026-05-14

Description:
    Documents all tables existing before Alembic was introduced to Fonrex.
    On existing deployments, run `alembic stamp 001` instead of `alembic upgrade 001`
    to mark this revision as already applied without re-creating tables.

    Tables documented:
        - assets
        - asset_listings
        - asset_mappings
        - prices_eod       (TimescaleDB hypertable)
        - fundamentals     (TimescaleDB hypertable — legacy, kept for compatibility)
        - stock_data
        - data_requests
        - usage_logs
        - cache_status
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create initial tables if they don't already exist.
    Safe to run on a fresh DB; on existing DBs use `alembic stamp 001`.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names()

    if conn.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    # ── assets ────────────────────────────────────────────────────────────────
    if "assets" not in existing_tables:
        op.create_table(
            "assets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticker", sa.String(50), nullable=False),
            sa.Column("name", sa.String(255)),
            sa.Column("exchange", sa.String(50)),
            sa.Column("currency", sa.String(10)),
            sa.Column("sector", sa.String(100)),
            sa.Column("industry", sa.String(100)),
            sa.Column("is_active", sa.Boolean(), default=True),
            sa.Column("isin", sa.String(20)),
            sa.Column("quote_type", sa.String(20)),
            sa.Column("fund_family", sa.String(100)),
            sa.Column("long_business_summary", sa.Text()),
            sa.Column("display_name", sa.String(255)),
            sa.Column("official_symbol", sa.String(50)),
            sa.Column("ir_website", sa.String(255)),
            sa.Column("logo_path", sa.String(255)),
            sa.Column("country", sa.String(100)),
            sa.Column("country_code", sa.String(10)),
            sa.Column("profile", postgresql.JSONB()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_assets_ticker_exchange", "assets", ["ticker", "exchange"])
        op.create_index("idx_assets_isin", "assets", ["isin"])
        op.create_index(
            "uq_assets_isin_not_null",
            "assets",
            ["isin"],
            unique=True,
            postgresql_where=sa.text("isin IS NOT NULL"),
        )

    # ── TimescaleDB source tables ───────────────────────────────────────────
    if "prices_eod" not in existing_tables:
        op.create_table(
            "prices_eod",
            sa.Column("time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("asset_listing_id", sa.Integer(), nullable=True),
            sa.Column("open", sa.Float()),
            sa.Column("high", sa.Float()),
            sa.Column("low", sa.Float()),
            sa.Column("close", sa.Float()),
            sa.Column("adj_close", sa.Float()),
            sa.Column("volume", sa.BigInteger()),
            sa.PrimaryKeyConstraint("time", "asset_id"),
            sa.UniqueConstraint("time", "asset_id", name="prices_eod_timestamp_asset_id_key"),
        )
        op.create_index(
            "idx_prices_asset_timestamp", "prices_eod", ["asset_id", sa.text("time DESC")]
        )
        op.create_index(
            "idx_prices_listing_timestamp", "prices_eod", ["asset_listing_id", sa.text("time DESC")]
        )
        if conn.dialect.name == "postgresql":
            op.execute("SELECT create_hypertable('prices_eod', 'time', if_not_exists => TRUE)")

    if "fundamentals" not in existing_tables:
        op.create_table(
            "fundamentals",
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("market_cap", sa.Float()),
            sa.Column("pe_ratio", sa.Float()),
            sa.Column("dividend_yield", sa.Float()),
            sa.Column("extra_metrics", postgresql.JSONB().with_variant(sa.JSON(), "sqlite")),
            sa.PrimaryKeyConstraint("timestamp", "asset_id"),
        )
        if conn.dialect.name == "postgresql":
            op.execute(
                "SELECT create_hypertable('fundamentals', 'timestamp', if_not_exists => TRUE)"
            )

    # ── asset_listings ────────────────────────────────────────────────────────
    if "asset_listings" not in existing_tables:
        op.create_table(
            "asset_listings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "asset_id",
                sa.Integer(),
                sa.ForeignKey("assets.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ticker", sa.String(50), nullable=False),
            sa.Column("exchange", sa.String(50), nullable=False, server_default=""),
            sa.Column("currency", sa.String(10), nullable=False, server_default=""),
            sa.Column("source", sa.String(50), nullable=False, server_default="manual"),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "asset_id", "ticker", "exchange", "currency", name="uq_asset_listing_identity"
            ),
        )
        op.create_index(
            "idx_asset_listings_ticker_identity",
            "asset_listings",
            ["ticker", "exchange", "currency"],
        )
        op.create_index("idx_asset_listings_asset", "asset_listings", ["asset_id"])

    # ── asset_mappings ────────────────────────────────────────────────────────
    if "asset_mappings" not in existing_tables:
        op.create_table(
            "asset_mappings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column(
                "asset_listing_id",
                sa.Integer(),
                sa.ForeignKey("asset_listings.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("provider_name", sa.String(50), nullable=False),
            sa.Column("provider_ticker", sa.String(50)),
            sa.Column("provider_url", sa.String(500)),
            sa.Column("source", sa.String(50), nullable=False, server_default="manual"),
            sa.Column("confidence_score", sa.Float()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_verified_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "asset_listing_id", "provider_name", name="uq_asset_listing_provider"
            ),
        )
        op.create_index(
            "idx_asset_mappings_asset_provider", "asset_mappings", ["asset_id", "provider_name"]
        )
        op.create_index(
            "idx_asset_mappings_provider_ticker",
            "asset_mappings",
            ["provider_name", "provider_ticker"],
        )
        op.create_index(
            "idx_asset_mappings_active", "asset_mappings", ["provider_name", "is_active"]
        )

    # ── stock_data ────────────────────────────────────────────────────────────
    if "stock_data" not in existing_tables:
        op.create_table(
            "stock_data",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticker", sa.String(20), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("open_price", sa.Numeric(precision=12, scale=6), nullable=False),
            sa.Column("high_price", sa.Numeric(precision=12, scale=6), nullable=False),
            sa.Column("low_price", sa.Numeric(precision=12, scale=6), nullable=False),
            sa.Column("close_price", sa.Numeric(precision=12, scale=6), nullable=False),
            sa.Column("adj_close_price", sa.Numeric(precision=12, scale=6), nullable=False),
            sa.Column("volume", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ticker", "date", name="uq_ticker_date"),
        )
        op.create_index("idx_ticker_date", "stock_data", ["ticker", "date"])
        op.create_index("idx_created_at", "stock_data", ["created_at"])

    # ── data_requests ─────────────────────────────────────────────────────────
    if "data_requests" not in existing_tables:
        op.create_table(
            "data_requests",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticker", sa.String(20), nullable=False),
            sa.Column("period", sa.String(10), nullable=False),
            sa.Column("format_requested", sa.String(10), nullable=False),
            sa.Column("success", sa.Boolean(), nullable=False),
            sa.Column("records_returned", sa.Integer()),
            sa.Column("data_source", sa.String(20), nullable=False),
            sa.Column("response_time_ms", sa.Integer()),
            sa.Column("ip_address", sa.String(45)),
            sa.Column("user_agent", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_ticker_created", "data_requests", ["ticker", "created_at"])
        op.create_index("idx_success_created", "data_requests", ["success", "created_at"])
        op.create_index("idx_data_source", "data_requests", ["data_source"])

    # ── usage_logs ────────────────────────────────────────────────────────────
    if "usage_logs" not in existing_tables:
        op.create_table(
            "usage_logs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("api_key_id", sa.String(100)),
            sa.Column("endpoint", sa.String(255), nullable=False),
            sa.Column("method", sa.String(10), nullable=False),
            sa.Column("provider_used", sa.String(100)),
            sa.Column("cache_hit", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("latency_ms", sa.Integer(), nullable=False),
            sa.Column("cost_bucket", sa.String(50)),
            sa.Column("ip_address", sa.String(45)),
            sa.Column("user_agent", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_usage_logs_created", "usage_logs", ["created_at"])
        op.create_index("idx_usage_logs_endpoint_created", "usage_logs", ["endpoint", "created_at"])
        op.create_index(
            "idx_usage_logs_api_key_created", "usage_logs", ["api_key_id", "created_at"]
        )
        op.create_index(
            "idx_usage_logs_provider_created", "usage_logs", ["provider_used", "created_at"]
        )

    # ── cache_status ──────────────────────────────────────────────────────────
    if "cache_status" not in existing_tables:
        op.create_table(
            "cache_status",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticker", sa.String(20), nullable=False, unique=True),
            sa.Column("earliest_date", sa.Date()),
            sa.Column("latest_date", sa.Date()),
            sa.Column("total_records", sa.Integer(), server_default="0"),
            sa.Column("last_sync_at", sa.DateTime()),
            sa.Column("last_sync_success", sa.Boolean(), server_default="true"),
            sa.Column("last_sync_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    """
    Downgrade from 001 would destroy all data.
    This is intentionally a no-op — manual intervention required.
    """
    raise NotImplementedError(
        "Downgrade from 001 (initial schema) is not supported. "
        "Manual database intervention required."
    )
