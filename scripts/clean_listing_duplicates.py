#!/usr/bin/env python3
"""
scripts/clean_listing_duplicates.py
───────────────────────────────────
Removes the listings that a CSV re-import duplicated after enrichment.

The importer stored a listing without exchange as ``exchange = ""``; the
enrichment then filled that exchange, and the next import of the same CSV did not
find it and created a second listing (``exchange = ""``) with its own mappings.
The importer now matches a row without exchange on (asset, ticker, currency).

A duplicate is a listing with an empty exchange when an older listing of the same
instrument, ticker and currency exists: the importer will never reach it again.
The oldest listing of the group is kept. A duplicate that holds prices is reported
and left alone (``prices_eod`` does not cascade); the others are deleted with
their ``asset_mappings`` rows. Prices are checked again right before each delete,
under a row lock, so an ingestion running at the same time cannot make the run
fail. When a deleted duplicate was the primary listing, the kept listing becomes
primary.

Usage
─────
    # Report what would be deleted (no writes)
    python scripts/clean_listing_duplicates.py --dry-run

    # Delete the duplicates without prices
    python scripts/clean_listing_duplicates.py

    # Specific ISIN
    python scripts/clean_listing_duplicates.py --isin US0378331005 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import exists
from sqlalchemy.orm import aliased

from database.service import DatabaseService
from models import Asset, AssetListing, PriceEOD

logger = logging.getLogger("clean_listing_duplicates")


@dataclass(frozen=True)
class ListingDuplicate:
    isin: str | None
    ticker: str
    currency: str
    duplicate_id: int
    kept_id: int
    kept_exchange: str
    kept_has_prices: bool
    is_primary: bool
    has_prices: bool


def _holds_prices(session, listing_id: int) -> bool:
    return bool(session.query(exists().where(PriceEOD.asset_listing_id == listing_id)).scalar())


def find_duplicates(session, isin_filter: str | None = None) -> list[ListingDuplicate]:
    """Listings with an empty exchange that an older listing of their group makes redundant."""
    older = aliased(AssetListing)
    query = (
        session.query(AssetListing, Asset.isin)
        .join(Asset, Asset.id == AssetListing.asset_id)
        .filter(
            AssetListing.exchange == "",
            exists().where(
                older.asset_id == AssetListing.asset_id,
                older.ticker == AssetListing.ticker,
                older.currency == AssetListing.currency,
                older.id < AssetListing.id,
            ),
        )
        .order_by(AssetListing.id)
    )
    if isin_filter:
        query = query.filter(Asset.isin == isin_filter)

    duplicates = []
    for listing, isin in query.all():
        kept = (
            session.query(AssetListing)
            .filter_by(asset_id=listing.asset_id, ticker=listing.ticker, currency=listing.currency)
            .order_by(AssetListing.id)
            .first()
        )
        duplicates.append(
            ListingDuplicate(
                isin=isin,
                ticker=listing.ticker,
                currency=listing.currency,
                duplicate_id=listing.id,
                kept_id=kept.id,
                kept_exchange=kept.exchange,
                kept_has_prices=_holds_prices(session, kept.id),
                is_primary=listing.is_primary,
                has_prices=_holds_prices(session, listing.id),
            )
        )
    return duplicates


def clean_duplicates(session, duplicates: list[ListingDuplicate], dry_run: bool = True) -> dict:
    """
    Delete the duplicates without prices; their mappings go with them.
    Returns the number of listings deleted, of primaries moved to the kept listing,
    and the ids of the duplicates left alone because they hold prices.
    """
    deleted = primaries_moved = 0
    held_prices = []
    for duplicate in duplicates:
        if duplicate.has_prices:
            held_prices.append(duplicate.duplicate_id)
            continue
        # The lock makes an ingestion that writes a price for this listing wait for
        # the end of the run, and the check sees the prices written since
        # find_duplicates: such a listing is skipped instead of failing the delete.
        listing = (
            session.query(AssetListing)
            .filter_by(id=duplicate.duplicate_id)
            .with_for_update()
            .one()
        )
        if _holds_prices(session, listing.id):
            held_prices.append(duplicate.duplicate_id)
            continue
        session.delete(listing)
        # The delete reaches the database before the kept listing becomes primary:
        # an instrument may hold a single primary listing.
        session.flush()
        deleted += 1
        if duplicate.is_primary:
            session.get(AssetListing, duplicate.kept_id).is_primary = True
            session.flush()
            primaries_moved += 1

    if dry_run:
        session.rollback()
        logger.info("[DRY-RUN] changes rolled back — no data was modified")
    else:
        session.commit()
        logger.info("Changes committed")

    return {
        "listings_deleted": deleted,
        "primaries_moved": primaries_moved,
        "held_prices": held_prices,
    }


def print_report(duplicates: list[ListingDuplicate], result: dict, dry_run: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    kept_with_prices = [d for d in duplicates if d.duplicate_id in result["held_prices"]]
    print()
    print("=" * 60)
    print(f"  {prefix}Listing Duplicates Cleanup")
    print("=" * 60)
    print(f"  Duplicate listings found     : {len(duplicates)}")
    print(f"  Duplicate listings removed   : {result['listings_deleted']}")
    print(f"  Primaries moved to kept one  : {result['primaries_moved']}")
    print(f"  Left alone (they hold prices): {len(kept_with_prices)}")
    for duplicate in kept_with_prices:
        print(
            f"    listing {duplicate.duplicate_id} ({duplicate.isin} {duplicate.ticker} "
            f"{duplicate.currency}) duplicates listing {duplicate.kept_id} "
            f"(exchange {duplicate.kept_exchange or '-'}, "
            f"{'with' if duplicate.kept_has_prices else 'without'} prices)"
        )
    if kept_with_prices:
        print("  See ARCHITECTURE.md (Listing Duplicates Cleanup) for what to do with them.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove the listings duplicated by a CSV re-import after enrichment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run the cleanup but roll back — no data is written (default: False)",
    )
    parser.add_argument(
        "--isin",
        metavar="ISIN",
        help="Restrict the cleanup to this ISIN only",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s — %(message)s",
    )

    session = DatabaseService().get_session()
    try:
        duplicates = find_duplicates(session, isin_filter=args.isin)
        result = clean_duplicates(session, duplicates, dry_run=args.dry_run)
        print_report(duplicates, result, dry_run=args.dry_run)
    except Exception as exc:
        session.rollback()
        logger.error("Cleanup failed: %s", exc)
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
