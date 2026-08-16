"""
tests/test_import_assets.py
────────────────────────────
Tests unitaires pour le pipeline d'import CSV ISIN-dédupliqué.

Couvre :
  - parse_csv            (TestParseCSV)
  - determine_exchange   (TestDetermineExchange)
  - determine_is_primary (TestDetermineIsPrimary)
  - AssetImporter        (TestAssetImporter)
  - ImportStats          (TestImportStats)
"""

import csv
import os
import tempfile
import types
import unittest
from pathlib import Path

from database.service import DatabaseService
from import_assets import (
    AssetImporter,
    CSVRow,
    ImportStats,
    determine_exchange,
    determine_is_primary,
    parse_csv,
)
from models import Asset, AssetListing, AssetMapping, Base

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_csv(tmp_dir: str, rows: list[dict], *, filename: str = "test.csv") -> Path:
    """Write a CSV file with standard headers and return its Path."""
    headers = ["name", "ticker", "isin", "productType", "currency"]
    path = Path(tmp_dir) / filename
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(**kwargs) -> dict:
    """Return a minimal valid CSV row dict, with overrideable defaults."""
    base = {
        "name": "Test Corp",
        "ticker": "TEST",
        "isin": "US0000000001",
        "productType": "STOCK",
        "currency": "USD",
    }
    base.update(kwargs)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Fixture mixin — SQLite in-memory DB
# ─────────────────────────────────────────────────────────────────────────────


class SqliteTestMixin(unittest.TestCase):
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


# ─────────────────────────────────────────────────────────────────────────────
# TestParseCSV
# ─────────────────────────────────────────────────────────────────────────────


class TestParseCSV(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def _parse(self, rows: list[dict]) -> tuple:
        path = _make_csv(self.tmp_dir, rows)
        return parse_csv(path)

    def test_valid_single_row(self):
        valid, dups, inv = self._parse([_row()])
        self.assertEqual(len(valid), 1)
        self.assertEqual(dups, 0)
        self.assertEqual(inv, 0)

    def test_dedup_identical_rows(self):
        """Two identical rows → 1 valid, 1 duplicate."""
        row = _row()
        valid, dups, inv = self._parse([row, row])
        self.assertEqual(len(valid), 1)
        self.assertEqual(dups, 1)
        self.assertEqual(inv, 0)

    def test_same_isin_different_currency_kept(self):
        """Same ISIN + ticker, different currency → 2 valid rows (distinct dedup_key)."""
        valid, dups, inv = self._parse(
            [
                _row(currency="USD"),
                _row(currency="EUR"),
            ]
        )
        self.assertEqual(len(valid), 2)
        self.assertEqual(dups, 0)

    def test_invalid_isin_skipped(self):
        valid, dups, inv = self._parse([_row(isin="INVALID")])
        self.assertEqual(len(valid), 0)
        self.assertEqual(inv, 1)

    def test_empty_name_skipped(self):
        valid, dups, inv = self._parse([_row(name="")])
        self.assertEqual(len(valid), 0)
        self.assertEqual(inv, 1)

    def test_invalid_product_type_skipped(self):
        valid, dups, inv = self._parse([_row(productType="FUND")])
        self.assertEqual(len(valid), 0)
        self.assertEqual(inv, 1)

    def test_invalid_currency_skipped(self):
        valid, dups, inv = self._parse([_row(currency="EU")])  # 2 chars, invalid
        self.assertEqual(len(valid), 0)
        self.assertEqual(inv, 1)

    def test_ticker_normalised_to_uppercase(self):
        valid, _, _ = self._parse([_row(ticker="aapl")])
        self.assertEqual(valid[0].ticker, "AAPL")

    def test_isin_normalised_to_uppercase(self):
        valid, _, _ = self._parse([_row(isin="us0378331005")])
        self.assertEqual(valid[0].isin, "US0378331005")

    def test_mixed_valid_and_invalid(self):
        valid, dups, inv = self._parse(
            [
                _row(),  # valid
                _row(isin="BAD"),  # invalid
                _row(currency="USD"),  # dup of first
                _row(ticker="AAPL", isin="US0378331005"),  # valid, different ISIN
            ]
        )
        self.assertEqual(len(valid), 2)
        self.assertEqual(inv, 1)
        self.assertEqual(dups, 1)


# ─────────────────────────────────────────────────────────────────────────────
# TestDetermineExchange
# ─────────────────────────────────────────────────────────────────────────────


class TestDetermineExchange(unittest.TestCase):
    def _row(self, ticker: str) -> CSVRow:
        return CSVRow(
            name="Test",
            ticker=ticker,
            isin="US0000000001",
            product_type="STOCK",
            currency="USD",
        )

    def test_paris_suffix(self):
        self.assertEqual(determine_exchange(self._row("AIR.PA")), "XPAR")

    def test_xetra_suffix(self):
        self.assertEqual(determine_exchange(self._row("BMW.DE")), "XETR")

    def test_no_suffix_returns_none(self):
        self.assertIsNone(determine_exchange(self._row("AAPL")))

    def test_amsterdam_suffix(self):
        self.assertEqual(determine_exchange(self._row("ASML.AS")), "XAMS")


# ─────────────────────────────────────────────────────────────────────────────
# TestDetermineIsPrimary
# ─────────────────────────────────────────────────────────────────────────────


class TestDetermineIsPrimary(unittest.TestCase):
    def _csv_row(self, currency: str = "USD") -> CSVRow:
        return CSVRow(
            name="Test",
            ticker="TEST",
            isin="US0000000001",
            product_type="STOCK",
            currency=currency,
        )

    def _listing(self, is_primary: bool):
        """Return a lightweight object that mimics the is_primary attribute."""
        return types.SimpleNamespace(is_primary=is_primary)

    def test_first_listing_is_primary(self):
        self.assertTrue(determine_is_primary(self._csv_row("USD"), []))

    def test_second_listing_not_primary_when_primary_exists(self):
        existing = [self._listing(is_primary=True)]
        self.assertFalse(determine_is_primary(self._csv_row("EUR"), existing))

    def test_primary_currency_gets_primary_when_no_primary(self):
        # USD is in PRIMARY_CURRENCIES; existing listing not primary
        existing = [self._listing(is_primary=False)]
        self.assertTrue(determine_is_primary(self._csv_row("USD"), existing))

    def test_non_primary_currency_stays_false_when_no_primary(self):
        existing = [self._listing(is_primary=False)]
        # SEK is NOT in PRIMARY_CURRENCIES
        self.assertFalse(determine_is_primary(self._csv_row("SEK"), existing))


# ─────────────────────────────────────────────────────────────────────────────
# TestAssetImporter
# ─────────────────────────────────────────────────────────────────────────────


class TestAssetImporter(SqliteTestMixin):
    def _csv_row(
        self,
        ticker: str = "AAPL",
        isin: str = "US0378331005",
        currency: str = "USD",
        product_type: str = "STOCK",
        name: str = "Apple Inc.",
    ) -> CSVRow:
        return CSVRow(
            name=name,
            ticker=ticker,
            isin=isin,
            product_type=product_type,
            currency=currency,
        )

    def test_single_row_creates_one_asset_one_listing(self):
        importer = AssetImporter(self.session)
        rows = [self._csv_row()]
        stats = importer.run(rows)

        self.assertEqual(self.session.query(Asset).count(), 1)
        self.assertEqual(self.session.query(AssetListing).count(), 1)
        self.assertEqual(stats.assets_created, 1)
        self.assertEqual(stats.listings_created, 1)

    def test_same_isin_two_currencies_creates_one_asset_two_listings(self):
        """The canonical multi-cotation scenario — one asset, two listings."""
        importer = AssetImporter(self.session)
        rows = [
            self._csv_row(currency="USD"),
            self._csv_row(currency="EUR"),
        ]
        stats = importer.run(rows)

        self.assertEqual(self.session.query(Asset).count(), 1)
        self.assertEqual(self.session.query(AssetListing).count(), 2)
        self.assertEqual(stats.assets_created, 1)
        self.assertEqual(stats.listings_created, 2)

    def test_idempotent_rerun_does_not_duplicate(self):
        """Running the same row twice must not create duplicate assets/listings."""
        rows = [self._csv_row()]
        AssetImporter(self.session).run(rows)
        self.session.expunge_all()  # clear identity map
        AssetImporter(self.session).run(rows)

        self.assertEqual(self.session.query(Asset).count(), 1)
        self.assertEqual(self.session.query(AssetListing).count(), 1)

    def test_default_mappings_created(self):
        """At least a YahooFinance mapping is created for a USD ticker."""
        importer = AssetImporter(self.session)
        rows = [self._csv_row(ticker="AAPL", currency="USD")]
        importer.run(rows)

        mappings = self.session.query(AssetMapping).all()
        provider_names = {m.provider_name for m in mappings}
        self.assertIn("YahooFinance", provider_names)

    def test_dry_run_does_not_persist(self):
        """dry_run=True must not commit rows — rollback leaves the DB empty."""
        importer = AssetImporter(self.session, dry_run=True)
        rows = [self._csv_row()]
        stats = importer.run(rows)

        # dry_run flushes within the open transaction but never commits;
        # rolling back confirms nothing was durably written.
        self.session.rollback()
        self.assertEqual(self.session.query(Asset).count(), 0)
        # stats still reflect what would have been written
        self.assertGreater(stats.assets_created + stats.listings_created, 0)

    def test_first_listing_is_primary(self):
        importer = AssetImporter(self.session)
        rows = [self._csv_row(currency="USD")]
        importer.run(rows)

        listing = self.session.query(AssetListing).one()
        self.assertTrue(listing.is_primary)

    def test_usd_listing_is_primary_when_eur_exists_without_primary(self):
        """USD listing becomes primary because USD ∈ PRIMARY_CURRENCIES."""
        importer = AssetImporter(self.session)
        # Insert EUR first manually without is_primary
        asset = Asset(
            ticker="AAPL", isin="US0378331005", name="Apple", is_active=True, quote_type="STOCK"
        )
        self.session.add(asset)
        self.session.flush()
        eur_listing = AssetListing(
            asset_id=asset.id,
            ticker="AAPL",
            exchange="",
            currency="EUR",
            is_primary=False,
            is_active=True,
            source="manual",
        )
        self.session.add(eur_listing)
        self.session.commit()

        rows = [self._csv_row(currency="USD")]
        importer.run(rows)

        usd_listing = self.session.query(AssetListing).filter_by(currency="USD").one()
        self.assertTrue(usd_listing.is_primary)

    def test_stats_track_listings_skipped(self):
        rows = [self._csv_row()]
        AssetImporter(self.session).run(rows)
        self.session.expunge_all()
        stats = AssetImporter(self.session).run(rows)
        # Second run: asset updated, listing skipped
        self.assertGreaterEqual(stats.listings_skipped, 1)

    def test_etf_with_multiple_currencies_and_tickers(self):
        """Reproduces the Degiro ETF multi-cotation pattern."""
        rows = [
            self._csv_row(
                ticker="GOVY",
                isin="IE00B3S5XW04",
                currency="EUR",
                product_type="ETF",
                name="SPDR GOVY",
            ),
            self._csv_row(
                ticker="GOVY",
                isin="IE00B3S5XW04",
                currency="CHF",
                product_type="ETF",
                name="SPDR GOVY",
            ),
            self._csv_row(
                ticker="GOVY2",
                isin="IE00B3S5XW04",
                currency="EUR",
                product_type="ETF",
                name="SPDR GOVY",
            ),
        ]
        stats = AssetImporter(self.session).run(rows)

        self.assertEqual(self.session.query(Asset).count(), 1)
        self.assertEqual(self.session.query(AssetListing).count(), 3)
        self.assertEqual(stats.assets_created, 1)
        self.assertEqual(stats.listings_created, 3)


# ─────────────────────────────────────────────────────────────────────────────
# TestImportStats
# ─────────────────────────────────────────────────────────────────────────────


class TestImportStats(unittest.TestCase):
    def test_default_values_are_zero(self):
        stats = ImportStats()
        self.assertEqual(stats.assets_created, 0)
        self.assertEqual(stats.listings_created, 0)
        self.assertEqual(stats.errors, 0)

    def test_print_summary_runs_without_error(self):
        stats = ImportStats(
            total_csv_rows=100,
            assets_created=10,
            listings_created=15,
            errors=0,
            duration_seconds=1.5,
        )
        import io
        import sys

        out = io.StringIO()
        sys.stdout = out
        try:
            stats.print_summary()
        finally:
            sys.stdout = sys.__stdout__
        self.assertIn("100", out.getvalue())
        self.assertIn("10", out.getvalue())


if __name__ == "__main__":
    unittest.main()
