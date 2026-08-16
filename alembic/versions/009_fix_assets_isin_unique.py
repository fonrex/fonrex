"""Migration 009 — Cleanup of duplicate assets.isin + unique constraints

Revision ID: 009
Revises: 008
Create Date: 2026-05-24

Description :
    1. Migrate orphan asset_listings to the canonical asset (MIN id per ISIN)
    2. Migrate orphan asset_mappings to the canonical asset
    3. Delete duplicates in asset_listings created after migration
    4. Delete duplicate assets (keep MIN id per ISIN)
    5. Create the partial unique index on assets(isin) WHERE isin IS NOT NULL
       (if absent — idempotent)
    6. Ensure the constraint uq_asset_listing_identity exists

    ⚠️ Perform a backup before this migration:
    pg_dump -Fc fonrex > backup_pre_009.dump
"""

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Step 1: Migrate asset_listings to the canonical asset ───────────────
    # For each ISIN with duplicates in assets, point all listings
    # to the MIN(id) = canonical asset.
    op.execute("""
        UPDATE asset_listings AS al
        SET asset_id = canonical.canonical_id
        FROM (
            SELECT
                isin,
                MIN(id) AS canonical_id
            FROM assets
            WHERE isin IS NOT NULL
            GROUP BY isin
            HAVING COUNT(*) > 1
        ) AS canonical
        JOIN assets AS a ON a.isin = canonical.isin
        WHERE al.asset_id = a.id
          AND a.id <> canonical.canonical_id
    """)

    # ── Step 2: Migrate asset_mappings to the canonical asset ───────────────
    op.execute("""
        UPDATE asset_mappings AS am
        SET asset_id = canonical.canonical_id
        FROM (
            SELECT
                isin,
                MIN(id) AS canonical_id
            FROM assets
            WHERE isin IS NOT NULL
            GROUP BY isin
            HAVING COUNT(*) > 1
        ) AS canonical
        JOIN assets AS a ON a.isin = canonical.isin
        WHERE am.asset_id = a.id
          AND a.id <> canonical.canonical_id
    """)

    # ── Step 3: Deduplication of asset_listings after migration ───────────
    # After migration, some listings may violate the unique constraint
    # (asset_id, ticker, exchange, currency). Keep MIN(id).
    op.execute("""
        DELETE FROM asset_listings
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM asset_listings
            GROUP BY asset_id, ticker, COALESCE(exchange, ''), COALESCE(currency, '')
        )
    """)

    # ── Step 4: Deduplication of asset_mappings after migration ────────────
    # Avoid violations of uq_asset_listing_provider after migration.
    op.execute("""
        DELETE FROM asset_mappings
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM asset_mappings
            GROUP BY COALESCE(asset_listing_id::text, 'NULL'), provider_name
        )
    """)

    # ── Step 5: Delete duplicate assets ───────────────────────────────
    op.execute("""
        DELETE FROM assets
        WHERE isin IS NOT NULL
          AND id NOT IN (
            SELECT MIN(id)
            FROM assets
            WHERE isin IS NOT NULL
            GROUP BY isin
          )
    """)

    # ── Step 6: Partial unique index on assets(isin) ──────────────────────
    # Use IF NOT EXISTS for idempotency.
    # CONCURRENTLY requires being outside a transaction — we use EXECUTE
    # via a separate connection if needed. Here we use the simple form
    # because Alembic already handles the transaction.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'uq_assets_isin_not_null'
            ) THEN
                CREATE UNIQUE INDEX uq_assets_isin_not_null
                ON assets (isin)
                WHERE isin IS NOT NULL;
            END IF;
        END $$;
    """)

    # ── Step 7: Unique constraint on asset_listings ────────────────────────
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_asset_listing_identity'
                  AND conrelid = 'asset_listings'::regclass
            ) THEN
                ALTER TABLE asset_listings
                ADD CONSTRAINT uq_asset_listing_identity
                UNIQUE (asset_id, ticker, exchange, currency);
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_assets_isin_not_null")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_asset_listing_identity'
            ) THEN
                ALTER TABLE asset_listings
                DROP CONSTRAINT uq_asset_listing_identity;
            END IF;
        END $$;
    """)
