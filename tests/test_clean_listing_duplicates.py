"""scripts/clean_listing_duplicates.py: listings duplicated by a re-import after enrichment."""

import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from database.service import DatabaseService
from import_assets import AssetImporter, CSVRow
from models import Asset, AssetListing, AssetMapping, Base, PriceEOD
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
            )
        )
        self.session.flush()
        return listing

    def _price(self, listing, day: date = date(2026, 10, 1), close: float | None = 1.0) -> None:
        self.session.add(
            PriceEOD(
                asset_listing_id=listing.id,
                asset_id=listing.asset_id,
                resolution="1D",
                timestamp=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                close=close,
            )
        )
        self.session.flush()

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

    def test_only_the_missing_sessions_move_when_the_series_agree(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
        self._price(kept, date(2026, 10, 1), 10.0)
        self._price(kept, date(2026, 10, 2), 11.0)
        self._price(duplicate, date(2026, 10, 2), 11.05)  # same session, within 1 %
        self._price(duplicate, date(2026, 10, 3), 12.0)  # a session the kept one lacks
        self.session.commit()

        result = self._clean()

        [move] = result["price_moves"]
        self.assertEqual((move.moved, move.dropped), (1, 1))
        self.assertEqual(self._listing_ids(), [kept.id])
        # The bar the kept listing already had is not replaced.
        self.assertEqual(self._closes(kept.id), {1: 10.0, 2: 11.0, 3: 12.0})

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

    def test_dry_run_reports_the_moves_and_writes_nothing(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        duplicate = self._listing(asset, "")
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
