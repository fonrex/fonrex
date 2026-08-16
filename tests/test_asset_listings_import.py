import os
import tempfile
import unittest
from unittest.mock import patch

import import_assets
from database.service import DatabaseService
from import_assets import (
    DEFAULT_LOGO_PATH,
    AssetImporter,
    CSVRow,
    download_logo,
    fetch_yfinance_data,
    get_google_ticker,
    resolve_input_file,
)
from models import Asset, AssetListing, AssetMapping, Base


def make_rows(raw_rows):
    """Build the current import DTOs and deduplicate on their public key."""
    rows = [
        CSVRow(
            name=row["name"],
            ticker=row["ticker"],
            isin=row["isin"],
            product_type=row["productType"],
            currency=row["currency"],
        )
        for row in raw_rows
    ]
    return list({row.dedup_key: row for row in rows}.values())


def run_import(session, rows):
    stats = AssetImporter(session).run(rows)
    return {"rows": len(rows), "stats": stats}


class AssetListingsImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db")
        self.db = DatabaseService(f"sqlite:///{self.tmp.name}")
        Base.metadata.create_all(self.db.engine)

    def tearDown(self):
        self.db.Session.remove()
        self.db.engine.dispose()
        self.tmp.close()

    def test_same_isin_with_multiple_tickers_and_currencies_creates_one_asset_and_many_listings(
        self,
    ):
        rows = make_rows(
            [
                {
                    "name": "State Street SPDR Bloomberg Euro Government Bond UCITS ETF",
                    "ticker": "GOVY.PA",
                    "isin": "IE00B3S5XW04",
                    "productType": "ETF",
                    "currency": "EUR",
                },
                {
                    "name": "State Street SPDR Bloomberg Euro Government Bond UCITS ETF",
                    "ticker": "GOVY.PA",
                    "isin": "IE00B3S5XW04",
                    "productType": "ETF",
                    "currency": "EUR",
                },
                {
                    "name": "State Street SPDR Bloomberg Euro Government Bond UCITS ETF",
                    "ticker": "GOVY.PA",
                    "isin": "IE00B3S5XW04",
                    "productType": "ETF",
                    "currency": "CHF",
                },
                {
                    "name": "State Street SPDR Bloomberg Euro Government Bond UCITS ETF",
                    "ticker": "SYBB.PA",
                    "isin": "IE00B3S5XW04",
                    "productType": "ETF",
                    "currency": "EUR",
                },
            ]
        )

        self.assertEqual(len(rows), 3)

        session = self.db.get_session()
        try:
            counters = run_import(session, rows)
            session.commit()

            self.assertEqual(counters["rows"], 3)
            self.assertEqual(session.query(Asset).count(), 1)
            self.assertEqual(session.query(AssetListing).count(), 3)

            asset = session.query(Asset).one()
            self.assertEqual(asset.isin, "IE00B3S5XW04")
            self.assertEqual(asset.quote_type, "ETF")

            listings = (
                session.query(AssetListing)
                .order_by(AssetListing.ticker, AssetListing.currency)
                .all()
            )
            self.assertEqual(
                [(item.ticker, item.currency) for item in listings],
                [("GOVY.PA", "CHF"), ("GOVY.PA", "EUR"), ("SYBB.PA", "EUR")],
            )
            self.assertEqual(session.query(AssetMapping).count(), 6)
        finally:
            session.close()

    def test_asset_context_resolves_currency_specific_listing_and_mappings(self):
        rows = make_rows(
            [
                {
                    "name": "State Street SPDR Bloomberg Euro Government Bond UCITS ETF",
                    "ticker": "GOVY.PA",
                    "isin": "IE00B3S5XW04",
                    "productType": "ETF",
                    "currency": "EUR",
                },
                {
                    "name": "State Street SPDR Bloomberg Euro Government Bond UCITS ETF",
                    "ticker": "GOVY.PA",
                    "isin": "IE00B3S5XW04",
                    "productType": "ETF",
                    "currency": "CHF",
                },
            ]
        )

        session = self.db.get_session()
        try:
            run_import(session, rows)
            session.commit()
        finally:
            session.close()

        context = self.db.get_asset_context(ticker="GOVY.PA", currency="CHF")

        self.assertIsNotNone(context)
        self.assertEqual(context["details"]["isin"], "IE00B3S5XW04")
        self.assertEqual(context["details"]["ticker"], "GOVY.PA")
        self.assertEqual(context["details"]["currency"], "CHF")
        self.assertEqual(len(context["details"]["listings"]), 2)
        self.assertIn("yahoofinance", context["mappings"])
        self.assertEqual(
            context["mappings"]["yahoofinance"].asset_listing_id, context["details"]["listing_id"]
        )

    def test_google_mapping_requires_a_known_exchange(self):
        self.assertIsNone(get_google_ticker("GOVY"))
        self.assertEqual(get_google_ticker("GOVY", "EPA"), "GOVY:EPA")

    def test_resolve_input_file_uses_configured_data_dir_for_simple_names(self):
        resolved = resolve_input_file("etf.csv", base_dir="/app/data/isin_data")

        self.assertEqual(resolved, "/app/data/isin_data/etf.csv")

    def test_resolve_input_file_accepts_existing_relative_paths(self):
        resolved = resolve_input_file("tests/data/seed_assets.csv")

        self.assertEqual(resolved, os.path.abspath("tests/data/seed_assets.csv"))


    def test_yfinance_data_resolves_yahoo_symbol_from_isin_before_ticker(self):
        calls = []

        class FakeTicker:
            def __init__(self, symbol):
                calls.append(symbol)
                self.info = {
                    "symbol": symbol,
                    "shortName": "CREDIT AGRICOLE",
                    "longName": "Crédit Agricole S.A.",
                    "exchange": "PAR",
                    "currency": "EUR",
                    "sector": "Financial Services",
                    "industry": "Banks-Regional",
                    "quoteType": "EQUITY",
                    "longBusinessSummary": "French banking group.",
                }

        with (
            patch.object(import_assets.yf, "Ticker", FakeTicker),
            patch(
                "import_assets.fetch_yahoo_search_quote",
                return_value={
                    "exchange": "PAR",
                    "shortname": "CREDIT AGRICOLE",
                    "quoteType": "EQUITY",
                    "symbol": "ACA.PA",
                    "longname": "Crédit Agricole S.A.",
                    "sector": "Financial Services",
                    "industry": "Banks-Regional",
                    "isYahooFinance": True,
                },
            ),
        ):
            data = fetch_yfinance_data("ACA", isin="FR0000045072")

        self.assertEqual(calls, ["ACA.PA"])
        self.assertEqual(data["symbol"], "ACA.PA")
        self.assertEqual(data["official_symbol"], "ACA.PA")
        self.assertEqual(data["isin"], "FR0000045072")
        self.assertEqual(data["exchange"], "PAR")

    def test_yahoo_search_quote_selection_skips_non_finance_low_score_rows(self):
        quote = import_assets._select_yahoo_search_quote(
            [
                {
                    "symbol": "OTHER",
                    "quoteType": "EQUITY",
                    "score": 10,
                    "isYahooFinance": True,
                },
                {
                    "symbol": "ACA.PA",
                    "quoteType": "EQUITY",
                    "score": 20005,
                    "isYahooFinance": True,
                },
            ],
            "FR0000045072",
        )

        self.assertEqual(quote["symbol"], "ACA.PA")


class LogoFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_download_logo_returns_default_logo_when_provider_has_no_match(self):
        class FakeResponse:
            status = 404

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def read(self):
                return b""

        class FakeSession:
            def get(self, *args, **kwargs):
                return FakeResponse()

        old_static_dir = import_assets.STATIC_DIR
        with tempfile.TemporaryDirectory() as tmp:
            import_assets.STATIC_DIR = tmp
            try:
                logo_path = await download_logo(FakeSession(), "MISSING", "NYSE")
            finally:
                import_assets.STATIC_DIR = old_static_dir

        self.assertEqual(logo_path, DEFAULT_LOGO_PATH)


if __name__ == "__main__":
    unittest.main()
