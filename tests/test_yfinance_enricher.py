"""
Tests unitaires pour YFinanceEnricher.
Utilise des mocks yfinance et SQLite in-memory.
"""

import asyncio
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from financials.enrichment.yfinance_enricher import (
    YFinanceEnricher,
    _safe_date,
    _to_decimal,
    _to_int,
)
from models import (
    AnalystRatings,
    Asset,
    Base,
    EarningsHistory,
    FinancialStatement,
    FundamentalsHighlights,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine():
    """SQLite in-memory partagé entre threads pour les tests async."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _make_db_service(engine):
    """Crée un mock de DatabaseService qui retourne des sessions SQLite."""
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
    """Crée un mock complet de yf.Ticker avec toutes les propriétés."""
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
        "dividendYield": 0.018,
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

    # .income_stmt (annuel)
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

    # .earnings_history
    eh_data = pd.DataFrame(
        [
            {
                "quarter": "2024Q4",
                "epsActual": 1.21,
                "epsEstimate": 1.15,
                "surprisePercent": 5.2,
            },
            {
                "quarter": "2024Q3",
                "epsActual": 0.98,
                "epsEstimate": 1.02,
                "surprisePercent": -3.9,
            },
        ]
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
        self.enricher._fetch_highlights(1, mock_ticker)

        session = self.Session()
        highlight = session.query(FundamentalsHighlights).filter_by(asset_id=1).first()
        self.assertIsNotNone(highlight)
        self.assertEqual(float(highlight.market_cap), 107400000000.0)
        self.assertEqual(float(highlight.pe_ratio), 28.4)
        self.assertEqual(float(highlight.roe), 0.421)
        self.assertEqual(highlight.shares_outstanding, 777000000)
        session.close()

    def test_highlights_upsert(self):
        """L'upsert doit mettre à jour, pas dupliquer."""
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_highlights(1, mock_ticker)
        # Modifier et refaire
        mock_ticker.info["marketCap"] = 200000000000
        self.enricher._fetch_highlights(1, mock_ticker)

        session = self.Session()
        count = session.query(FundamentalsHighlights).filter_by(asset_id=1).count()
        self.assertEqual(count, 1)
        highlight = session.query(FundamentalsHighlights).filter_by(asset_id=1).first()
        self.assertEqual(float(highlight.market_cap), 200000000000.0)
        session.close()

    def test_highlights_empty_info(self):
        """Ne doit pas crasher avec un info vide."""
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        self.enricher._fetch_highlights(1, mock_ticker)  # No exception

        session = self.Session()
        count = session.query(FundamentalsHighlights).filter_by(asset_id=1).count()
        self.assertEqual(count, 0)
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
        self.enricher._fetch_statements(1, mock_ticker)

        session = self.Session()
        # 2 income annual + 1 balance + 1 cashflow = 4
        stmts = session.query(FinancialStatement).filter_by(asset_id=1).all()
        self.assertGreaterEqual(len(stmts), 4)

        # Vérifier une entrée income spécifique
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
        """Ne doit pas crasher avec des DataFrames vides."""
        mock_ticker = MagicMock()
        mock_ticker.income_stmt = pd.DataFrame()
        mock_ticker.quarterly_income_stmt = pd.DataFrame()
        mock_ticker.balance_sheet = pd.DataFrame()
        mock_ticker.quarterly_balance_sheet = pd.DataFrame()
        mock_ticker.cashflow = pd.DataFrame()
        mock_ticker.quarterly_cashflow = pd.DataFrame()

        self.enricher._fetch_statements(1, mock_ticker)

        session = self.Session()
        count = session.query(FinancialStatement).filter_by(asset_id=1).count()
        self.assertEqual(count, 0)
        session.close()

    def test_statements_upsert(self):
        """L'upsert ne doit pas créer de doublons."""
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_statements(1, mock_ticker)
        self.enricher._fetch_statements(1, mock_ticker)

        session = self.Session()
        # Doit avoir exactement le même nombre d'entrées
        income_count = (
            session.query(FinancialStatement)
            .filter_by(
                asset_id=1,
                statement_type="income",
                period_type="annual",
            )
            .count()
        )
        self.assertEqual(income_count, 2)  # 2 années
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
        self.assertEqual(float(q4.surprise_pct), 5.2)
        session.close()

    def test_earnings_none(self):
        """Ne doit pas crasher quand earnings_history est None."""
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
        """Le consensus doit être déterminé par le vote majoritaire."""
        mock_ticker = _make_mock_ticker()
        self.enricher._fetch_ratings(1, mock_ticker)

        session = self.Session()
        rating = session.query(AnalystRatings).filter_by(asset_id=1).first()
        # 10 buy > 8 strong buy > 5 hold
        self.assertEqual(rating.consensus, "Buy")
        session.close()

    def test_ratings_no_data(self):
        """Ne doit pas crasher sans données."""
        mock_ticker = MagicMock()
        mock_ticker.analyst_price_targets = None
        mock_ticker.recommendations = None
        self.enricher._fetch_ratings(1, mock_ticker)

        session = self.Session()
        count = session.query(AnalystRatings).filter_by(asset_id=1).count()
        self.assertEqual(count, 0)
        session.close()


# ---------------------------------------------------------------------------
# Test enrich() (orchestrateur)
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

        self.assertTrue(result["highlights"])
        self.assertTrue(result["statements"])
        self.assertTrue(result["earnings"])
        self.assertTrue(result["ratings"])
        self.assertEqual(len(result["errors"]), 0)

    @patch("financials.enrichment.yfinance_enricher.yf.Ticker")
    def test_enrich_partial_failure(self, mock_yf_ticker_class):
        """Une méthode qui échoue ne doit pas bloquer les autres."""
        mock_ticker = _make_mock_ticker()
        mock_yf_ticker_class.return_value = mock_ticker

        # Patch _fetch_earnings pour qu'elle lève une exception
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

        # Restaurer
        self.enricher._fetch_earnings = original_fetch_earnings


if __name__ == "__main__":
    unittest.main()
