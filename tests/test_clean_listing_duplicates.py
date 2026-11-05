"""scripts/clean_listing_duplicates.py: listings duplicated by a re-import after enrichment."""

import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from database.service import DatabaseService
from historical.adjustment import ADJUSTMENT_SCHEME
from import_assets import AssetImporter, CSVRow
from models import Asset, AssetListing, AssetMapping, Base, PriceEOD, PriceSeriesAdjustment
from scripts.clean_listing_duplicates import (
    PriceMove,
    clean_duplicates,
    find_duplicates,
    refresh_price_aggregates,
)


class CleanListingDuplicatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = DatabaseService(f"sqlite:///{self.tmp_db.name}")
        Base.metadata.create_all(self.db.engine)
        self.session = self.db.get_session()

    def tearDown(self):
        self.session.close()
        self.db.Session.remove()
        self.db.engine.dispose()
        self.tmp_db.close()
        os.unlink(self.tmp_db.name)

    def _asset(self, isin: str, ticker: str) -> Asset:
        asset = Asset(isin=isin, ticker=ticker, name=ticker, is_active=True)
        self.session.add(asset)
        self.session.flush()
        return asset

    def _listing(self, asset, exchange, currency="USD", is_primary=False, ticker=None):
        listing = AssetListing(
            asset_id=asset.id,
            ticker=ticker or asset.ticker,
            exchange=exchange,
            currency=currency,
            is_primary=is_primary,
            source="csv_import",
        )
        self.session.add(listing)
        self.session.flush()
        self.session.add(
            AssetMapping(
                asset_id=asset.id,
                asset_listing_id=listing.id,
                provider_name="YahooFinance",
                provider_ticker=listing.ticker,
                source="csv_import",  # what the importer writes: not a verified symbol
            )
        )
        self.session.flush()
        return listing

    def _price(
        self,
        listing,
        day: date = date(2026, 10, 1),
        close: float | None = 1.0,
        resolution: str = "1D",
        **fields,
    ) -> None:
        self.session.add(
            PriceEOD(
                asset_listing_id=listing.id,
                asset_id=listing.asset_id,
                resolution=resolution,
                timestamp=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                close=close,
                **fields,
            )
        )
        self.session.flush()

    @staticmethod
    def _comparison(result: dict, duplicate_id: int, resolution: str = "1D"):
        [comparison] = [
            c for c in result["comparisons"][duplicate_id] if c.resolution == resolution
        ]
        return comparison

    def _closes(self, listing_id: int) -> dict[int, float]:
        """Closes of a listing by day of October 2026."""
        rows = (
            self.session.query(PriceEOD)
            .filter_by(asset_listing_id=listing_id)
            .order_by(PriceEOD.timestamp)
        )
        return {row.timestamp.day: row.close for row in rows}

    def _clean(self, move_prices=True, dry_run=False) -> dict:
        duplicates = find_duplicates(self.session)
        return clean_duplicates(self.session, duplicates, dry_run=dry_run, move_prices=move_prices)

    def _listing_ids(self) -> list[int]:
        return [listing.id for listing in self.session.query(AssetListing).order_by("id")]

    def test_reimport_duplicate_is_found_and_deleted_with_its_mappings(self):
        rows = [
            CSVRow(
                name="Apple",
                ticker="AAPL",
                isin="US0378331005",
                product_type="STOCK",
                currency="USD",
            )
        ]
        AssetImporter(self.session).run(rows)
        enriched = self.session.query(AssetListing).one()
        enriched.exchange = "NMS"
        duplicate = self._listing(enriched.asset, "")  # what the re-import created
        self.session.commit()

        duplicates = find_duplicates(self.session)
        self.assertEqual(
            [(d.duplicate_id, d.kept_id, d.kept_exchange) for d in duplicates],
            [(duplicate.id, enriched.id, "NMS")],
        )

        result = clean_duplicates(self.session, duplicates, dry_run=False)

        self.assertEqual(
            result,
            {
                "listings_deleted": 1,
                "primaries_moved": 0,
                "held_prices": [],
                "series_differ": [],
                "series_unchecked": [],
                "comparisons": {},
                "mappings_moved": [],
                "price_moves": [],
            },
        )
        self.assertEqual(self._listing_ids(), [enriched.id])
        self.assertEqual(
            self.session.query(AssetMapping).filter_by(asset_listing_id=duplicate.id).count(), 0
        )
        self.assertEqual(find_duplicates(self.session), [])

    def test_a_duplicate_holding_prices_is_reported_and_kept(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS", is_primary=True)
        duplicate = self._listing(asset, "")
        self._price(duplicate)
        self.session.commit()

        duplicates = find_duplicates(self.session)
        result = clean_duplicates(self.session, duplicates, dry_run=False)

        self.assertEqual([(d.has_prices, d.kept_has_prices) for d in duplicates], [(True, False)])
        self.assertEqual(result["listings_deleted"], 0)
        self.assertEqual(result["held_prices"], [duplicate.id])
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])

    def test_prices_ingested_after_the_search_keep_the_duplicate(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "", is_primary=True)
        self.session.commit()

        duplicates = find_duplicates(self.session)
        self.assertEqual([d.has_prices for d in duplicates], [False])
        self._price(duplicate)  # the scheduler ingests before the cleanup reaches it
        self.session.commit()
        result = clean_duplicates(self.session, duplicates, dry_run=False)

        self.assertEqual(
            result,
            {
                "listings_deleted": 0,
                "primaries_moved": 0,
                "held_prices": [duplicate.id],
                "series_differ": [],
                "series_unchecked": [],
                "comparisons": {},
                "mappings_moved": [],
                "price_moves": [],
            },
        )
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])
        self.assertTrue(self.session.get(AssetListing, duplicate.id).is_primary)

    def test_a_primary_duplicate_hands_its_role_to_the_kept_listing(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        self._listing(asset, "", is_primary=True)
        self.session.commit()

        result = clean_duplicates(self.session, find_duplicates(self.session), dry_run=False)

        self.assertEqual(
            result,
            {
                "listings_deleted": 1,
                "primaries_moved": 1,
                "held_prices": [],
                "series_differ": [],
                "series_unchecked": [],
                "comparisons": {},
                "mappings_moved": [],
                "price_moves": [],
            },
        )
        primaries = self.session.query(AssetListing).filter_by(is_primary=True).all()
        self.assertEqual([listing.id for listing in primaries], [kept.id])

    def test_other_listings_are_not_duplicates(self):
        asset = self._asset("IE00B3S5XW04", "GOVY")
        self._listing(asset, "", currency="EUR", is_primary=True)  # oldest of its group
        self._listing(asset, "XAMS", currency="EUR")
        self._listing(asset, "", currency="CHF")  # another currency
        self._listing(asset, "", currency="EUR", ticker="GOVY2")  # another ticker
        self._listing(asset, "XETR", currency="USD")
        self._listing(asset, "XLON", currency="USD")  # two real exchanges
        self.session.commit()

        self.assertEqual(find_duplicates(self.session), [])

    def test_dry_run_and_isin_filter_write_nothing_elsewhere(self):
        apple = self._asset("US0378331005", "AAPL")
        self._listing(apple, "NMS")
        self._listing(apple, "")
        sap = self._asset("DE0007164600", "SAP")
        self._listing(sap, "GER", currency="EUR")
        self._listing(sap, "", currency="EUR")
        self.session.commit()
        before = self._listing_ids()

        duplicates = find_duplicates(self.session)
        result = clean_duplicates(self.session, duplicates, dry_run=True)
        self.assertEqual(result["listings_deleted"], 2)
        self.assertEqual(self._listing_ids(), before)

        only_sap = find_duplicates(self.session, isin_filter="DE0007164600")
        self.assertEqual([d.isin for d in only_sap], ["DE0007164600"])
        clean_duplicates(self.session, only_sap, dry_run=False)
        self.assertEqual(len(self._listing_ids()), len(before) - 1)
        self.assertEqual([d.isin for d in find_duplicates(self.session)], ["US0378331005"])

    # --move-prices

    def test_bars_move_to_a_kept_listing_without_prices(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "", is_primary=True)
        self._yahoo(kept, "isin_search")  # ingested: the series is fetched again in full
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self._price(duplicate, date(2026, 10, 2), 11.0)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["listings_deleted"], 1)
        self.assertEqual(result["primaries_moved"], 1)
        self.assertEqual(result["held_prices"], [])
        [move] = result["price_moves"]
        self.assertEqual((move.duplicate_id, move.kept_id), (duplicate.id, kept.id))
        self.assertEqual((move.moved, move.dropped), (2, 0))
        self.assertEqual(self._listing_ids(), [kept.id])
        self.assertEqual(self._closes(kept.id), {1: 10.0, 2: 11.0})
        self.assertEqual(self._closes(duplicate.id), {})
        self.assertTrue(self.session.get(AssetListing, kept.id).is_primary)
        self.assertFalse(self._comparison(result, duplicate.id).checked)

    def test_a_series_nobody_compared_needs_a_trusted_symbol_to_move(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "", is_primary=True)
        # Fetched before rule 13 with the ticker as typed: maybe another fund's series.
        self._yahoo(duplicate, "csv_import", verified=False)
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["held_prices"], [duplicate.id])
        self.assertEqual(result["series_unchecked"], [duplicate.id])
        self.assertEqual(result["series_differ"], [])
        self.assertEqual(result["price_moves"], [])
        self.assertEqual(self._closes(kept.id), {})
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])

    def test_a_trusted_symbol_handed_over_lets_the_series_move(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "", is_primary=True)
        symbol = self._yahoo(duplicate, "isin_search")
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_unchecked"], [])
        self.assertEqual(result["mappings_moved"], [duplicate.id])
        self.assertEqual(self._yahoo_of(kept.id), (symbol, "isin_search"))
        self.assertEqual(self._closes(kept.id), {1: 10.0})

    def test_only_the_missing_sessions_move_when_the_series_agree(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        for day in range(1, 6):
            self._price(kept, date(2026, 10, day), 10.0 + day)
            # Day 2 differs within 1 %: the same session, from another source.
            self._price(duplicate, date(2026, 10, day), 12.05 if day == 2 else 10.0 + day)
        self._price(duplicate, date(2026, 10, 6), 16.0)  # a session the kept one lacks
        self.session.commit()

        result = self._clean()

        [move] = result["price_moves"]
        self.assertEqual((move.moved, move.dropped), (1, 5))
        self.assertEqual(self._comparison(result, duplicate.id).compared, 5)
        self.assertEqual(self._listing_ids(), [kept.id])
        # The bar the kept listing already had is not replaced.
        self.assertEqual(
            self._closes(kept.id), {1: 11.0, 2: 12.0, 3: 13.0, 4: 14.0, 5: 15.0, 6: 16.0}
        )

    def test_one_shared_session_does_not_carry_a_long_series(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        for day in range(1, 6):
            self._price(kept, date(2026, 10, day), 10.0)
        for day in range(5, 20):
            self._price(duplicate, date(2026, 10, day), 10.0)  # one session in common
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_differ"], [duplicate.id])
        comparison = self._comparison(result, duplicate.id)
        self.assertEqual((comparison.compared, comparison.required), (1, 5))
        self.assertEqual(result["price_moves"], [])
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])

    def test_a_single_kept_bar_does_not_carry_a_long_series(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), 10.0)
        for day in range(1, 16):
            self._price(duplicate, date(2026, 10, day), 10.0)  # the one shared bar matches
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_differ"], [duplicate.id])
        comparison = self._comparison(result, duplicate.id)
        self.assertEqual((comparison.compared, comparison.required), (1, 5))
        self.assertEqual(self._closes(kept.id), {1: 10.0})

    def test_a_short_series_outside_the_kept_dates_is_reported(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._yahoo(kept, "isin_search")  # trusted: it does not lift the comparison
        for day in range(1, 11):
            self._price(kept, date(2026, 10, day), 10.0)
        for day in range(1, 4):
            self._price(duplicate, date(2026, 9, day), 10.0)  # no session in common
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_differ"], [duplicate.id])
        comparison = self._comparison(result, duplicate.id)
        self.assertEqual((comparison.compared, comparison.required), (0, 3))
        self.assertEqual(result["price_moves"], [])
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])

    def test_each_resolution_is_compared_on_its_own(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        for day in range(1, 6):
            self._price(kept, date(2026, 10, day), 10.0)
            self._price(duplicate, date(2026, 10, day), 10.0)  # the daily series agree
        # A weekly series the kept listing lacks: nothing to compare it with.
        self._price(duplicate, date(2026, 9, 28), 30.0, resolution="1W")
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_unchecked"], [duplicate.id])
        self.assertTrue(self._comparison(result, duplicate.id, "1D").agree)
        self.assertFalse(self._comparison(result, duplicate.id, "1W").checked)
        self.assertEqual(result["price_moves"], [])
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])

    def test_a_weekly_series_moves_when_the_kept_listing_is_ingested(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._yahoo(kept, "isin_search")
        for day in range(1, 6):
            self._price(kept, date(2026, 10, day), 10.0)
            self._price(duplicate, date(2026, 10, day), 10.0)
        self._price(duplicate, date(2026, 9, 28), 30.0, resolution="1W")
        self._scheme(kept, "1D")
        self._scheme(kept, "1W")
        self.session.commit()

        [move] = self._clean()["price_moves"]

        self.assertEqual((move.moved, move.dropped), (1, 5))
        weekly = self.session.query(PriceEOD).filter_by(asset_listing_id=kept.id, resolution="1W")
        self.assertEqual([bar.close for bar in weekly], [30.0])
        # The daily series received nothing; the weekly one is fetched again in full.
        self.assertEqual(self._schemes(kept.id), ["1D"])

    def test_one_resolution_that_differs_keeps_the_whole_duplicate(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        for day in range(1, 6):
            self._price(kept, date(2026, 10, day), 10.0)
            self._price(duplicate, date(2026, 10, day), 10.0)
        self._price(kept, date(2026, 9, 28), 30.0, resolution="1W")
        self._price(duplicate, date(2026, 9, 28), 45.0, resolution="1W")
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_differ"], [duplicate.id])
        self.assertFalse(self._comparison(result, duplicate.id, "1W").agree)
        self.assertEqual(result["price_moves"], [])
        self.assertEqual(
            self._closes(duplicate.id), {1: 10.0, 2: 10.0, 3: 10.0, 4: 10.0, 5: 10.0, 28: 45.0}
        )

    def test_a_short_series_compared_in_full_is_merged(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        for day in (1, 2):
            self._price(kept, date(2026, 10, day), 10.0)
        for day in (1, 2, 3):
            self._price(duplicate, date(2026, 10, day), 10.0)
        self.session.commit()

        result = self._clean()

        # A short duplicate series: as many sessions as the shorter series holds (two).
        self.assertEqual(self._comparison(result, duplicate.id).required, 2)
        [move] = result["price_moves"]
        self.assertEqual((move.moved, move.dropped), (1, 2))

    def test_a_duplicate_whose_series_differs_is_reported_and_kept(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), 10.0)
        self._price(duplicate, date(2026, 10, 1), 25.0)  # another instrument's series
        self._price(duplicate, date(2026, 10, 2), 26.0)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["listings_deleted"], 0)
        self.assertEqual(result["held_prices"], [duplicate.id])
        self.assertEqual(result["series_differ"], [duplicate.id])
        self.assertEqual(result["price_moves"], [])
        self.assertEqual(self._closes(kept.id), {1: 10.0})
        self.assertEqual(self._closes(duplicate.id), {1: 25.0, 2: 26.0})

    def test_series_that_share_no_session_are_not_merged(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), 10.0)
        self._price(duplicate, date(2026, 10, 5), 10.0)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_differ"], [duplicate.id])
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])

    def test_a_close_missing_on_one_side_only_is_a_difference(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), None)
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_differ"], [duplicate.id])
        self.assertEqual(result["price_moves"], [])
        self.assertEqual(self._closes(duplicate.id), {1: 10.0})

    def test_sessions_without_close_on_both_sides_are_not_an_agreement(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), None)
        self._price(duplicate, date(2026, 10, 1), None)
        self._price(duplicate, date(2026, 10, 2), 10.0)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["series_differ"], [duplicate.id])
        self.assertEqual(result["price_moves"], [])
        self.assertEqual(self._closes(duplicate.id), {1: None, 2: 10.0})

    def _scheme(self, listing, resolution: str = "1D") -> None:
        self.session.add(
            PriceSeriesAdjustment(
                asset_listing_id=listing.id,
                resolution=resolution,
                scheme=ADJUSTMENT_SCHEME,
                fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            )
        )
        self.session.flush()

    def _schemes(self, listing_id: int) -> list[str]:
        rows = self.session.query(PriceSeriesAdjustment).filter_by(asset_listing_id=listing_id)
        return sorted(row.resolution for row in rows)

    def test_a_series_that_receives_bars_is_fetched_again_in_full(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), 10.0)
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self._price(duplicate, date(2026, 10, 2), 11.0)  # may be on another basis
        self._scheme(kept, "1D")
        self._scheme(kept, "1W")  # receives nothing: stays
        self.session.commit()

        [move] = self._clean()["price_moves"]

        self.assertEqual(move.moved, 1)
        # Without its row, the daily series is fetched again in one piece.
        self.assertEqual(self._schemes(kept.id), ["1W"])

    def test_fields_only_the_duplicate_has_fill_the_kept_bar(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), 10.0, volume=500)
        self._price(duplicate, date(2026, 10, 1), 10.05, volume=900, adj_close=9.5, open=9.9)
        self._scheme(kept, "1D")
        self.session.commit()

        [move] = self._clean()["price_moves"]

        self.assertEqual((move.moved, move.dropped, move.filled), (0, 1, 1))
        bar = self.session.query(PriceEOD).filter_by(asset_listing_id=kept.id).one()
        # The kept values stay; the missing ones come from the duplicate.
        self.assertEqual((bar.close, bar.volume, bar.adj_close, bar.open), (10.0, 500, 9.5, 9.9))
        # An adjusted close from another source: the series is fetched again in full.
        self.assertEqual(self._schemes(kept.id), [])

    def test_a_series_whose_bars_were_all_dropped_keeps_its_scheme(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), 10.0)
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self._scheme(kept, "1D")
        self.session.commit()

        [move] = self._clean()["price_moves"]

        self.assertEqual(move.moved, 0)
        self.assertEqual(self._schemes(kept.id), ["1D"])

    def test_bars_all_dropped_still_report_their_range(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), 10.0)
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self.session.commit()

        [move] = self._clean()["price_moves"]

        self.assertEqual((move.moved, move.dropped), (0, 1))
        self.assertIsNotNone(move.first)  # the aggregates get refreshed on this range
        self.assertEqual(self._listing_ids(), [kept.id])

    def test_aggregates_are_refreshed_when_bars_were_only_dropped(self):
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        day = datetime(2026, 10, 1, tzinfo=timezone.utc)
        move = PriceMove(duplicate_id=2, kept_id=1, moved=0, dropped=1, first=day, last=day)

        self.assertTrue(refresh_price_aggregates(engine, [move]))
        engine.connect.assert_called_once()

    def test_each_range_is_refreshed_on_its_own(self):
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        connection = engine.connect.return_value.execution_options.return_value.__enter__()

        def move(first: date, last: date) -> PriceMove:
            return PriceMove(
                duplicate_id=2,
                kept_id=1,
                moved=1,
                dropped=0,
                first=datetime(first.year, first.month, first.day, tzinfo=timezone.utc),
                last=datetime(last.year, last.month, last.day, tzinfo=timezone.utc),
            )

        moves = [
            move(date(2025, 1, 2), date(2025, 3, 1)),
            move(date(1995, 1, 2), date(1995, 2, 1)),
            move(date(1995, 1, 20), date(1995, 6, 1)),  # overlaps the one above
        ]
        self.assertTrue(refresh_price_aggregates(engine, moves))

        windows = [
            (params["start"].year, params["end"].month)
            for call in connection.execute.call_args_list
            if len(call.args) > 1 and "start" in (params := call.args[1])
        ]
        # Two ranges for each of the two aggregates, not one of thirty years.
        self.assertEqual(windows, [(1995, 6), (2025, 3)] * 2)

    def _yahoo(self, listing, source: str, verified: bool = True, active: bool = True):
        mapping = (
            self.session.query(AssetMapping)
            .filter_by(asset_listing_id=listing.id, provider_name="YahooFinance")
            .one()
        )
        mapping.source = source
        mapping.provider_ticker = f"{listing.ticker}.{listing.id}"
        mapping.is_active = active
        mapping.last_verified_at = datetime(2026, 10, 1, tzinfo=timezone.utc) if verified else None
        self.session.flush()
        return mapping.provider_ticker

    def _yahoo_of(self, listing_id: int) -> tuple[str, str] | None:
        mapping = (
            self.session.query(AssetMapping)
            .filter_by(asset_listing_id=listing_id, provider_name="YahooFinance")
            .one_or_none()
        )
        return (mapping.provider_ticker, mapping.source) if mapping else None

    def test_a_verified_yahoo_symbol_goes_to_the_kept_listing(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "", is_primary=True)
        self._yahoo(kept, "symbol_not_found", verified=False, active=False)
        symbol = self._yahoo(duplicate, "isin_search")
        self._price(duplicate)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["mappings_moved"], [duplicate.id])
        self.assertEqual(self._yahoo_of(kept.id), (symbol, "isin_search"))
        self.assertEqual(self._listing_ids(), [kept.id])

    def test_a_symbol_set_by_hand_is_not_lost(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        symbol = self._yahoo(duplicate, "manual", verified=False)
        self.session.commit()

        result = self._clean(move_prices=False)

        self.assertEqual(result["mappings_moved"], [duplicate.id])
        self.assertEqual(self._yahoo_of(kept.id), (symbol, "manual"))

    def test_a_trusted_symbol_of_the_kept_listing_stays(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        mine = self._yahoo(kept, "isin_search")
        self._yahoo(duplicate, "manual")
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["mappings_moved"], [])
        self.assertEqual(self._yahoo_of(kept.id), (mine, "isin_search"))

    def test_an_unverified_symbol_of_the_duplicate_is_not_handed_over(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._yahoo(duplicate, "csv_import", verified=False)
        self.session.commit()

        result = self._clean()

        self.assertEqual(result["mappings_moved"], [])
        self.assertEqual(self._yahoo_of(kept.id), (kept.ticker, "csv_import"))

    def test_a_dry_run_hands_over_no_symbol(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._yahoo(duplicate, "isin_search")
        self.session.commit()

        result = self._clean(dry_run=True)

        self.assertEqual(result["mappings_moved"], [duplicate.id])
        self.assertEqual(self._yahoo_of(kept.id), (kept.ticker, "csv_import"))
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])

    def test_dry_run_reports_the_moves_and_writes_nothing(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._yahoo(kept, "isin_search")
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self.session.commit()

        result = self._clean(dry_run=True)

        self.assertEqual([(m.moved, m.dropped) for m in result["price_moves"]], [(1, 0)])
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])
        self.assertEqual(self._closes(kept.id), {})
        self.assertEqual(self._closes(duplicate.id), {1: 10.0})

    def test_without_the_option_prices_are_never_moved(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(duplicate, date(2026, 10, 1), 10.0)
        self.session.commit()

        result = self._clean(move_prices=False)

        self.assertEqual(result["held_prices"], [duplicate.id])
        self.assertEqual(result["series_differ"], [])
        self.assertEqual(self._closes(kept.id), {})


if __name__ == "__main__":
    unittest.main()
