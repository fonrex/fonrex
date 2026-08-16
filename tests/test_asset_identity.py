import tempfile
import unittest

from database.service import DatabaseService
from models import Asset, AssetListing, AssetMapping, Base


class AssetIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db")
        self.db = DatabaseService(f"sqlite:///{self.tmp.name}")
        Base.metadata.create_all(self.db.engine)

    def tearDown(self):
        self.db.Session.remove()
        self.db.engine.dispose()
        self.tmp.close()

    def test_ticker_is_unique_per_exchange_and_isin_lookup_wins(self):
        session = self.db.get_session()
        try:
            air_euronext = Asset(
                ticker="AIR",
                exchange="EPA",
                isin="NL0000235190",
                name="Airbus SE",
            )
            air_nyse = Asset(
                ticker="AIR",
                exchange="NYSE",
                isin="US0003611052",
                name="AAR Corp",
            )
            session.add_all([air_euronext, air_nyse])
            session.commit()

            asset = self.db.get_asset_by_identity(ticker="AIR", exchange="NYSE")
            self.assertEqual(asset.name, "AAR Corp")

            asset = self.db.get_asset_by_identity(
                ticker="AIR", isin="NL0000235190", exchange="NYSE"
            )
            self.assertEqual(asset.name, "Airbus SE")
        finally:
            session.close()

    def test_asset_mappings_are_loaded_with_asset_identity(self):
        session = self.db.get_session()
        try:
            asset = Asset(
                ticker="AAPL",
                exchange="NASDAQ",
                isin="US0378331005",
                name="Apple Inc.",
            )
            session.add(asset)
            session.flush()
            session.add(
                AssetMapping(
                    asset_id=asset.id,
                    provider_name="GoogleFinance",
                    provider_ticker="NASDAQ: AAPL",
                    provider_url="https://www.google.com/finance/quote/AAPL:NASDAQ",
                    source="unit_test",
                    confidence_score=0.99,
                )
            )
            session.commit()

            loaded = self.db.get_asset_mappings(isin="US0378331005")
            self.assertEqual(loaded.ticker, "AAPL")
            self.assertEqual(len(loaded.mappings), 1)
            self.assertEqual(loaded.mappings[0].provider_name, "GoogleFinance")
            self.assertTrue(loaded.mappings[0].is_active)
            self.assertEqual(loaded.mappings[0].source, "unit_test")
            self.assertEqual(loaded.mappings[0].failure_count, 0)
        finally:
            session.close()

    def test_missing_asset_profile_fields_are_completed_from_metadata(self):
        session = self.db.get_session()
        try:
            asset = Asset(
                ticker="AAPL",
                isin="US0378331005",
                name="Apple Inc",
                currency="USD",
                quote_type="STOCK",
            )
            session.add(asset)
            session.flush()
            listing = AssetListing(
                asset_id=asset.id,
                ticker="AAPL",
                exchange="",
                currency="USD",
                is_primary=True,
                source="unit_test",
            )
            session.add(listing)
            session.commit()
            asset_id = asset.id
            listing_id = listing.id
        finally:
            session.close()

        details = self.db.get_asset_details(ticker="AAPL")
        self.assertTrue(self.db.asset_profile_needs_enrichment(details))

        updated = self.db.update_asset_profile_from_metadata(
            asset_id,
            {
                "display_name": "Apple Inc.",
                "official_symbol": "AAPL",
                "exchange": "NMS",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "ir_website": "https://investor.apple.com",
                "long_business_summary": "Apple designs consumer technology products.",
            },
            listing_id=listing_id,
        )

        self.assertTrue(updated)

        enriched = self.db.get_asset_details(ticker="AAPL")
        self.assertFalse(self.db.asset_profile_needs_enrichment(enriched))
        self.assertEqual(enriched["display_name"], "Apple Inc.")
        self.assertEqual(enriched["official_symbol"], "AAPL")
        self.assertEqual(enriched["exchange"], "NMS")
        self.assertEqual(enriched["sector"], "Technology")
        self.assertEqual(enriched["industry"], "Consumer Electronics")
        self.assertEqual(enriched["ir_website"], "https://investor.apple.com")
        self.assertEqual(
            enriched["long_business_summary"], "Apple designs consumer technology products."
        )

    def test_enrichment_tickers_prefer_us_listing_over_legacy_isin_profile(self):
        details = {
            "ticker": "TL0",
            "official_symbol": None,
            "listings": [
                {
                    "ticker": "TL0",
                    "currency": "EUR",
                    "exchange": None,
                    "source": "legacy",
                    "is_primary": True,
                },
                {
                    "ticker": "TSLA",
                    "currency": "USD",
                    "exchange": None,
                    "source": "import_assets",
                    "is_primary": False,
                },
            ],
        }

        candidates = self.db.asset_profile_enrichment_tickers(
            details, request_ticker="US88160R1014"
        )

        self.assertEqual(candidates[:2], ["TSLA", "TL0"])
        self.assertNotIn("US88160R1014", candidates)

    def test_isin_context_prefers_official_symbol_listing_without_explicit_listing(self):
        session = self.db.get_session()
        try:
            asset = Asset(
                ticker="TL0",
                isin="US88160R1014",
                name="Tesla Inc",
                official_symbol="TSLA",
                exchange="NMS",
                currency="USD",
                quote_type="STOCK",
            )
            session.add(asset)
            session.flush()
            legacy_listing = AssetListing(
                asset_id=asset.id,
                ticker="TL0",
                exchange="",
                currency="EUR",
                is_primary=True,
                source="legacy",
            )
            usd_listing = AssetListing(
                asset_id=asset.id,
                ticker="TSLA",
                exchange="NMS",
                currency="USD",
                is_primary=False,
                source="import_assets",
            )
            session.add_all([legacy_listing, usd_listing])
            session.commit()
            usd_listing_id = usd_listing.id
        finally:
            session.close()

        details = self.db.get_asset_details(ticker="US88160R1014", isin="US88160R1014")

        self.assertEqual(details["ticker"], "TSLA")
        self.assertEqual(details["exchange"], "NMS")
        self.assertEqual(details["currency"], "USD")
        self.assertEqual(details["listing_id"], usd_listing_id)

    def test_bare_ticker_lookup_prefers_stock_over_same_ticker_etf(self):
        session = self.db.get_session()
        try:
            etf = Asset(
                ticker="TSLA",
                isin="XS2337093798",
                name="Leverage Shares 1x Tesla",
                quote_type="ETF",
            )
            stock = Asset(
                ticker="TSLA",
                isin="US88160R1014",
                name="Tesla Inc",
                quote_type="STOCK",
            )
            session.add_all([etf, stock])
            session.flush()
            session.add_all(
                [
                    AssetListing(
                        asset_id=etf.id,
                        ticker="TSLA",
                        exchange="",
                        currency="EUR",
                        is_primary=True,
                        source="import_assets",
                    ),
                    AssetListing(
                        asset_id=stock.id,
                        ticker="TSLA",
                        exchange="NMS",
                        currency="USD",
                        is_primary=False,
                        source="import_assets",
                    ),
                ]
            )
            session.commit()
        finally:
            session.close()

        details = self.db.get_asset_details(ticker="TSLA")

        self.assertEqual(details["isin"], "US88160R1014")
        self.assertEqual(details["name"], "Tesla Inc")
        self.assertEqual(details["currency"], "USD")

    def test_etf_profile_is_not_enriched_with_stock_metadata(self):
        session = self.db.get_session()
        try:
            asset = Asset(
                ticker="TSLA",
                isin="XS2337093798",
                name="Leverage Shares 1x Tesla",
                quote_type="ETF",
            )
            session.add(asset)
            session.flush()
            asset_id = asset.id
            session.commit()
        finally:
            session.close()

        updated = self.db.update_asset_profile_from_metadata(
            asset_id,
            {
                "display_name": "Tesla, Inc.",
                "symbol": "TSLA",
                "quoteType": "EQUITY",
                "sector": "Consumer Cyclical",
            },
        )

        self.assertFalse(updated)
        details = self.db.get_asset_details(isin="XS2337093798")
        self.assertIsNone(details["display_name"])
        self.assertIsNone(details["sector"])


if __name__ == "__main__":
    unittest.main()
