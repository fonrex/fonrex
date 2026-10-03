"""Identify a price bar by its listing, its resolution and its session date.

Revision ID: 014
Revises: 013
Create Date: 2026-10-04

Before this revision a row of ``prices_eod`` was identified by ``(time, asset_id)``:

* the listings of one instrument (the same ETF quoted in EUR and in USD) shared
  one series: their bars overwrote or interleaved each other;
* a weekly or monthly bar replaced the daily bar stored at the same instant;
* ``time`` was the UTC instant of midnight on the exchange, so a session in
  Paris or Tokyo was dated the day before.

After it, a row is identified by ``(asset_listing_id, resolution, time)`` and
``time`` is the date of the trading session, stored as midnight UTC.

The table is rebuilt rather than altered in place: a hypertable with compressed
chunks and continuous aggregates does not allow its key to be changed. Existing
rows are copied, re-dated and merged; nothing has to be downloaded again.

Re-dating existing rows. The old instants are of two kinds, told apart by their
time of day in UTC:

* midnight on the exchange (yfinance): before 11:00 for the Americas (same UTC
  date), from 11:00 onwards for Europe, Asia and Oceania (the session is the
  next UTC date);
* opening of the session (TradingView fallback): the UTC date is the session
  date, including the North-American openings at 13:30 / 14:30.

A TradingView bar of an exchange opening on the hour between 13:00 and 15:59 UTC
(São Paulo, Buenos Aires, Lima) cannot be told apart from an Australian midnight
and is dated one day late. A forced refresh of the ticker
(``POST /historical/ingest`` with ``force_refresh``) replaces the series and
settles any doubt.
"""

import logging

import sqlalchemy as sa

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

COLUMNS = "open, high, low, close, adj_close, volume, adjusted, source"

# Session date (as midnight UTC) of a row stored at an exchange-local instant.
# `t` is the old instant read in UTC, `m` its minute of the day.
SESSION_TIME = """
    (CASE
        WHEN m >= 660 AND NOT (m >= 750 AND m < 960 AND m % 60 = 30)
            THEN date_trunc('day', t) + INTERVAL '1 day'
        ELSE date_trunc('day', t)
    END) AT TIME ZONE 'UTC'
"""

# Listing that stands for an instrument when a row names none.
PREFERRED_LISTING = """
    (SELECT l.id FROM asset_listings l
     WHERE l.asset_id = p.asset_id
     ORDER BY l.is_primary DESC, l.is_active DESC, l.id
     LIMIT 1)
"""


def _count(conn: sa.Connection, table: str) -> int:
    return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() or 0


# Background jobs of TimescaleDB that work on the price tables: compression of
# ``prices_eod`` and refresh of its weekly and monthly aggregates. The jobs
# view names an aggregate or, in older versions, its materialisation hypertable.
PRICE_JOBS = """
    SELECT j.job_id
    FROM timescaledb_information.jobs j
    WHERE j.proc_name IN ('policy_compression', 'policy_refresh_continuous_aggregate')
      AND (j.hypertable_name = 'prices_eod'
           OR j.hypertable_name IN (
               SELECT view_name FROM timescaledb_information.continuous_aggregates
               WHERE hypertable_name = 'prices_eod'
               UNION
               SELECT materialization_hypertable_name
               FROM timescaledb_information.continuous_aggregates
               WHERE hypertable_name = 'prices_eod'))
    ORDER BY j.job_id
"""


def _take_price_tables(conn: sa.Connection) -> None:
    """Stop the background jobs of the price tables, then lock the tables.

    TimescaleDB runs its jobs in other sessions, at any time, and a policy
    created by a migration runs at once. Two deadlocks were seen between such a
    job and this migration (CI: a downgrade followed by an upgrade):

    * the job held the hypertable while the migration held a chunk it had read;
    * dropping ``prices_eod`` deletes its job, which waits for that job to end,
      while the job waited for a table the migration held.

    Deleting a job waits for its run to end. Done before any table is held, the
    job is waited for and never waits for the migration. The jobs are created
    again with the tables. The tables are then locked before they are read,
    ``prices_eod`` (and its chunks) first, as a refresh reads it before writing
    its aggregate.
    """
    for job_id in conn.execute(sa.text(PRICE_JOBS)).scalars().all():
        conn.execute(sa.text("SELECT delete_job(:job_id)"), {"job_id": job_id})
    op.execute("LOCK TABLE prices_eod IN ACCESS EXCLUSIVE MODE")
    for view_name in ("prices_weekly", "prices_monthly"):
        if conn.execute(sa.text("SELECT to_regclass(:name)"), {"name": view_name}).scalar():
            op.execute(f"LOCK TABLE {view_name} IN ACCESS EXCLUSIVE MODE")


def _drop_aggregates() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS prices_monthly")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS prices_weekly")


def _compress(segment_by: str) -> None:
    op.execute(f"""
        ALTER TABLE prices_eod SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = '{segment_by}'
        )
    """)
    op.execute("SELECT add_compression_policy('prices_eod', INTERVAL '14 days')")


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    _take_price_tables(conn)
    rows_before = _count(conn, "prices_eod")

    # 1. A price series needs a listing. An instrument that has prices but no
    #    listing gets one built from its own ticker, exchange and currency.
    created = conn.execute(
        sa.text("""
            INSERT INTO asset_listings
                (asset_id, ticker, exchange, currency, source, is_primary, is_active,
                 created_at, updated_at)
            SELECT a.id, a.ticker, COALESCE(a.exchange, ''), COALESCE(a.currency, ''),
                   'migration_014', TRUE, COALESCE(a.is_active, TRUE), now(), now()
            FROM assets a
            WHERE EXISTS (SELECT 1 FROM prices_eod p WHERE p.asset_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM asset_listings l WHERE l.asset_id = a.id)
        """)
    ).rowcount

    # 2. The weekly and monthly aggregates read the table: they are rebuilt at the end.
    _drop_aggregates()

    # 3. New table, keyed by listing, resolution and session.
    op.execute("""
        CREATE TABLE prices_eod_new (
            time TIMESTAMPTZ NOT NULL,
            asset_id INTEGER NOT NULL,
            asset_listing_id INTEGER NOT NULL,
            open DOUBLE PRECISION,
            high DOUBLE PRECISION,
            low DOUBLE PRECISION,
            close DOUBLE PRECISION,
            adj_close DOUBLE PRECISION,
            volume BIGINT,
            resolution VARCHAR(3) NOT NULL DEFAULT '1D',
            adjusted BOOLEAN NOT NULL DEFAULT TRUE,
            source VARCHAR(20),
            CONSTRAINT prices_eod_pkey PRIMARY KEY (asset_listing_id, resolution, time),
            CONSTRAINT prices_eod_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES assets (id),
            CONSTRAINT fk_prices_eod_asset_listing_id
                FOREIGN KEY (asset_listing_id) REFERENCES asset_listings (id)
        )
    """)
    op.execute(
        "SELECT create_hypertable('prices_eod_new', 'time', create_default_indexes => FALSE)"
    )

    # 4. Copy: each row goes to its listing (the preferred listing of its
    #    instrument when it names none) and to its session date. Rows that land
    #    on the same listing, resolution and session are merged: the bar with
    #    the largest volume is kept.
    op.execute(f"""
        INSERT INTO prices_eod_new
            (time, asset_id, asset_listing_id, resolution, {COLUMNS})
        SELECT DISTINCT ON (listing_id, resolution, session_time)
               session_time, listing_asset_id, listing_id, resolution, {COLUMNS}
        FROM (
            SELECT located.*, l.asset_id AS listing_asset_id, {SESSION_TIME} AS session_time
            FROM (
                SELECT p.*,
                       COALESCE(p.asset_listing_id, {PREFERRED_LISTING}) AS listing_id,
                       p.time AT TIME ZONE 'UTC' AS t,
                       (EXTRACT(HOUR FROM p.time AT TIME ZONE 'UTC') * 60
                        + EXTRACT(MINUTE FROM p.time AT TIME ZONE 'UTC'))::int AS m
                FROM prices_eod p
            ) located
            JOIN asset_listings l ON l.id = located.listing_id
        ) dated
        ORDER BY listing_id, resolution, session_time, volume DESC NULLS LAST, time DESC
    """)
    rows_after = _count(conn, "prices_eod_new")

    # 5. Swap the tables.
    op.execute("DROP TABLE prices_eod")
    op.execute("ALTER TABLE prices_eod_new RENAME TO prices_eod")
    op.execute("CREATE INDEX idx_prices_asset_timestamp ON prices_eod (asset_id, time DESC)")
    op.execute(
        "CREATE INDEX ix_prices_eod_asset_resolution_time "
        "ON prices_eod (asset_id, resolution, time)"
    )

    # 6. Compression, segmented like the key.
    _compress("asset_listing_id, resolution")

    # 7. Weekly and monthly aggregates of the daily bars, per listing. They
    #    answer from the daily bars for what is not materialised yet, and a
    #    daily job materialises the rest.
    for period, view_name in (("1 week", "prices_weekly"), ("1 month", "prices_monthly")):
        op.execute(f"""
            CREATE MATERIALIZED VIEW {view_name}
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT time_bucket('{period}', time) AS bucket,
                   asset_listing_id,
                   asset_id,
                   first(open, time) AS open,
                   max(high) AS high,
                   min(low) AS low,
                   last(close, time) AS close,
                   sum(volume) AS volume
            FROM prices_eod
            WHERE resolution = '1D'
            GROUP BY bucket, asset_listing_id, asset_id
            WITH NO DATA
        """)
        op.execute(f"""
            SELECT add_continuous_aggregate_policy(
                '{view_name}',
                start_offset => NULL,
                end_offset => INTERVAL '{period}',
                schedule_interval => INTERVAL '1 day'
            )
        """)

    logger.info(
        "prices_eod rebuilt per listing: %d rows read, %d rows written "
        "(%d merged on the same listing, resolution and session), %d listing(s) created",
        rows_before,
        rows_after,
        rows_before - rows_after,
        created,
    )


def downgrade() -> None:
    """Rebuild the former key ``(time, asset_id)``.

    One bar per instrument and instant is kept (daily bars and the primary
    listing first); the session dates are not turned back into instants.
    """
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    _take_price_tables(conn)
    _drop_aggregates()
    op.execute("""
        CREATE TABLE prices_eod_old (
            time TIMESTAMPTZ NOT NULL,
            asset_id INTEGER NOT NULL,
            asset_listing_id INTEGER,
            open DOUBLE PRECISION,
            high DOUBLE PRECISION,
            low DOUBLE PRECISION,
            close DOUBLE PRECISION,
            adj_close DOUBLE PRECISION,
            volume BIGINT,
            resolution VARCHAR(3) NOT NULL DEFAULT '1D',
            adjusted BOOLEAN NOT NULL DEFAULT TRUE,
            source VARCHAR(20),
            CONSTRAINT prices_eod_timestamp_asset_id_key PRIMARY KEY (time, asset_id),
            CONSTRAINT prices_eod_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES assets (id),
            CONSTRAINT fk_prices_eod_asset_listing_id
                FOREIGN KEY (asset_listing_id) REFERENCES asset_listings (id)
        )
    """)
    op.execute("SELECT create_hypertable('prices_eod_old', 'time')")
    op.execute(f"""
        INSERT INTO prices_eod_old (time, asset_id, asset_listing_id, resolution, {COLUMNS})
        SELECT DISTINCT ON (p.time, p.asset_id)
               p.time, p.asset_id, p.asset_listing_id, p.resolution,
               p.open, p.high, p.low, p.close, p.adj_close, p.volume, p.adjusted, p.source
        FROM prices_eod p
        JOIN asset_listings l ON l.id = p.asset_listing_id
        ORDER BY p.time, p.asset_id, (p.resolution = '1D') DESC, l.is_primary DESC, l.id
    """)
    op.execute("DROP TABLE prices_eod")
    op.execute("ALTER TABLE prices_eod_old RENAME TO prices_eod")
    op.execute("ALTER INDEX prices_eod_old_time_idx RENAME TO prices_eod_time_idx")
    op.execute("CREATE INDEX idx_prices_asset_timestamp ON prices_eod (asset_id, time DESC)")
    op.execute(
        "CREATE INDEX idx_prices_listing_timestamp ON prices_eod (asset_listing_id, time DESC)"
    )
    op.execute(
        "CREATE INDEX ix_prices_eod_asset_resolution_time "
        "ON prices_eod (asset_id, resolution, time)"
    )
    _compress("asset_id")
    for period, view_name in (("1 week", "prices_weekly"), ("1 month", "prices_monthly")):
        op.execute(f"""
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
        """)
