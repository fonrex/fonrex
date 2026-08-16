"""Make Alembic authoritative for TimescaleDB schema objects.

Revision ID: 012
Revises: 011
Create Date: 2026-07-18
"""

import sqlalchemy as sa

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    columns = {column["name"] for column in sa.inspect(conn).get_columns("prices_eod")}
    if "time" not in columns and "timestamp" in columns:
        op.alter_column("prices_eod", "timestamp", new_column_name="time")
        columns.remove("timestamp")
        columns.add("time")
    if "asset_listing_id" not in columns:
        op.add_column("prices_eod", sa.Column("asset_listing_id", sa.Integer(), nullable=True))

    foreign_keys = sa.inspect(conn).get_foreign_keys("prices_eod")
    if not any(fk.get("constrained_columns") == ["asset_listing_id"] for fk in foreign_keys):
        op.create_foreign_key(
            "fk_prices_eod_asset_listing_id",
            "prices_eod",
            "asset_listings",
            ["asset_listing_id"],
            ["id"],
        )

    op.execute(
        "SELECT create_hypertable('prices_eod', 'time', if_not_exists => TRUE, migrate_data => TRUE)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_prices_listing_timestamp ON prices_eod (asset_listing_id, time DESC)"
    )

    op.execute("""
        ALTER TABLE prices_eod SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'asset_id'
        )
    """)
    op.execute("""
        DO $migration$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_compression' AND hypertable_name = 'prices_eod'
            ) THEN
                PERFORM add_compression_policy('prices_eod', INTERVAL '14 days');
            END IF;
        END $migration$
    """)

    for period, view_name in (("1 week", "prices_weekly"), ("1 month", "prices_monthly")):
        op.execute(f"""
            DO $migration$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM timescaledb_information.continuous_aggregates
                    WHERE view_name = '{view_name}'
                ) THEN
                    EXECUTE $view$
                        CREATE MATERIALIZED VIEW {view_name}
                        WITH (timescaledb.continuous) AS
                        SELECT time_bucket('{period}', time) AS bucket,
                               asset_id,
                               first(open, time) AS open,
                               max(high) AS high,
                               min(low) AS low,
                               last(close, time) AS close,
                               sum(volume) AS volume
                        FROM prices_eod
                        GROUP BY bucket, asset_id
                        WITH NO DATA
                    $view$;
                END IF;
            END $migration$
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP MATERIALIZED VIEW IF EXISTS prices_monthly")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS prices_weekly")
    op.execute("SELECT remove_compression_policy('prices_eod', if_exists => TRUE)")
