"""
Unit tests of YFinanceEnricher.
Uses yfinance mocks and in-memory SQLite.
"""

import asyncio
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from database.assets import AssetRepository
from financials.enrichment.yfinance_enricher import (
    YFinanceEnricher,
    _load_info,
    _safe_date,
    _to_decimal,
    _to_int,
    clean_yahoo_isin,
    profile_metadata,
)
from models import (
    AnalystRatings,
    Asset,
    AssetListing,
    Base,
    EarningsHistory,
    FinancialStatement,
    FundamentalsHighlights,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine():
    """In-memory SQLite shared between threads for the async tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _make_db_service(engine):
    """A DatabaseService mock returning SQLite sessions."""
    Session = scoped_session(sessionmaker(bind=engine))
    db_service = MagicMock()
    db_service.get_session.side_effect = lambda: Session()
    return db_service, Session


def _make_test_asset(session):
    asset = Asset(id=1, ticker="AIR.PA", name="Airbus SE", isin="NL0000235190")
    session.add(asset)
    session.commit()
    return asset


def _make_mock_ticker():
    """A complete yf.Ticker mock with every property."""
    ticker = MagicMock()

    # .info
    ticker.info = {
        "marketCap": 107400000000,
        "enterpriseValue": 120000000000,
        "trailingPE": 28.4,
        "forwardPE": 24.1,
        "priceToBook": 6.2,
        "priceToSalesTrailing12Months": 1.64,
        "pegRatio": 1.8,
        "enterpriseToEbitda": 18.7,
        "enterpriseToRevenue": 1.83,
        "returnOnEquity": 0.421,
        "returnOnAssets": 0.038,
        "profitMargins": 0.052,
        "operatingMargins": 0.068,
        "grossMargins": 0.15,
        "trailingEps": 4.87,
        "forwardEps": 5.2,
        "bookValue": 25.3,
        "revenuePerShare": 83.4,
        # In percent, as yfinance publishes it: 1.8 dividend for a price of 100.
        "dividendYield": 1.8,
        "dividendRate": 1.80,
        "payoutRatio": 0.37,
        "beta": 1.12,
        "fiftyTwoWeekHigh": 172.40,
        "fiftyTwoWeekLow": 118.30,
        "fiftyDayAverage": 155.0,
        "twoHundredDayAverage": 145.0,
        "sharesOutstanding": 777000000,
        "floatShares": 750000000,
        "heldPercentInsiders": 0.0023,
        "heldPercentInstitutions": 0.612,
    }

    # .income_stmt (annual)
    dates_annual = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    income_data = {
        dates_annual[0]: {
            "Total Revenue": 65400000000,
            "Gross Profit": 9800000000,
            "EBITDA": 7450000000,
            "Operating Income": 5200000000,
            "Net Income": 3400000000,
            "Basic EPS": 4.90,
            "Diluted EPS": 4.87,
        },
        dates_annual[1]: {
            "Total Revenue": 55000000000,
            "Gross Profit": 8200000000,
            "EBITDA": 6000000000,
            "Operating Income": 4100000000,
            "Net Income": 2800000000,
            "Basic EPS": 3.60,
            "Diluted EPS": 3.58,
        },
    }
    ticker.income_stmt = pd.DataFrame(income_data)
    ticker.quarterly_income_stmt = pd.DataFrame()  # vide

    # .balance_sheet
    balance_data = {
        dates_annual[0]: {
            "Total Assets": 130000000000,
            "Total Liabilities Net Minority Interest": 110000000000,
            "Total Equity Gross Minority Interest": 20000000000,
            "Total Debt": 25000000000,
            "Cash And Cash Equivalents": 12000000000,
        },
    }
    ticker.balance_sheet = pd.DataFrame(balance_data)
    ticker.quarterly_balance_sheet = pd.DataFrame()

    # .cashflow
    cashflow_data = {
        dates_annual[0]: {
            "Operating Cash Flow": 8000000000,
            "Investing Cash Flow": -3000000000,
            "Financing Cash Flow": -2000000000,
            "Free Cash Flow": 5000000000,
            "Capital Expenditure": -3000000000,
        },
    }
    ticker.cashflow = pd.DataFrame(cashflow_data)
    ticker.quarterly_cashflow = pd.DataFrame()

    # .earnings_history: the shape of yfinance 1.7, quarter end dates as index
    eh_data = pd.DataFrame(
        {
            "epsActual": [0.98, 1.21],
            "epsEstimate": [1.02, 1.15],
            "epsDifference": [-0.04, 0.06],
            "surprisePercent": [-0.0392, 0.0522],
        },
        index=pd.DatetimeIndex(["2024-09-30", "2024-12-31"], name="quarter"),
    )
    ticker.earnings_history = eh_data

    # .analyst_price_targets (dict)
    ticker.analyst_price_targets = {
        "current": 162.0,
        "low": 130.0,
        "high": 195.0,
        "mean": 165.0,
        "numberOfAnalystOpinions": 24,
    }

    # .recommendations
    recs_data = pd.DataFrame(
        [
            {
                "strongBuy": 8,
                "buy": 10,
                "hold": 5,
                "sell": 1,
                "strongSell": 0,
            },
        ]
    )
    ticker.recommendations = recs_data

    return ticker


class _DatabaseTestCase(unittest.TestCase):
    """Dispose every in-memory SQLite resource created by an enricher test."""

    def tearDown(self):
        self.Session.remove()
        self.engine.dispose()


# ---------------------------------------------------------------------------
# Test utility functions
# ---------------------------------------------------------------------------


class TestUtilityFunctions(unittest.TestCase):
    def test_to_decimal_valid(self):
        self.assertEqual(_to_decimal(42), Decimal("42"))
        self.assertEqual(_to_decimal(3.14), Decimal("3.14"))
        self.assertEqual(_to_decimal("100"), Decimal("100"))

    def test_to_decimal_none(self):
        self.assertIsNone(_to_decimal(None))

    def test_to_decimal_nan(self):
        self.assertIsNone(_to_decimal(float("nan")))
        self.assertIsNone(_to_decimal(float("inf")))

    def test_to_int_valid(self):
        self.assertEqual(_to_int(42), 42)
        self.assertEqual(_to_int(3.7), 3)

    def test_to_int_none(self):
        self.assertIsNone(_to_int(None))
        self.assertIsNone(_to_int(float("nan")))

    def test_safe_date_timestamp(self):
        ts = pd.Timestamp("2024-12-31")
        self.assertEqual(_safe_date(ts), date(2024, 12, 31))

    def test_safe_date_datetime(self):
        dt = datetime(2024, 6, 15, 10, 30)
        self.assertEqual(_safe_date(dt), date(2024, 6, 15))

    def test_safe_date_none(self):
        self.assertIsNone(_safe_date(None))

    def test_safe_date_reads_yahoo_epoch_seconds(self):
        # Yahoo's info dates are seconds since 1970; they used to give 1970-01-01.
        self.assertEqual(_safe_date(1786406400), date(2026, 8, 11))
        self.assertEqual(_safe_date(1786406400.0), date(2026, 8, 11))
        self.assertEqual(_safe_date(np.int64(1789430400)), date(2026, 9, 15))

    def test_load_info_keeps_a_usable_info(self):
        info = {"marketCap": 1, "currency": "EUR"}
        self.assertEqual(_load_info(MagicMock(info=info)), info)

    def test_load_info_without_data_is_empty(self):
        for info in (None, {}, {"trailingPegRatio": None}):
            self.assertEqual(_load_info(MagicMock(info=info)), {})

    def test_load_info_that_fails_is_empty(self):
        class Broken:
            @property
            def info(self):
                raise RuntimeError("Yahoo is down")

        self.assertEqual(_load_info(Broken()), {})

    def test_safe_date_odd_numbers_are_unknown(self):
        for value in (0, -1, float("nan"), float("inf"), 10**20, True):
            with self.subTest(value=value):
                self.assertIsNone(_safe_date(value))


class TestProfileMetadata(unittest.TestCase):
    def test_profile_fields_are_read_from_info(self):
        metadata = profile_metadata(
            {
                "symbol": "AIR.PA",
                "shortName": "AIRBUS",
                "longName": "Airbus SE",
                "exchange": "PAR",
                "currency": "EUR",
                "sector": "Industrials",
                "industry": "Aerospace & Defense",
                "longBusinessSummary": "Airbus designs aircraft.",
                "website": "https://www.airbus.com",
                "country": "France",
                "city": "Leiden",
                "quoteType": "EQUITY",
                "isin": " nl0000235190 ",
            }
        )

        self.assertEqual(metadata["name"], "AIRBUS")
        self.assertEqual(metadata["display_name"], "Airbus SE")
        self.assertEqual(metadata["official_symbol"], "AIR.PA")
        self.assertEqual(metadata["exchange"], "PAR")
        self.assertEqual(metadata["sector"], "Industrials")
        self.assertEqual(metadata["industry"], "Aerospace & Defense")
        self.assertEqual(metadata["long_business_summary"], "Airbus designs aircraft.")
        self.assertEqual(metadata["ir_website"], "https://www.airbus.com")
        self.assertEqual(metadata["quote_type"], "EQUITY")
        self.assertEqual(metadata["isin"], "NL0000235190")
        self.assertEqual(metadata["profile"]["city"], "Leiden")

    def test_empty_info_gives_no_metadata(self):
        self.assertEqual(profile_metadata({}), {})

    def test_isin_placeholders_are_no_isin(self):
        for value in ("-", "N/A", "none", "", None, 42):
            with self.subTest(value=value):
                self.assertIsNone(clean_yahoo_isin(value))


# ---------------------------------------------------------------------------
# Test _fetch_highlights
# ---------------------------------------------------------------------------


class TestFetchHighlights(_DatabaseTestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.db_service, self.Session = _make_db_service(self.engine)
        session = self.Session()
        _make_test_asset(session)
        session.close()
        self.enricher = YFinanceEnricher(self.db_service)

    def test_highlights_inserted(self):
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_highlights(1, mock_ticker.info)

        session = self.Session()
        highlight = session.query(FundamentalsHighlights).filter_by(asset_id=1).first()
        self.assertIsNotNone(highlight)
        self.assertEqual(float(highlight.market_cap), 107400000000.0)
        self.assertEqual(float(highlight.pe_ratio), 28.4)
        # Stored as a ratio: the valuation multiplies it by the price.
        self.assertAlmostEqual(float(highlight.dividend_yield), 0.018)
        self.assertEqual(float(highlight.roe), 0.421)
        self.assertEqual(highlight.shares_outstanding, 777000000)
        session.close()

    def test_highlight_dates_are_read_from_epoch_seconds(self):
        mock_ticker = _make_mock_ticker()
        mock_ticker.info.update(
            exDividendDate=1786406400, dividendDate=1786665600, dateShortInterest=1789430400
        )
        self.enricher._fetch_highlights(1, mock_ticker.info)

        highlight = self.Session().query(FundamentalsHighlights).filter_by(asset_id=1).one()
        self.assertEqual(highlight.dividend_ex_date, date(2026, 8, 11))
        self.assertEqual(highlight.dividend_pay_date, date(2026, 8, 14))
        self.assertEqual(highlight.shares_short_date, date(2026, 9, 15))

    def test_highlights_upsert(self):
        """The upsert must update, not duplicate."""
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_highlights(1, mock_ticker.info)
        # Change and run again
        mock_ticker.info["marketCap"] = 200000000000
        self.enricher._fetch_highlights(1, mock_ticker.info)

        session = self.Session()
        count = session.query(FundamentalsHighlights).filter_by(asset_id=1).count()
        self.assertEqual(count, 1)
        highlight = session.query(FundamentalsHighlights).filter_by(asset_id=1).first()
        self.assertEqual(float(highlight.market_cap), 200000000000.0)
        session.close()


# ---------------------------------------------------------------------------
# Test _fetch_statements
# ---------------------------------------------------------------------------


class TestFetchStatements(_DatabaseTestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.db_service, self.Session = _make_db_service(self.engine)
        session = self.Session()
        _make_test_asset(session)
        session.close()
        self.enricher = YFinanceEnricher(self.db_service)

    def test_statements_inserted(self):
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_statements(1, mock_ticker, mock_ticker.info)

        session = self.Session()
        # 2 income annual + 1 balance + 1 cashflow = 4
        stmts = session.query(FinancialStatement).filter_by(asset_id=1).all()
        self.assertGreaterEqual(len(stmts), 4)

        # Check one income entry
        income_2024 = (
            session.query(FinancialStatement)
            .filter_by(
                asset_id=1,
                statement_type="income",
                period_type="annual",
                period_end=date(2024, 12, 31),
            )
            .first()
        )
        self.assertIsNotNone(income_2024)
        self.assertEqual(float(income_2024.revenue), 65400000000.0)
        self.assertEqual(float(income_2024.net_income), 3400000000.0)
        session.close()

    def test_statements_empty_df(self):
        """Must not crash with empty DataFrames."""
        mock_ticker = MagicMock()
        mock_ticker.income_stmt = pd.DataFrame()
        mock_ticker.quarterly_income_stmt = pd.DataFrame()
        mock_ticker.balance_sheet = pd.DataFrame()
        mock_ticker.quarterly_balance_sheet = pd.DataFrame()
        mock_ticker.cashflow = pd.DataFrame()
        mock_ticker.quarterly_cashflow = pd.DataFrame()

        self.enricher._fetch_statements(1, mock_ticker, {})

        session = self.Session()
        count = session.query(FinancialStatement).filter_by(asset_id=1).count()
        self.assertEqual(count, 0)
        session.close()

    def test_statements_upsert(self):
        """The upsert must not create duplicates."""
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_statements(1, mock_ticker, mock_ticker.info)
        self.enricher._fetch_statements(1, mock_ticker, mock_ticker.info)

        session = self.Session()
        # Must have exactly the same number of entries
        income_count = (
            session.query(FinancialStatement)
            .filter_by(
                asset_id=1,
                statement_type="income",
                period_type="annual",
            )
            .count()
        )
        self.assertEqual(income_count, 2)  # 2 years
        session.close()


# ---------------------------------------------------------------------------
# Test _fetch_earnings
# ---------------------------------------------------------------------------


class TestFetchEarnings(_DatabaseTestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.db_service, self.Session = _make_db_service(self.engine)
        session = self.Session()
        _make_test_asset(session)
        session.close()
        self.enricher = YFinanceEnricher(self.db_service)

    def test_earnings_inserted(self):
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_earnings(1, mock_ticker)

        session = self.Session()
        earnings = session.query(EarningsHistory).filter_by(asset_id=1).all()
        self.assertEqual(len(earnings), 2)

        q4 = (
            session.query(EarningsHistory)
            .filter_by(
                asset_id=1,
                period="2024Q4",
            )
            .first()
        )
        self.assertIsNotNone(q4)
        self.assertEqual(float(q4.eps_actual), 1.21)
        self.assertEqual(float(q4.eps_estimate), 1.15)
        self.assertEqual(float(q4.surprise), 0.06)
        # A ratio, as Yahoo gives it
        self.assertEqual(float(q4.surprise_pct), 0.0522)
        self.assertEqual(q4.period_end, date(2024, 12, 31))
        q3 = session.query(EarningsHistory).filter_by(asset_id=1, period="2024Q3").first()
        self.assertEqual(q3.period_end, date(2024, 9, 30))
        self.assertEqual(float(q3.surprise_pct), -0.0392)
        session.close()

    def test_earnings_updated_on_a_second_fetch(self):
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_earnings(1, mock_ticker)
        mock_ticker.earnings_history = mock_ticker.earnings_history.assign(
            epsActual=[0.99, 1.25]
        )
        self.enricher._fetch_earnings(1, mock_ticker)

        session = self.Session()
        earnings = session.query(EarningsHistory).filter_by(asset_id=1).all()
        self.assertEqual(len(earnings), 2)
        q4 = session.query(EarningsHistory).filter_by(asset_id=1, period="2024Q4").one()
        self.assertEqual(float(q4.eps_actual), 1.25)
        session.close()

    def test_rows_without_quarter_date_are_reported(self):
        """Rows Yahoo gives but that cannot be dated are not dropped silently."""
        mock_ticker = MagicMock()
        mock_ticker.earnings_history = pd.DataFrame(
            {"epsActual": [1.0], "epsEstimate": [0.9]}, index=pd.Index([None], name="quarter")
        )
        with self.assertLogs("financials.enrichment.yfinance_enricher", "WARNING") as logs:
            self.enricher._fetch_earnings(1, mock_ticker)

        self.assertIn("nothing stored", logs.output[0])
        session = self.Session()
        self.assertEqual(session.query(EarningsHistory).filter_by(asset_id=1).count(), 0)
        session.close()

    def test_earnings_none(self):
        """Must not crash when earnings_history is None."""
        mock_ticker = MagicMock()
        mock_ticker.earnings_history = None
        self.enricher._fetch_earnings(1, mock_ticker)

        session = self.Session()
        count = session.query(EarningsHistory).filter_by(asset_id=1).count()
        self.assertEqual(count, 0)
        session.close()


# ---------------------------------------------------------------------------
# Test _fetch_ratings
# ---------------------------------------------------------------------------


class TestFetchRatings(_DatabaseTestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.db_service, self.Session = _make_db_service(self.engine)
        session = self.Session()
        _make_test_asset(session)
        session.close()
        self.enricher = YFinanceEnricher(self.db_service)

    def test_ratings_inserted(self):
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_ratings(1, mock_ticker)

        session = self.Session()
        rating = session.query(AnalystRatings).filter_by(asset_id=1).first()
        self.assertIsNotNone(rating)
        self.assertEqual(float(rating.target_mean), 162.0)
        self.assertEqual(float(rating.target_low), 130.0)
        self.assertEqual(float(rating.target_high), 195.0)
        self.assertEqual(rating.nb_analysts, 24)
        self.assertEqual(rating.strong_buy, 8)
        self.assertEqual(rating.buy, 10)
        self.assertEqual(rating.hold, 5)
        self.assertEqual(rating.sell, 1)
        self.assertEqual(rating.strong_sell, 0)
        session.close()

    def test_ratings_consensus(self):
        """The consensus is the majority vote."""
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_ratings(1, mock_ticker)

        session = self.Session()
        rating = session.query(AnalystRatings).filter_by(asset_id=1).first()
        # 10 buy > 8 strong buy > 5 hold
        self.assertEqual(rating.consensus, "Buy")
        session.close()

    def test_ratings_no_data(self):
        """Must not crash without data."""
        mock_ticker = MagicMock()
        mock_ticker.analyst_price_targets = None
        mock_ticker.recommendations = None
        self.enricher._fetch_ratings(1, mock_ticker)

        session = self.Session()
        count = session.query(AnalystRatings).filter_by(asset_id=1).count()
        self.assertEqual(count, 0)
        session.close()


# ---------------------------------------------------------------------------
# Test enrich() (orchestrator)
# ---------------------------------------------------------------------------


class TestEnrich(_DatabaseTestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.db_service, self.Session = _make_db_service(self.engine)
        session = self.Session()
        _make_test_asset(session)
        session.close()
        self.enricher = YFinanceEnricher(self.db_service)

    @patch("financials.enrichment.yfinance_enricher.yf.Ticker")
    def test_enrich_full(self, mock_yf_ticker_class):
        mock_yf_ticker_class.return_value = _make_mock_ticker()

        result = asyncio.run(self.enricher.enrich(1, "AIR.PA"))

        self.assertTrue(result["profile"])
        self.assertTrue(result["highlights"])
        self.assertTrue(result["statements"])
        self.assertTrue(result["earnings"])
        self.assertTrue(result["ratings"])
        self.assertEqual(len(result["errors"]), 0)

    @patch("financials.enrichment.yfinance_enricher.yf.Ticker")
    def test_enrich_partial_failure(self, mock_yf_ticker_class):
        """A failing method must not block the others."""
        mock_ticker = _make_mock_ticker()
        mock_yf_ticker_class.return_value = mock_ticker

        # Patch _fetch_earnings so that it raises
        original_fetch_earnings = self.enricher._fetch_earnings

        def failing_fetch_earnings(asset_id, t):
            raise RuntimeError("Simulated API error")

        self.enricher._fetch_earnings = failing_fetch_earnings

        result = asyncio.run(self.enricher.enrich(1, "AIR.PA"))

        self.assertTrue(result["highlights"])
        self.assertTrue(result["statements"])
        self.assertFalse(result["earnings"])
        self.assertTrue(result["ratings"])
        self.assertGreater(len(result["errors"]), 0)

        # Restore
        self.enricher._fetch_earnings = original_fetch_earnings

    @patch("financials.enrichment.yfinance_enricher.yf.Ticker")
    def test_info_is_read_once_for_every_step(self, mock_yf_ticker_class):
        """yfinance 1.7 gives parallel readers of .info one answer, the others get {}."""
        mock_ticker = _make_mock_ticker()
        info = mock_ticker.info
        info["financialCurrency"] = "EUR"
        info["sector"] = "Industrials"
        reads = []

        def read_info(_):
            reads.append(1)
            return info if len(reads) == 1 else {}

        type(mock_ticker).info = property(read_info)
        mock_yf_ticker_class.return_value = mock_ticker

        result = asyncio.run(self.enricher.enrich(1, "AIR.PA"))

        self.assertEqual(len(reads), 1)
        self.assertEqual(result["skipped"], {})
        session = self.Session()
        self.assertEqual(session.query(FundamentalsHighlights).filter_by(asset_id=1).count(), 1)
        currencies = {row.currency for row in session.query(FinancialStatement)}
        self.assertEqual(currencies, {"EUR"})
        self.assertEqual(session.get(Asset, 1).gic_sector, "Industrials")
        session.close()

    def _store_profiles(self):
        """The real profile write, on the test database."""
        repository = AssetRepository(self.engine, self.Session)
        self.db_service.update_asset_profile_from_metadata = (
            repository.update_asset_profile_from_metadata
        )
        session = self.Session()
        # As the CSV import stores it: catalogue ticker, no exchange.
        session.add(
            AssetListing(id=10, asset_id=1, ticker="AIR", exchange="", currency="", is_primary=True)
        )
        asset = session.get(Asset, 1)
        asset.sector = None
        asset.display_name = "Airbus (catalogue)"
        session.commit()
        session.close()

    @patch("financials.enrichment.yfinance_enricher.yf.Ticker")
    def test_profile_is_stored_for_the_verified_listing(self, mock_yf_ticker_class):
        self._store_profiles()
        mock_ticker = _make_mock_ticker()
        mock_ticker.info.update(
            {
                "symbol": "AIR.PA",
                "longName": "Airbus SE",
                "exchange": "PAR",
                "currency": "EUR",
                "sector": "Industrials",
                "industry": "Aerospace & Defense",
                "longBusinessSummary": "Airbus designs aircraft.",
                "isin": "NL0000235190",
            }
        )
        mock_yf_ticker_class.return_value = mock_ticker

        result = asyncio.run(self.enricher.enrich(1, "AIR.PA", 10))

        self.assertTrue(result["profile"])
        session = self.Session()
        asset = session.get(Asset, 1)
        self.assertEqual(asset.sector, "Industrials")
        self.assertEqual(asset.industry, "Aerospace & Defense")
        self.assertEqual(asset.long_business_summary, "Airbus designs aircraft.")
        self.assertEqual(asset.display_name, "Airbus (catalogue)")  # existing values are kept
        # The symbol was verified for listing 10: its ticker need not be Yahoo's.
        listing = session.get(AssetListing, 10)
        self.assertEqual((listing.exchange, listing.currency), ("PAR", "EUR"))
        session.close()

    @patch("financials.enrichment.yfinance_enricher.yf.Ticker")
    def test_profile_of_another_instrument_is_not_stored(self, mock_yf_ticker_class):
        self._store_profiles()
        mock_ticker = _make_mock_ticker()
        mock_ticker.info.update({"symbol": "AIR.PA", "sector": "Utilities", "isin": "FR0000000000"})
        mock_yf_ticker_class.return_value = mock_ticker

        asyncio.run(self.enricher.enrich(1, "AIR.PA", 10))

        session = self.Session()
        self.assertIsNone(session.get(Asset, 1).sector)
        self.assertEqual(session.get(AssetListing, 10).exchange, "")
        session.close()

    @patch("financials.enrichment.yfinance_enricher.yf.Ticker")
    def test_empty_info_is_reported_as_skipped(self, mock_yf_ticker_class):
        mock_ticker = _make_mock_ticker()
        mock_ticker.info = {"trailingPegRatio": None}
        mock_yf_ticker_class.return_value = mock_ticker

        result = asyncio.run(self.enricher.enrich(1, "AIR.PA"))

        self.assertFalse(result["profile"])
        self.assertFalse(result["highlights"])
        self.assertFalse(result["gics"])
        self.assertFalse(result["solvency"])
        self.assertEqual(
            result["skipped"],
            {
                "profile": "Yahoo returned no info",
                "highlights": "Yahoo returned no info",
                "gics": "Yahoo returned no info",
                "statements_currency": "Yahoo returned no info",
            },
        )
        self.assertEqual(result["errors"], [])
        # The steps that do not need info still run.
        self.assertTrue(result["statements"])
        self.assertTrue(result["earnings"])
        session = self.Session()
        self.assertEqual(session.query(FundamentalsHighlights).filter_by(asset_id=1).count(), 0)
        self.assertGreater(session.query(FinancialStatement).filter_by(asset_id=1).count(), 0)
        self.assertIsNone(session.get(Asset, 1).gic_sector)
        session.close()


if __name__ == "__main__":
    unittest.main()
