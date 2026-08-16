#!/usr/bin/env python3
"""
scripts/clean_isin_duplicates.py
─────────────────────────────────
Diagnostic and cleanup tool for ISIN duplicates in the assets table.

Usage
─────
    # Audit only (no writes)
    python scripts/clean_isin_duplicates.py --dry-run

    # Remove duplicates + create unique index
    python scripts/clean_isin_duplicates.py --create-index

    # Specific ISIN
    python scripts/clean_isin_duplicates.py --isin US0378331005 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from database.service import DatabaseService

logger = logging.getLogger("clean_isin_duplicates")


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic
# ─────────────────────────────────────────────────────────────────────────────


def diagnose_duplicates(session, isin_filter: str | None = None) -> dict:
    """
    Return a dict with diagnostic counts and sample duplicate groups.

    Keys
    ────
        total_assets        – total rows in assets
        total_isin          – rows where isin IS NOT NULL
        duplicate_isins     – ISINs that appear on more than one asset row
        affected_assets     – sum of all duplicated-ISIN asset rows
        sample_groups       – list of dicts [{isin, count, asset_ids}, …] (up to 10)
        orphaned_listings   – asset_listings referencing a non-canonical asset
        orphaned_mappings   – asset_mappings referencing a non-canonical listing
    """
    where_isin = "AND a.isin = :isin" if isin_filter else ""
    params: dict = {"isin": isin_filter} if isin_filter else {}

    # Basic counts
    total_assets = session.execute(text("SELECT COUNT(*) FROM assets")).scalar()
    total_isin = session.execute(
        text("SELECT COUNT(*) FROM assets WHERE isin IS NOT NULL")
    ).scalar()

    dup_isin_count: int = session.execute(
        text(f"""
            SELECT COUNT(DISTINCT isin)
            FROM (
                SELECT isin
                FROM assets
                WHERE isin IS NOT NULL {where_isin}
                GROUP BY isin
                HAVING COUNT(*) > 1
            ) sub
        """),
        params,
    ).scalar()

    affected_assets: int = session.execute(
        text(f"""
            SELECT COALESCE(SUM(cnt), 0)
            FROM (
                SELECT COUNT(*) AS cnt
                FROM assets
                WHERE isin IS NOT NULL {where_isin}
                GROUP BY isin
                HAVING COUNT(*) > 1
            ) sub
        """),
        params,
    ).scalar()

    # Sample groups (up to 10)
    rows = session.execute(
        text(f"""
            SELECT a.isin, COUNT(*) AS cnt, ARRAY_AGG(a.id ORDER BY a.id) AS asset_ids
            FROM assets a
            WHERE a.isin IS NOT NULL {where_isin}
            GROUP BY a.isin
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC, a.isin
            LIMIT 10
        """),
        params,
    ).fetchall()

    sample_groups = [{"isin": r[0], "count": r[1], "asset_ids": r[2]} for r in rows]

    # Orphaned listings (point to non-min(id) asset for the same isin)
    orphaned_listings: int = session.execute(
        text("""
            SELECT COUNT(*)
            FROM asset_listings al
            JOIN assets a ON al.asset_id = a.id
            WHERE a.isin IS NOT NULL
              AND a.id > (
                  SELECT MIN(a2.id) FROM assets a2 WHERE a2.isin = a.isin
              )
        """)
    ).scalar()

    # Orphaned mappings (point to listings on non-canonical assets)
    orphaned_mappings: int = session.execute(
        text("""
            SELECT COUNT(*)
            FROM asset_mappings am
            JOIN asset_listings al ON am.asset_listing_id = al.id
            JOIN assets a ON al.asset_id = a.id
            WHERE a.isin IS NOT NULL
              AND a.id > (
                  SELECT MIN(a2.id) FROM assets a2 WHERE a2.isin = a.isin
              )
        """)
    ).scalar()

    return {
        "total_assets": total_assets,
        "total_isin": total_isin,
        "duplicate_isins": dup_isin_count,
        "affected_assets": affected_assets,
        "sample_groups": sample_groups,
        "orphaned_listings": orphaned_listings,
        "orphaned_mappings": orphaned_mappings,
    }


def print_diagnostic_report(report: dict) -> None:
    print()
    print("=" * 60)
    print("  ISIN Duplicate Diagnostic Report")
    print("=" * 60)
    print(f"  Total assets             : {report['total_assets']}")
    print(f"  Assets with ISIN         : {report['total_isin']}")
    print(f"  Duplicate ISINs          : {report['duplicate_isins']}")
    print(f"  Affected asset rows      : {report['affected_assets']}")
    print(f"  Orphaned listings        : {report['orphaned_listings']}")
    print(f"  Orphaned mappings        : {report['orphaned_mappings']}")

    if report["sample_groups"]:
        print()
        print("  Top duplicate groups (ISIN → count → IDs):")
        for g in report["sample_groups"]:
            ids_preview = ", ".join(str(i) for i in g["asset_ids"][:6])
            if len(g["asset_ids"]) > 6:
                ids_preview += f", … (+{len(g['asset_ids']) - 6} more)"
            print(f"    {g['isin']}  ×{g['count']:>3}  [{ids_preview}]")
    else:
        print()
        print("  ✅ No duplicate ISINs found.")

    print("=" * 60)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────


def clean_duplicates(
    session,
    dry_run: bool = True,
    isin_filter: str | None = None,
) -> dict:
    """
    Re-parent orphaned listings/mappings to the canonical asset (MIN id per ISIN),
    then delete the now-empty duplicate assets.

    Returns a dict with the number of affected rows per step.
    """
    where_isin = "AND a.isin = :isin" if isin_filter else ""
    params: dict = {"isin": isin_filter} if isin_filter else {}

    # ── Step 1: re-parent asset_listings ────────────────────────────────────
    reparent_listings = session.execute(
        text(f"""
            WITH canonical AS (
                SELECT isin, MIN(id) AS canonical_id
                FROM assets
                WHERE isin IS NOT NULL {where_isin}
                GROUP BY isin
                HAVING COUNT(*) > 1
            )
            UPDATE asset_listings al
            SET asset_id = c.canonical_id
            FROM assets a
            JOIN canonical c ON a.isin = c.isin
            WHERE al.asset_id = a.id
              AND a.id <> c.canonical_id
        """),
        params,
    )

    # ── Step 2: fix asset_mappings that became duplicate after re-parenting ─
    # Remove duplicate (asset_listing_id, provider_name) keeping lowest id
    del_dup_mappings = session.execute(
        text("""
            DELETE FROM asset_mappings
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM asset_mappings
                GROUP BY asset_listing_id, provider_name
            )
        """)
    )

    # ── Step 3: fix asset_listings that became duplicate after re-parenting ─
    del_dup_listings = session.execute(
        text("""
            DELETE FROM asset_listings
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM asset_listings
                GROUP BY asset_id, ticker, exchange, currency
            )
        """)
    )

    # ── Step 4: delete now-empty duplicate assets ────────────────────────────
    del_dup_assets = session.execute(
        text(f"""
            WITH canonical AS (
                SELECT isin, MIN(id) AS canonical_id
                FROM assets
                WHERE isin IS NOT NULL {where_isin}
                GROUP BY isin
                HAVING COUNT(*) > 1
            )
            DELETE FROM assets a
            USING canonical c
            WHERE a.isin = c.isin
              AND a.id <> c.canonical_id
        """),
        params,
    )

    result = {
        "listings_reparented": reparent_listings.rowcount,
        "mappings_deleted": del_dup_mappings.rowcount,
        "listings_deleted": del_dup_listings.rowcount,
        "assets_deleted": del_dup_assets.rowcount,
    }

    if dry_run:
        session.rollback()
        logger.info("[DRY-RUN] changes rolled back — no data was modified")
    else:
        session.commit()
        logger.info("Changes committed")

    return result


def print_cleanup_report(result: dict, dry_run: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    print()
    print(f"  {prefix}Cleanup Results")
    print("  " + "-" * 40)
    print(f"  Listings re-parented     : {result['listings_reparented']}")
    print(f"  Duplicate mappings removed: {result['mappings_deleted']}")
    print(f"  Duplicate listings removed: {result['listings_deleted']}")
    print(f"  Duplicate assets deleted  : {result['assets_deleted']}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Index creation
# ─────────────────────────────────────────────────────────────────────────────


def create_unique_index(session) -> None:
    """Create the partial unique index on assets.isin (WHERE isin IS NOT NULL)."""
    session.execute(
        text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE tablename = 'assets'
                      AND indexname  = 'uq_assets_isin_not_null'
                ) THEN
                    CREATE UNIQUE INDEX uq_assets_isin_not_null
                        ON assets (isin)
                        WHERE isin IS NOT NULL;
                    RAISE NOTICE 'Index uq_assets_isin_not_null created';
                ELSE
                    RAISE NOTICE 'Index uq_assets_isin_not_null already exists — skipped';
                END IF;
            END $$;
        """)
    )
    session.commit()
    logger.info("Unique index uq_assets_isin_not_null ensured")


def create_listing_constraint(session) -> None:
    """Add unique constraint uq_asset_listing_identity if it doesn't exist."""
    session.execute(
        text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_asset_listing_identity'
                ) THEN
                    ALTER TABLE asset_listings
                        ADD CONSTRAINT uq_asset_listing_identity
                        UNIQUE (asset_id, ticker, exchange, currency);
                    RAISE NOTICE 'Constraint uq_asset_listing_identity created';
                ELSE
                    RAISE NOTICE 'Constraint uq_asset_listing_identity already exists — skipped';
                END IF;
            END $$;
        """)
    )
    session.commit()
    logger.info("Unique constraint uq_asset_listing_identity ensured")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnose and clean ISIN duplicates in the assets table",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run cleanup steps but roll back — no data is written (default: False)",
    )
    parser.add_argument(
        "--create-index",
        action="store_true",
        default=False,
        help="After cleanup, create unique index + listing constraint in PostgreSQL",
    )
    parser.add_argument(
        "--isin",
        metavar="ISIN",
        help="Restrict diagnosis/cleanup to this ISIN only",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG logging",
    )
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        default=False,
        help="Print diagnostic report only; skip cleanup step",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s — %(message)s",
    )

    db_service = DatabaseService()
    session = db_service.get_session()

    try:
        # ── Diagnose ────────────────────────────────────────────────────────
        logger.info("Running diagnostic…")
        report = diagnose_duplicates(session, isin_filter=args.isin)
        print_diagnostic_report(report)

        if args.diagnose_only:
            return

        if report["duplicate_isins"] == 0:
            logger.info("No duplicates found — nothing to clean.")
        else:
            # ── Clean ───────────────────────────────────────────────────────
            if args.dry_run:
                logger.info("DRY-RUN mode: simulating cleanup (no writes)")
            else:
                logger.warning(
                    "LIVE mode: %d duplicate ISINs will be cleaned",
                    report["duplicate_isins"],
                )
            result = clean_duplicates(session, dry_run=args.dry_run, isin_filter=args.isin)
            print_cleanup_report(result, dry_run=args.dry_run)

        # ── Index creation (PostgreSQL only, skip in dry-run) ───────────────
        if args.create_index:
            if args.dry_run:
                logger.info("[DRY-RUN] --create-index skipped in dry-run mode")
            else:
                logger.info("Creating unique index and listing constraint…")
                create_unique_index(session)
                create_listing_constraint(session)

    except Exception:
        session.rollback()
        logger.exception("Fatal error during cleanup")
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
