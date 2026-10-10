"""Migration 007 — Realtime price tables

Revision ID: 007
Revises: 006
Create Date: 2026-05-22

Changes:
    1. Table prices_intraday — realtime ticks (1min) — TimescaleDB hypertable
    2. Table realtime_subscriptions — tickers streamed by RealtimePriceWorker
"""

import sqlalchemy as sa

from alembic import op

revision = "007"
down_revision = "006"


def upgrade() -> None:

    # ── 1. Table prices_intraday (hypertable TimescaleDB) ────────────────────
    # Stores the 1-minute candles received from the TradingView WebSocket
    op.create_table(
        "prices_intraday",
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("open", sa.Numeric(14, 4), nullable=True),
        sa.Column("high", sa.Numeric(14, 4), nullable=True),
        sa.Column("low", sa.Numeric(14, 4), nullable=True),
        sa.Column("close", sa.Numeric(14, 4), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("resolution", sa.String(5), server_default="1min"),
        sa.Column("source", sa.String(20), server_default="tradingview"),
        sa.PrimaryKeyConstraint("timestamp", "asset_id"),
    )
    op.create_index(
        "ix_prices_intraday_asset_ts",
        "prices_intraday",
        ["asset_id", "timestamp"],
    )

    # Turn it into a TimescaleDB hypertable
    op.execute("""
        SELECT create_hypertable(
            'prices_intraday', 'timestamp',
            chunk_time_interval => INTERVAL '1 day',
            if_not_exists => TRUE
        )
    """)

    # Retention policy: keep 30 days of intraday data only
    op.execute("""
        SELECT add_retention_policy(
            'prices_intraday',
            INTERVAL '30 days',
            if_not_exists => TRUE
        )
    """)

    # ── 2. Table realtime_subscriptions ─────────────────────────────────────
    # Tickers streamed by the RealtimePriceWorker
    op.create_table(
        "realtime_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("tv_exchange", sa.String(20), nullable=False),
        sa.Column("tv_symbol", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("subscribed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tick_count", sa.BigInteger(), server_default="0"),
        sa.UniqueConstraint("asset_id", name="uq_realtime_sub_asset"),
    )
    op.create_index("ix_realtime_subs_active", "realtime_subscriptions", ["is_active"])
    op.create_index("ix_realtime_subs_ticker", "realtime_subscriptions", ["ticker"])


def downgrade() -> None:
    op.drop_index("ix_realtime_subs_ticker", "realtime_subscriptions")
    op.drop_index("ix_realtime_subs_active", "realtime_subscriptions")
    op.drop_table("realtime_subscriptions")
    op.drop_index("ix_prices_intraday_asset_ts", "prices_intraday")
    op.drop_table("prices_intraday")
