"""Migration 011 — Provider health monitoring tables

Revision ID: 011
Revises: 010
Create Date: 2026-07-13

Tables created:
    - provider_health_log    : log of each health check (TimescaleDB hypertable)
    - provider_health_daily  : daily aggregate by provider
    - provider_alerts        : active alerts
"""

import sqlalchemy as sa

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── Table 1: provider_health_log ────────────────────────────────────────
    # One row per value check per provider.
    # Composite primary key (id, checked_at) for TimescaleDB hypertable
    # compatibility (the partitioning column must be in the PK).
    op.create_table(
        "provider_health_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider_name", sa.String(50), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("field", sa.String(50), nullable=False),  # "pe_ratio", "price"...
        sa.Column("value_received", sa.Numeric(20, 6), nullable=True),
        sa.Column("value_expected_min", sa.Numeric(20, 6), nullable=True),
        sa.Column("value_expected_max", sa.Numeric(20, 6), nullable=True),
        sa.Column("consensus_value", sa.Numeric(20, 6), nullable=True),
        sa.Column("deviation_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        # "ok" | "outlier" | "out_of_range" | "null" | "timeout"
        sa.Column("check_type", sa.String(20), nullable=False),
        # "canary" | "realtime" | "consensus"
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", "checked_at"),
    )
    op.create_index(
        "ix_phl_provider_checked", "provider_health_log", ["provider_name", "checked_at"]
    )
    op.create_index("ix_phl_status", "provider_health_log", ["status"])

    # Convert to TimescaleDB hypertable and apply 30-day retention
    op.execute("""
        DO $$
        BEGIN
            PERFORM create_hypertable(
                'provider_health_log', 'checked_at',
                if_not_exists => TRUE
            );
            PERFORM add_retention_policy(
                'provider_health_log', INTERVAL '30 days',
                if_not_exists => TRUE
            );
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END $$;
    """)

    # ── Table 2: provider_health_daily ──────────────────────────────────────
    # Daily aggregate by provider — basis of reliability statistics
    op.create_table(
        "provider_health_daily",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider_name", sa.String(50), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("checks_total", sa.Integer(), default=0),
        sa.Column("checks_ok", sa.Integer(), default=0),
        sa.Column("checks_outlier", sa.Integer(), default=0),
        sa.Column("checks_null", sa.Integer(), default=0),
        sa.Column("checks_timeout", sa.Integer(), default=0),
        sa.Column("success_rate", sa.Numeric(5, 4), nullable=True),  # 0.0 to 1.0
        sa.Column("avg_latency_ms", sa.Integer(), nullable=True),
        sa.Column("canary_passed", sa.Boolean(), nullable=True),
        sa.Column("is_healthy", sa.Boolean(), default=True),
        sa.UniqueConstraint("provider_name", "date", name="uq_provider_health_daily"),
    )
    op.create_index("ix_phd_provider_date", "provider_health_daily", ["provider_name", "date"])

    # ── Table 3: provider_alerts ─────────────────────────────────────────────
    # Active alerts — one row per unresolved alert
    op.create_table(
        "provider_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider_name", sa.String(50), nullable=False),
        sa.Column("alert_type", sa.String(50), nullable=False),
        # "canary_failed" | "high_outlier_rate" | "consecutive_nulls" | "latency_spike"
        sa.Column("severity", sa.String(10), nullable=False),
        # "critical" | "warning" | "info"
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("field", sa.String(50), nullable=True),
        sa.Column("value_received", sa.Numeric(20, 6), nullable=True),
        sa.Column("value_expected", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), default=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_pa_provider_active", "provider_alerts", ["provider_name", "is_resolved"])
    op.create_index("ix_pa_severity", "provider_alerts", ["severity", "is_resolved"])


def downgrade() -> None:
    op.drop_index("ix_pa_severity")
    op.drop_index("ix_pa_provider_active")
    op.drop_table("provider_alerts")

    op.drop_index("ix_phd_provider_date")
    op.drop_table("provider_health_daily")

    op.drop_index("ix_phl_status")
    op.drop_index("ix_phl_provider_checked")
    op.drop_table("provider_health_log")
