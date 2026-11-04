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
duplicate is deleted. Each series (resolution) of the duplicate is checked on its
own:

- when the kept listing holds bars at that resolution, the two series must agree on
  the sessions they share (close within 1 %; a close missing on one side only is a
  difference; a session without close on both sides is not compared), and enough
  sessions must be compared: :data:`MIN_SHARED_SESSIONS`, or, for a duplicate
  series of that many bars or fewer, all the bars the two series can share;
- when it holds none, nothing can be compared: the series moves only if the kept
  listing ends up with a trusted Yahoo symbol (its own, or the one handed over),
  so that the next ingestion fetches it again in full and replaces it.

If one series fails, nothing moves and the duplicate is reported. Otherwise the bars
whose (resolution, time) the kept listing lacks are moved; on a shared session the
kept bar stays, and only the fields it lacks (a volume, an adjusted close) are taken
from the duplicate. A series of the kept listing that receives bars or fields loses
its row of ``price_series_adjustments``: the next ingestion fetches it again in full,
on one adjustment basis. The weekly and monthly aggregates are refreshed on the range
of each duplicate's bars once the change is committed.

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
from historical.yahoo_symbols import YAHOO_PROVIDER, is_trusted_symbol
from models import Asset, AssetListing, AssetMapping, PriceEOD

logger = logging.getLogger("clean_listing_duplicates")

# Two copies of the same session agree when their closes differ by at most this
# share of the close of the kept listing.
CLOSE_TOLERANCE = 0.01

# Sessions two series of one resolution must share, and agree on, before bars are
# merged (a duplicate series of that many bars or fewer: all the sessions it can share).
MIN_SHARED_SESSIONS = 5

# Fields of a shared session that the duplicate's bar fills when the kept bar lacks them.
FILLED_FIELDS = ("open", "high", "low", "close", "adj_close", "volume")

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
    filled: int = 0  # kept bars that received fields from the duplicate's bar


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


@dataclass(frozen=True)
class SeriesComparison:
    """How one series (resolution) of a duplicate compares with the kept listing's."""

    resolution: str
    bars: int  # bars of the duplicate at this resolution
    shared: int  # of them, sessions the kept listing holds too
    compared: int  # shared sessions whose closes were compared
    required: int  # compared sessions needed; 0 when the kept listing has no bar here
    differ: int  # compared sessions on which the closes disagree
    filled: int  # shared sessions whose kept bar lacks a field the duplicate's bar has
    first: datetime | None
    last: datetime | None

    @property
    def checked(self) -> bool:
        """The kept listing holds bars at this resolution: the series were compared."""
        return self.required > 0

    @property
    def agree(self) -> bool:
        return self.differ == 0 and self.compared >= self.required

    @property
    def moved(self) -> int:
        return self.bars - self.shared


def _required(bars: int, kept_bars: int) -> int:
    """Compared sessions needed before a duplicate series of ``bars`` bars is merged.

    :data:`MIN_SHARED_SESSIONS` as soon as the duplicate holds more bars than that: a
    single matching bar must not carry thousands of bars nobody compared, however
    short the kept series is. A shorter duplicate series must match on every session
    the two can share. ``kept_bars`` is counted up to :data:`MIN_SHARED_SESSIONS`.
    """
    if bars > MIN_SHARED_SESSIONS:
        return MIN_SHARED_SESSIONS
    # At least one compared session, even between two series of bars without close.
    return max(1, min(bars, kept_bars))


def _compare_series(session, duplicate_id: int, kept_id: int) -> tuple[SeriesComparison, ...]:
    """Compare each series of the duplicate with the kept listing's, resolution by resolution.

    A close missing on one side only is a difference: otherwise the bar of the kept
    listing, without close, would win over the one of the duplicate. A session
    without close on both sides compares nothing, so it is not counted: series
    whose shared sessions all lack a close do not agree. One aggregate over the
    duplicate's bars gives every count the move needs, so that nothing else is
    counted while both listings are locked.
    """
    fills = " OR ".join(f"(k.{f} IS NULL AND d.{f} IS NOT NULL)" for f in FILLED_FIELDS)
    rows = session.execute(
        text(f"""
            SELECT d.resolution,
                   count(*),
                   count(k.time),
                   count(*) FILTER (
                       WHERE k.time IS NOT NULL AND (d.close IS NOT NULL OR k.close IS NOT NULL)),
                   count(*) FILTER (
                       WHERE k.time IS NOT NULL
                         AND ((d.close IS NULL) <> (k.close IS NULL)
                              OR abs(d.close - k.close) > :tolerance * abs(k.close))),
                   count(*) FILTER (WHERE k.time IS NOT NULL AND ({fills})),
                   min(d.time),
                   max(d.time)
            FROM prices_eod d
            LEFT JOIN prices_eod k
              ON k.asset_listing_id = :kept
             AND k.resolution = d.resolution
             AND k.time = d.time
            WHERE d.asset_listing_id = :duplicate
            GROUP BY d.resolution
            ORDER BY d.resolution
        """),
        {"duplicate": duplicate_id, "kept": kept_id, "tolerance": CLOSE_TOLERANCE},
    ).all()
    comparisons = []
    for resolution, bars, shared, compared, differ, filled, first, last in rows:
        # Counted up to MIN_SHARED_SESSIONS: that is all _required reads.
        kept_bars = session.execute(
            text("""
                SELECT count(*) FROM (
                    SELECT 1 FROM prices_eod
                    WHERE asset_listing_id = :kept AND resolution = :resolution
                    LIMIT :limit
                ) AS kept_bars
            """),
            {"kept": kept_id, "resolution": resolution, "limit": MIN_SHARED_SESSIONS},
        ).scalar()
        comparisons.append(
            SeriesComparison(
                resolution=resolution,
                bars=bars,
                shared=shared,
                compared=compared,
                required=_required(bars, kept_bars) if kept_bars else 0,
                differ=differ,
                filled=filled,
                first=first,
                last=last,
            )
        )
    return tuple(comparisons)


def _move_prices(
    session, duplicate_id: int, kept_id: int, comparisons: tuple[SeriesComparison, ...]
) -> PriceMove:
    """Copy the bars of the duplicate that the kept listing lacks, then delete them all.

    The bars are inserted under the kept listing rather than updated in place: the
    listing is part of the key and of the compression segments of the hypertable.
    On a shared session the kept bar stays, and receives only the fields it lacks.
    The counts come from ``comparisons``, read under the same locks.
    """
    copied = [c.name for c in PriceEOD.__table__.columns if c.name != "asset_listing_id"]
    columns = ", ".join(copied)
    fill = ", ".join(f"{f} = coalesce(prices_eod.{f}, excluded.{f})" for f in FILLED_FIELDS)
    lacking = " OR ".join(
        f"(prices_eod.{f} IS NULL AND excluded.{f} IS NOT NULL)" for f in FILLED_FIELDS
    )
    session.execute(
        text(f"""
            INSERT INTO prices_eod (asset_listing_id, {columns})
            SELECT :kept, {columns} FROM prices_eod WHERE asset_listing_id = :duplicate
            ON CONFLICT (asset_listing_id, resolution, time)
            DO UPDATE SET {fill} WHERE {lacking}
        """),
        {"duplicate": duplicate_id, "kept": kept_id},
    )
    total = session.execute(
        text("DELETE FROM prices_eod WHERE asset_listing_id = :duplicate"),
        {"duplicate": duplicate_id},
    ).rowcount
    # The moved bars and fields may follow another adjustment basis than the kept
    # ones (the closes are only compared within 1 %, and a dividend changes
    # adj_close only). Without its row, a series is fetched again in full at the
    # next ingestion (historical/adjustment.py), so it comes back on one basis.
    for comparison in comparisons:
        if comparison.moved or comparison.filled:
            session.execute(
                text("""
                    DELETE FROM price_series_adjustments
                    WHERE asset_listing_id = :kept AND resolution = :resolution
                """),
                {"kept": kept_id, "resolution": comparison.resolution},
            )
    # PriceEOD objects of the duplicate still in the session are gone from the table.
    session.expire_all()
    moved = sum(comparison.moved for comparison in comparisons)
    starts = [c.first for c in comparisons if c.first is not None]
    ends = [c.last for c in comparisons if c.last is not None]
    return PriceMove(
        duplicate_id=duplicate_id,
        kept_id=kept_id,
        moved=moved,
        dropped=total - moved,
        first=min(starts) if starts else None,
        last=max(ends) if ends else None,
        filled=sum(comparison.filled for comparison in comparisons),
    )


def _refresh_windows(moves: list[PriceMove]) -> list[tuple[datetime, datetime]]:
    """The ranges of the moves, those that overlap merged: one refresh per range."""
    windows: list[tuple[datetime, datetime]] = []
    for first, last in sorted((m.first, m.last) for m in moves if m.first is not None):
        last = last if last is not None else first
        if windows and first <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], last))
        else:
            windows.append((first, last))
    return windows


def refresh_price_aggregates(engine, moves: list[PriceMove]) -> bool:
    """Refresh the weekly and monthly aggregates on the range of each duplicate's bars.

    Each move is refreshed on its own range (overlapping ones once): two duplicates
    from 1995 and 2025 must not refresh the thirty years between them. The range
    covers the dropped bars too: when every bar of a duplicate was already
    on the kept listing, nothing moves but the aggregates still hold the duplicate.
    A refresh cannot run inside a transaction, so it runs after the commit, on its
    own connection. Without it the aggregates still show the duplicate until their
    daily job. Returns False when there was nothing to refresh or it could not run.
    """
    windows = _refresh_windows(moves)
    if not windows or engine.dialect.name != "postgresql":
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
                for start, end in windows:
                    # A refresh only covers the buckets that fit whole in its window.
                    connection.execute(
                        text(f"""
                            CALL refresh_continuous_aggregate(
                                '{view_name}',
                                CAST(:start AS timestamptz) - INTERVAL '{period}',
                                CAST(:end AS timestamptz) + INTERVAL '{period}'
                            )
                        """),
                        {"start": start, "end": end},
                    )
    except Exception as exc:  # the cleanup is committed; the daily job will catch up
        logger.warning("Aggregates not refreshed (their daily job will do it): %s", exc)
        return False
    return True


def _yahoo_mapping(listing: AssetListing) -> AssetMapping | None:
    return next((m for m in listing.mappings if m.provider_name == YAHOO_PROVIDER), None)


def _trusted_yahoo(mapping: AssetMapping | None) -> bool:
    """A Yahoo symbol the ingestion uses without asking Yahoo again."""
    return mapping is not None and is_trusted_symbol(
        mapping.provider_ticker,
        source=mapping.source,
        active=bool(mapping.is_active),
        verified=mapping.last_verified_at is not None,
    )


def _kept_ends_trusted(session, duplicate: AssetListing, kept_id: int) -> bool:
    """The kept listing has a trusted Yahoo symbol, or receives the duplicate's.

    Only then is it ingested (AGENTS.md, rule 13), and a series it receives without
    comparison fetched again in full and replaced.
    """
    kept = session.get(AssetListing, kept_id)
    return _trusted_yahoo(_yahoo_mapping(kept)) or _trusted_yahoo(_yahoo_mapping(duplicate))


def _hand_over_yahoo_mapping(session, duplicate: AssetListing, kept_id: int) -> bool:
    """Give the trusted Yahoo mapping of the duplicate to the kept listing.

    The mappings of a listing go with it (cascade). The duplicate that holds prices
    is usually the former primary listing, the one that was ingested: its symbol,
    verified or set by hand, is the one the kept listing needs. Without a trusted
    symbol the kept listing is not ingested (AGENTS.md, rule 13), so its series
    would not be fetched again in full. A kept listing that already has a trusted
    mapping keeps it; an untrusted one (not found, switched off) is replaced.
    """
    mine = _yahoo_mapping(duplicate)
    if not _trusted_yahoo(mine):
        return False
    kept = session.get(AssetListing, kept_id)
    theirs = _yahoo_mapping(kept)
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
    those whose series disagrees with the kept one, and those holding a series the
    kept listing lacks while it would have no trusted Yahoo symbol), the
    comparisons of each duplicate (one per resolution), the ids of the duplicates
    whose Yahoo mapping went to the kept listing, and the bars moved and dropped.
    """
    deleted = primaries_moved = 0
    held_prices = []
    series_differ = []
    series_unchecked = []
    price_moves = []
    comparisons: dict[int, tuple[SeriesComparison, ...]] = {}
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
            series = _compare_series(session, listing.id, duplicate.kept_id)
            comparisons[duplicate.duplicate_id] = series
            # All or nothing: a duplicate is either merged and deleted, or left whole.
            if not all(comparison.agree for comparison in series):
                held_prices.append(duplicate.duplicate_id)
                series_differ.append(duplicate.duplicate_id)
                continue
            unchecked = any(not comparison.checked for comparison in series)
            if unchecked and not _kept_ends_trusted(session, listing, duplicate.kept_id):
                # Nothing compared, and nothing would ever fetch the series again.
                held_prices.append(duplicate.duplicate_id)
                series_unchecked.append(duplicate.duplicate_id)
                continue
            price_moves.append(_move_prices(session, listing.id, duplicate.kept_id, series))
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
        "series_unchecked": series_unchecked,
        "comparisons": comparisons,
        "mappings_moved": mappings_moved,
        "price_moves": price_moves,
    }


def _describe(comparison: SeriesComparison) -> str:
    if not comparison.checked:
        return f"{comparison.resolution}: {comparison.bars} bars, the kept listing has none"
    verdict = "agree" if comparison.agree else "differ"
    return (
        f"{comparison.resolution}: {comparison.bars} bars, {comparison.compared} shared "
        f"sessions compared ({comparison.required} required), {comparison.differ} differ: "
        f"{verdict}"
    )


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
        print(f"  Kept bars given fields       : {sum(move.filled for move in moves)}")
        for move in moves:
            print(
                f"    listing {move.duplicate_id} -> listing {move.kept_id}: "
                f"{move.moved} moved, {move.dropped} dropped, {move.filled} filled"
            )
            for comparison in result["comparisons"].get(move.duplicate_id, ()):
                print(f"      {_describe(comparison)}")
    if result["mappings_moved"]:
        print(f"  Yahoo symbols handed over    : {len(result['mappings_moved'])}")
    print(f"  Left alone (they hold prices): {len(kept_with_prices)}")
    for duplicate in kept_with_prices:
        reason = ""
        if duplicate.duplicate_id in result["series_differ"]:
            reason = " — a series does not agree: nothing moved"
        elif duplicate.duplicate_id in result["series_unchecked"]:
            reason = (
                " — a series the kept listing lacks, and no trusted Yahoo symbol "
                "to fetch it again: nothing moved"
            )
        print(
            f"    listing {duplicate.duplicate_id} ({duplicate.isin} {duplicate.ticker} "
            f"{duplicate.currency}) duplicates listing {duplicate.kept_id} "
            f"(exchange {duplicate.kept_exchange or '-'}, "
            f"{'with' if duplicate.kept_has_prices else 'without'} prices){reason}"
        )
        for comparison in result["comparisons"].get(duplicate.duplicate_id, ()):
            print(f"      {_describe(comparison)}")
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
