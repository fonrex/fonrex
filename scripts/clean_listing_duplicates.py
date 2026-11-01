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
they share (close within 1 %, and a close missing on one side only counts as a
difference; a session without close on both sides is not compared), and enough
sessions must be compared (:data:`MIN_SHARED_SESSIONS`, or every bar of the
shorter series); otherwise nothing is moved and the duplicate is reported. A series of the kept listing that receives bars loses its row of
``price_series_adjustments``: the next ingestion fetches it again in full, on one
adjustment basis. The weekly and monthly aggregates are refreshed on the range
of the duplicate's bars once the change is committed.

The mappings of a deleted duplicate go with it, except a trusted Yahoo symbol
(verified, or set by hand): it goes to the kept listing when that one has none.

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
from historical.yahoo_symbols import SET_BY_HAND, YAHOO_PROVIDER
from models import Asset, AssetListing, AssetMapping, PriceEOD

logger = logging.getLogger("clean_listing_duplicates")

# Two copies of the same session agree when their closes differ by at most this
# share of the close of the kept listing.
CLOSE_TOLERANCE = 0.01

# Sessions the two series must share, and agree on, before bars are merged (or
# every bar of the shorter series when it holds fewer).
MIN_SHARED_SESSIONS = 5

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


@dataclass(frozen=True)
class SeriesComparison:
    """How the series of a duplicate compares with the one of the kept listing."""

    compared: int  # shared sessions whose closes were compared
    required: int  # compared sessions needed before the series are merged
    differ: int  # compared sessions on which the closes disagree

    @property
    def agree(self) -> bool:
        return self.differ == 0 and self.compared >= self.required


def _compare_series(session, duplicate_id: int, kept_id: int) -> SeriesComparison:
    """Compare the two series on the sessions they share.

    They agree when no compared session differs and enough sessions were compared:
    :data:`MIN_SHARED_SESSIONS`, or every bar of the shorter series when it holds
    fewer. A single matching bar must not carry thousands of bars nobody compared.
    When the kept listing has no bar, nothing can be compared and nothing is required.

    A close missing on one side only is a difference: otherwise the bar of the kept
    listing, without close, would win over the one of the duplicate. A session
    without close on both sides compares nothing, so it is not counted: series
    whose shared sessions all lack a close do not agree.
    """
    kept_bars = _count_bars(session, kept_id)
    if not kept_bars:
        return SeriesComparison(compared=0, required=0, differ=0)
    required = min(MIN_SHARED_SESSIONS, kept_bars, _count_bars(session, duplicate_id))
    compared, differ = session.execute(
        text("""
            SELECT count(*) FILTER (WHERE d.close IS NOT NULL OR k.close IS NOT NULL),
                   coalesce(sum(CASE
                       WHEN (d.close IS NULL) <> (k.close IS NULL) THEN 1
                       WHEN abs(d.close - k.close) > :tolerance * abs(k.close) THEN 1
                       ELSE 0 END), 0)
            FROM prices_eod d
            JOIN prices_eod k
              ON k.asset_listing_id = :kept
             AND k.resolution = d.resolution
             AND k.time = d.time
            WHERE d.asset_listing_id = :duplicate
        """),
        {"duplicate": duplicate_id, "kept": kept_id, "tolerance": CLOSE_TOLERANCE},
    ).one()
    # At least one compared session, even between two series of bars without close.
    return SeriesComparison(compared=compared, required=max(required, 1), differ=differ)


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
    # The resolutions in which the kept listing receives bars, read before the copy.
    received = (
        session.execute(
            text("""
                SELECT DISTINCT d.resolution FROM prices_eod d
                WHERE d.asset_listing_id = :duplicate
                  AND NOT EXISTS (
                      SELECT 1 FROM prices_eod k
                      WHERE k.asset_listing_id = :kept
                        AND k.resolution = d.resolution
                        AND k.time = d.time
                  )
            """),
            {"duplicate": duplicate_id, "kept": kept_id},
        )
        .scalars()
        .all()
    )

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
    # The moved bars may follow another adjustment basis than the kept ones (the
    # closes are only compared within 1 %, and a dividend changes adj_close only).
    # Without its row, a series is fetched again in full at the next ingestion
    # (historical/adjustment.py), so it comes back on one basis.
    for resolution in received:
        session.execute(
            text("""
                DELETE FROM price_series_adjustments
                WHERE asset_listing_id = :kept AND resolution = :resolution
            """),
            {"kept": kept_id, "resolution": resolution},
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
    """Refresh the weekly and monthly aggregates on the range of the duplicates' bars.

    The range covers the dropped bars too: when every bar of a duplicate was already
    on the kept listing, nothing moves but the aggregates still hold the duplicate.
    A refresh cannot run inside a transaction, so it runs after the commit, on its
    own connection. Without it the aggregates still show the duplicate until their
    daily job. Returns False when there was nothing to refresh or it could not run.
    """
    starts = [move.first for move in moves if move.first is not None]
    ends = [move.last for move in moves if move.last is not None]
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


def _trusted_yahoo(mapping: AssetMapping | None) -> bool:
    """A Yahoo symbol the ingestion uses without asking Yahoo again
    (``YahooSymbolResolver._stored`` of ``historical/yahoo_symbols.py``)."""
    return bool(
        mapping is not None
        and mapping.is_active
        and mapping.provider_ticker
        and (mapping.source == SET_BY_HAND or mapping.last_verified_at)
    )


def _hand_over_yahoo_mapping(session, duplicate: AssetListing, kept_id: int) -> bool:
    """Give the trusted Yahoo mapping of the duplicate to the kept listing.

    The mappings of a listing go with it (cascade). The duplicate that holds prices
    is usually the former primary listing, the one that was ingested: its symbol,
    verified or set by hand, is the one the kept listing needs. Without a trusted
    symbol the kept listing is not ingested (AGENTS.md, rule 13), so its series
    would not be fetched again in full. A kept listing that already has a trusted
    mapping keeps it; an untrusted one (not found, switched off) is replaced.
    """
    mine = next((m for m in duplicate.mappings if m.provider_name == YAHOO_PROVIDER), None)
    if not _trusted_yahoo(mine):
        return False
    kept = session.get(AssetListing, kept_id)
    theirs = next((m for m in kept.mappings if m.provider_name == YAHOO_PROVIDER), None)
    if _trusted_yahoo(theirs):
        return False
    if theirs is not None:
        kept.mappings.remove(theirs)
        # One mapping per listing and provider: the old one leaves first.
        session.flush()
    duplicate.mappings.remove(mine)
    kept.mappings.append(mine)
    session.flush()
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
    A trusted Yahoo mapping of a deleted duplicate goes to the kept listing when
    that one has none (:func:`_hand_over_yahoo_mapping`).
    Returns the number of listings deleted, of primaries moved to the kept listing,
    the ids of the duplicates left alone because they hold prices (and, among them,
    those whose series disagrees with the kept one), the comparison of each series,
    the ids of the duplicates whose Yahoo mapping went to the kept listing, and the
    bars moved and dropped.
    """
    deleted = primaries_moved = 0
    held_prices = []
    series_differ = []
    price_moves = []
    comparisons: dict[int, SeriesComparison] = {}
    mappings_moved = []
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
            comparison = _compare_series(session, listing.id, duplicate.kept_id)
            comparisons[duplicate.duplicate_id] = comparison
            if not comparison.agree:
                held_prices.append(duplicate.duplicate_id)
                series_differ.append(duplicate.duplicate_id)
                continue
            price_moves.append(_move_prices(session, listing.id, duplicate.kept_id))
        if _hand_over_yahoo_mapping(session, listing, duplicate.kept_id):
            mappings_moved.append(duplicate.duplicate_id)
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
        "comparisons": comparisons,
        "mappings_moved": mappings_moved,
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
            comparison = result["comparisons"].get(move.duplicate_id)
            compared = (
                f", {comparison.compared} shared sessions compared"
                if comparison and comparison.required
                else ", kept listing had no bar: nothing compared"
            )
            print(
                f"    listing {move.duplicate_id} -> listing {move.kept_id}: "
                f"{move.moved} moved, {move.dropped} dropped{compared}"
            )
    if result["mappings_moved"]:
        print(f"  Yahoo symbols handed over    : {len(result['mappings_moved'])}")
    print(f"  Left alone (they hold prices): {len(kept_with_prices)}")
    for duplicate in kept_with_prices:
        reason = ""
        if duplicate.duplicate_id in result["series_differ"]:
            comparison = result["comparisons"][duplicate.duplicate_id]
            reason = (
                f" — {comparison.compared} shared sessions compared "
                f"({comparison.required} required), {comparison.differ} differ: nothing moved"
            )
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
