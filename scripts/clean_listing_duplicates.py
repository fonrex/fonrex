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

With ``--move-prices``, the bars of a duplicate go to the kept listing before the
duplicate is deleted: the bars whose (resolution, time) the kept listing does not
have are moved, the others (a second copy of the same session) are dropped. When
the kept listing has prices of its own, the two series must agree on the sessions
they share (close within 1 %); otherwise, or when they share no session, nothing
is moved and the duplicate is reported. The weekly and monthly aggregates are
refreshed on the moved range once the change is committed.

Usage
─────
    # Report what would be deleted (no writes)
    python scripts/clean_listing_duplicates.py --dry-run

    # Delete the duplicates without prices
    python scripts/clean_listing_duplicates.py

    # Also move the prices of the duplicates that hold some, then delete them
    python scripts/clean_listing_duplicates.py --move-prices --dry-run

    # Specific ISIN
    python scripts/clean_listing_duplicates.py --isin US0378331005 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Allow running from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import exists, text
from sqlalchemy.orm import aliased

from database.service import DatabaseService
from models import Asset, AssetListing, PriceEOD

logger = logging.getLogger("clean_listing_duplicates")

# Two copies of the same session agree when their closes differ by at most this
# share of the close of the kept listing.
CLOSE_TOLERANCE = 0.01

# Continuous aggregates built on the daily bars of prices_eod, with their bucket.
PRICE_AGGREGATES = (("prices_weekly", "1 week"), ("prices_monthly", "1 month"))


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


@dataclass(frozen=True)
class PriceMove:
    """Bars of a duplicate handed to the kept listing."""

    duplicate_id: int
    kept_id: int
    moved: int
    dropped: int
    first: datetime | None
    last: datetime | None


def _holds_prices(session, listing_id: int) -> bool:
    return bool(session.query(exists().where(PriceEOD.asset_listing_id == listing_id)).scalar())


def _count_bars(session, listing_id: int) -> int:
    return session.query(PriceEOD).filter(PriceEOD.asset_listing_id == listing_id).count()


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


def _series_agree(session, duplicate_id: int, kept_id: int) -> bool:
    """True when the kept listing has no bar, or when the two series share sessions
    and agree on every one of them."""
    if not _holds_prices(session, kept_id):
        return True
    shared, differ = session.execute(
        text("""
            SELECT count(*),
                   coalesce(sum(CASE
                       WHEN d.close IS NOT NULL AND k.close IS NOT NULL
                        AND abs(d.close - k.close) > :tolerance * abs(k.close)
                       THEN 1 ELSE 0 END), 0)
            FROM prices_eod d
            JOIN prices_eod k
              ON k.asset_listing_id = :kept
             AND k.resolution = d.resolution
             AND k.time = d.time
            WHERE d.asset_listing_id = :duplicate
        """),
        {"duplicate": duplicate_id, "kept": kept_id, "tolerance": CLOSE_TOLERANCE},
    ).one()
    return shared > 0 and differ == 0


def _move_prices(session, duplicate_id: int, kept_id: int) -> PriceMove:
    """Copy the bars of the duplicate that the kept listing lacks, then delete them all.

    The bars are inserted under the kept listing rather than updated in place: the
    listing is part of the key and of the compression segments of the hypertable.
    """
    first, last = session.execute(
        text("SELECT min(time), max(time) FROM prices_eod WHERE asset_listing_id = :duplicate"),
        {"duplicate": duplicate_id},
    ).one()
    total = _count_bars(session, duplicate_id)
    kept_before = _count_bars(session, kept_id)

    copied = [c.name for c in PriceEOD.__table__.columns if c.name != "asset_listing_id"]
    columns = ", ".join(copied)
    session.execute(
        text(f"""
            INSERT INTO prices_eod (asset_listing_id, {columns})
            SELECT :kept, {columns} FROM prices_eod WHERE asset_listing_id = :duplicate
            ON CONFLICT (asset_listing_id, resolution, time) DO NOTHING
        """),
        {"duplicate": duplicate_id, "kept": kept_id},
    )
    session.execute(
        text("DELETE FROM prices_eod WHERE asset_listing_id = :duplicate"),
        {"duplicate": duplicate_id},
    )
    # PriceEOD objects of the duplicate still in the session are gone from the table.
    session.expire_all()
    moved = _count_bars(session, kept_id) - kept_before
    return PriceMove(
        duplicate_id=duplicate_id,
        kept_id=kept_id,
        moved=moved,
        dropped=total - moved,
        first=first,
        last=last,
    )


def refresh_price_aggregates(engine, moves: list[PriceMove]) -> bool:
    """Refresh the weekly and monthly aggregates on the range of the moved bars.

    A refresh cannot run inside a transaction, so it runs after the commit, on its
    own connection. Without it the aggregates still show the duplicate until their
    daily job. Returns False when there was nothing to refresh or it could not run.
    """
    starts = [move.first for move in moves if move.moved and move.first is not None]
    ends = [move.last for move in moves if move.moved and move.last is not None]
    if not starts or engine.dialect.name != "postgresql":
        return False
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for view_name, period in PRICE_AGGREGATES:
                present = connection.execute(
                    text("""
                        SELECT 1 FROM timescaledb_information.continuous_aggregates
                        WHERE view_name = :view_name
                    """),
                    {"view_name": view_name},
                ).first()
                if not present:
                    continue
                # A refresh only covers the buckets that fit whole in its window.
                connection.execute(
                    text(f"""
                        CALL refresh_continuous_aggregate(
                            '{view_name}',
                            CAST(:start AS timestamptz) - INTERVAL '{period}',
                            CAST(:end AS timestamptz) + INTERVAL '{period}'
                        )
                    """),
                    {"start": min(starts), "end": max(ends)},
                )
    except Exception as exc:  # the cleanup is committed; the daily job will catch up
        logger.warning("Aggregates not refreshed (their daily job will do it): %s", exc)
        return False
    return True


def clean_duplicates(
    session,
    duplicates: list[ListingDuplicate],
    dry_run: bool = True,
    move_prices: bool = False,
) -> dict:
    """
    Delete the duplicates without prices; their mappings go with them. With
    ``move_prices``, the bars of a duplicate go to the kept listing first.
    Returns the number of listings deleted, of primaries moved to the kept listing,
    the ids of the duplicates left alone because they hold prices (and, among them,
    those whose series disagrees with the kept one), and the bars moved and dropped.
    """
    deleted = primaries_moved = 0
    held_prices = []
    series_differ = []
    price_moves = []
    for duplicate in duplicates:
        if duplicate.has_prices and not move_prices:
            held_prices.append(duplicate.duplicate_id)
            continue
        if move_prices:
            # The kept listing receives bars: locked first, like the duplicate below,
            # so that no ingestion writes either series while they are compared.
            session.query(AssetListing).filter_by(id=duplicate.kept_id).with_for_update().one()
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
            if not move_prices:
                held_prices.append(duplicate.duplicate_id)
                continue
            if not _series_agree(session, listing.id, duplicate.kept_id):
                held_prices.append(duplicate.duplicate_id)
                series_differ.append(duplicate.duplicate_id)
                continue
            price_moves.append(_move_prices(session, listing.id, duplicate.kept_id))
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
        refresh_price_aggregates(session.get_bind(), price_moves)

    return {
        "listings_deleted": deleted,
        "primaries_moved": primaries_moved,
        "held_prices": held_prices,
        "series_differ": series_differ,
        "price_moves": price_moves,
    }


def print_report(
    duplicates: list[ListingDuplicate], result: dict, dry_run: bool, move_prices: bool = False
) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    kept_with_prices = [d for d in duplicates if d.duplicate_id in result["held_prices"]]
    moves = result["price_moves"]
    print()
    print("=" * 60)
    print(f"  {prefix}Listing Duplicates Cleanup")
    print("=" * 60)
    print(f"  Duplicate listings found     : {len(duplicates)}")
    print(f"  Duplicate listings removed   : {result['listings_deleted']}")
    print(f"  Primaries moved to kept one  : {result['primaries_moved']}")
    if move_prices:
        print(f"  Bars moved to kept listing   : {sum(move.moved for move in moves)}")
        print(f"  Bars dropped (already there) : {sum(move.dropped for move in moves)}")
        for move in moves:
            print(
                f"    listing {move.duplicate_id} -> listing {move.kept_id}: "
                f"{move.moved} moved, {move.dropped} dropped"
            )
    print(f"  Left alone (they hold prices): {len(kept_with_prices)}")
    for duplicate in kept_with_prices:
        reason = ""
        if duplicate.duplicate_id in result["series_differ"]:
            reason = " — series differ or share no session, nothing moved"
        print(
            f"    listing {duplicate.duplicate_id} ({duplicate.isin} {duplicate.ticker} "
            f"{duplicate.currency}) duplicates listing {duplicate.kept_id} "
            f"(exchange {duplicate.kept_exchange or '-'}, "
            f"{'with' if duplicate.kept_has_prices else 'without'} prices){reason}"
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
        "--move-prices",
        action="store_true",
        default=False,
        help=(
            "Move the bars of a duplicate to the kept listing, then delete it "
            "(only when the two series agree) (default: False)"
        ),
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
        result = clean_duplicates(
            session, duplicates, dry_run=args.dry_run, move_prices=args.move_prices
        )
        print_report(duplicates, result, dry_run=args.dry_run, move_prices=args.move_prices)
    except Exception as exc:
        session.rollback()
        logger.error("Cleanup failed: %s", exc)
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
