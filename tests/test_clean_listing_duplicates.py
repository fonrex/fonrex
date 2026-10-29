"""scripts/clean_listing_duplicates.py: listings duplicated by a re-import after enrichment."""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from database.service import DatabaseService
from import_assets import AssetImporter, CSVRow
from models import Asset, AssetListing, AssetMapping, Base, PriceEOD
from scripts.clean_listing_duplicates import clean_duplicates, find_duplicates


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

    def _price(self, listing) -> None:
        self.session.add(
            PriceEOD(
                asset_listing_id=listing.id,
                asset_id=listing.asset_id,
                resolution="1D",
                timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
                close=1.0,
            )
        )
        self.session.flush()

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

        self.assertEqual(result, {"listings_deleted": 1, "primaries_moved": 0})
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

        self.assertEqual([d.has_prices for d in duplicates], [True])
        self.assertEqual(result["listings_deleted"], 0)
        self.assertEqual(self._listing_ids(), [kept.id, duplicate.id])

    def test_a_primary_duplicate_hands_its_role_to_the_kept_listing(self):
        asset = self._asset("US0378331005", "AAPL")
        kept = self._listing(asset, "NMS")
        self._listing(asset, "", is_primary=True)
        self.session.commit()

        result = clean_duplicates(self.session, find_duplicates(self.session), dry_run=False)

        self.assertEqual(result, {"listings_deleted": 1, "primaries_moved": 1})
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


if __name__ == "__main__":
    unittest.main()
