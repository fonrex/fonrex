"""A valuation is made in the currency of the statements, with the risk-free rate of that currency.

The DCF used to discount every company with the US 10-year Treasury rate, and to
compare the intrinsic value with a share price whatever its currency (pence for
a London line, dollars for a US listing of a European company).
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from financials.enrichment.yfinance_enricher import YFinanceEnricher, _statements_currency
from models import Asset, AssetListing, Base, FinancialStatement, FundamentalsHighlights, PriceEOD
from schemas.dcf import DCFRequest, WACCInput
from schemas.macro import RiskFreeRate
from valuation.currency import resolve_currency
from valuation.dcf_service import DCFService

ECB_RATE = RiskFreeRate(Decimal("0.0352"), "ecb_live", date(2026, 10, 8))
FRED_RATE = RiskFreeRate(Decimal("0.0412"), "fred_cached", date(2026, 10, 8))


class TestCurrencyRule:
    def test_price_in_the_currency_of_the_statements_is_compared(self):
        currency = resolve_currency("EUR", "EUR")
        assert (currency.currency, currency.price_factor, currency.warning) == ("EUR", 1, None)

    @pytest.mark.parametrize("pence", ["GBX", "GBp"])
    def test_a_price_in_pence_is_turned_into_pounds(self, pence):
        currency = resolve_currency("GBP", pence)
        assert (currency.currency, currency.price_factor) == ("GBP", Decimal("0.01"))

    def test_a_price_in_another_currency_is_not_compared(self):
        currency = resolve_currency("EUR", "USD")

        assert currency.currency == "EUR"
        assert not currency.price_comparable
        assert "USD" in currency.warning and "EUR" in currency.warning

    def test_johannesburg_cents_are_turned_into_rand(self):
        currency = resolve_currency("ZAR", "ZAc")
        assert (currency.currency, currency.price_factor) == ("ZAR", Decimal("0.01"))

    def test_unknown_statements_take_the_currency_of_the_price(self):
        assert resolve_currency(None, "GBX").currency == "GBP"
        assert resolve_currency(None, "EUR").currency == "EUR"
        assert resolve_currency(None, None, "CHF").currency == "CHF"
        assert resolve_currency(None, None).currency == "USD"


class TestStatementsCurrencyFromYahoo:
    def test_financial_currency_is_read(self):
        assert _statements_currency(SimpleNamespace(info={"financialCurrency": "eur"})) == "EUR"

    @pytest.mark.parametrize("info", [{}, {"financialCurrency": None}, {"financialCurrency": "€"}])
    def test_missing_or_odd_currency_is_unknown(self, info):
        assert _statements_currency(SimpleNamespace(info=info)) is None

    def test_info_that_fails_leaves_it_unknown(self):
        class Broken:
            @property
            def info(self):
                raise RuntimeError("Yahoo is down")

        assert _statements_currency(Broken()) is None


# ── The rate follows the currency ─────────────────────────────────────────────


@pytest.fixture
def sources():
    ecb = MagicMock()
    ecb.get_euro_risk_free_rate = AsyncMock(return_value=ECB_RATE)
    fred = MagicMock()
    fred.get_risk_free_rate = AsyncMock(return_value=FRED_RATE)
    service = DCFService(MagicMock(), fred_service=fred, ecb_service=ecb)
    return SimpleNamespace(service=service, ecb=ecb, fred=fred)


class TestRiskFreeRateByCurrency:
    async def test_euro_cash_flows_use_the_ecb_rate(self, sources):
        assert await sources.service._risk_free_rate("EUR") == ECB_RATE
        sources.fred.get_risk_free_rate.assert_not_awaited()

    async def test_dollar_cash_flows_use_the_fred_rate(self, sources):
        assert await sources.service._risk_free_rate("USD") == FRED_RATE
        sources.ecb.get_euro_risk_free_rate.assert_not_awaited()

    async def test_without_an_ecb_rate_the_configured_rate_is_used_not_a_dollar_one(
        self, sources, monkeypatch
    ):
        import valuation.dcf_service as module

        monkeypatch.setattr(module, "DEFAULT_RISK_FREE_RATE", Decimal("0.03"))
        sources.ecb.get_euro_risk_free_rate.return_value = None

        rate = await sources.service._risk_free_rate("EUR")

        assert (rate.value, rate.source) == (Decimal("0.03"), "env_fallback")
        sources.fred.get_risk_free_rate.assert_not_awaited()

    async def test_a_currency_without_source_uses_the_configured_rate(self, sources):
        rate = await sources.service._risk_free_rate("GBP")

        assert rate.source == "env_fallback"
        sources.fred.get_risk_free_rate.assert_not_awaited()
        sources.ecb.get_euro_risk_free_rate.assert_not_awaited()


# ── On stored data ────────────────────────────────────────────────────────────

YEARS = [2024, 2023, 2022, 2021, 2020]


def _yahoo(financial_currency):
    dates = {year: pd.Timestamp(f"{year}-12-31") for year in YEARS}
    ticker = MagicMock()
    ticker.info = {"financialCurrency": financial_currency}
    ticker.income_stmt = pd.DataFrame(
        {
            dates[year]: {"EBIT": 2_000, "Interest Expense": 80, "Tax Provision": 400}
            for year in YEARS
        }
    )
    ticker.balance_sheet = pd.DataFrame(
        {dates[year]: {"Total Debt": 2_000, "Cash And Cash Equivalents": 500} for year in YEARS}
    )
    ticker.cashflow = pd.DataFrame({dates[year]: {"Free Cash Flow": 1_000.0} for year in YEARS})
    ticker.quarterly_income_stmt = pd.DataFrame()
    ticker.quarterly_balance_sheet = pd.DataFrame()
    ticker.quarterly_cashflow = pd.DataFrame()
    return ticker


def _company(financial_currency, listing_currency, last_close):
    """One company: statements enriched from Yahoo, a main listing with one close."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine))
    database = MagicMock()
    database.get_session.side_effect = lambda: Session()

    session = Session()
    session.add(Asset(id=1, ticker="ACME", name="Acme", currency="USD", is_active=True))
    session.add(
        AssetListing(
            id=10,
            asset_id=1,
            ticker="ACME",
            exchange="X",
            currency=listing_currency,
            is_primary=True,
            is_active=True,
        )
    )
    session.add(
        FundamentalsHighlights(
            asset_id=1,
            market_cap=Decimal(8_000),
            shares_outstanding=100,
            beta=Decimal("1.0"),
            week_52_high=Decimal(90),
            week_52_low=Decimal(70),
        )
    )
    session.add(
        PriceEOD(
            asset_listing_id=10,
            asset_id=1,
            resolution="1D",
            timestamp=pd.Timestamp("2026-10-08", tz="UTC").to_pydatetime(),
            close=last_close,
        )
    )
    session.commit()
    session.close()
    YFinanceEnricher(database)._fetch_statements(1, _yahoo(financial_currency))
    return SimpleNamespace(database=database, Session=Session, engine=engine)


class TestValuationCurrency:
    def _value(self, company, rf=ECB_RATE):
        return DCFService(company.database)._compute_dcf_sync("ACME", DCFRequest(), rf)

    def test_statements_record_their_currency(self):
        company = _company("EUR", "EUR", 80.0)
        currencies = {row.currency for row in company.Session().query(FinancialStatement)}
        assert currencies == {"EUR"}

    def test_euro_company_is_valued_in_euros_with_the_euro_rate(self):
        result = self._value(_company("EUR", "EUR", 80.0))

        assert (result.currency, result.price_currency, result.warnings) == ("EUR", "EUR", [])
        assert result.current_price == Decimal("80.00")
        assert result.wacc.risk_free_rate == Decimal("0.0352")
        assert result.wacc.risk_free_rate_source == "ecb_live"
        assert result.wacc.risk_free_rate_currency == "EUR"
        fcf = result.models["fcf"]
        # Each model now gives its own upside over the price (it was always 0).
        expected = (fcf.intrinsic_value_per_share - Decimal(80)) / Decimal(80) * 100
        assert fcf.upside_pct == expected.quantize(Decimal("0.01"))
        assert result.consensus_upside_pct is not None

    def test_london_price_in_pence_is_compared_in_pounds(self):
        result = self._value(_company("GBP", "GBX", 8_000.0), rf=None)

        assert (result.currency, result.price_currency) == ("GBP", "GBP")
        assert result.current_price == Decimal("80.00")
        assert result.models["fcf"].upside_pct is not None

    def test_price_in_another_currency_gives_no_upside(self):
        result = self._value(_company("EUR", "USD", 95.0))

        assert (result.currency, result.price_currency) == ("EUR", "USD")
        assert result.consensus_upside_pct is None
        assert result.models["fcf"].upside_pct is None
        assert "no upside is computed" in result.warnings[0]

    def test_the_rate_of_the_configuration_names_no_currency(self):
        rf = RiskFreeRate(Decimal("0.04"), "env_fallback")
        result = self._value(_company("EUR", "EUR", 80.0), rf=rf)

        assert result.wacc.risk_free_rate_source == "env_fallback"
        assert result.wacc.risk_free_rate_currency is None

    def test_a_rate_given_in_the_request_names_no_currency(self):
        request = DCFRequest(wacc_params=WACCInput(risk_free_rate=Decimal("0.03")))
        result = DCFService(_company("EUR", "EUR", 80.0).database)._compute_dcf_sync(
            "ACME", request, ECB_RATE
        )

        assert result.wacc.risk_free_rate == Decimal("0.03")
        assert result.wacc.risk_free_rate_source == "client_override"
        assert result.wacc.risk_free_rate_currency is None

    async def test_compute_dcf_asks_the_source_of_the_currency_of_the_statements(self):
        company = _company("EUR", "USD", 95.0)
        ecb = MagicMock()
        ecb.get_euro_risk_free_rate = AsyncMock(return_value=ECB_RATE)
        fred = MagicMock()
        fred.get_risk_free_rate = AsyncMock(return_value=FRED_RATE)
        service = DCFService(company.database, fred_service=fred, ecb_service=ecb)

        result = await service.compute_dcf("ACME", DCFRequest())

        # The listing is in dollars, the cash flows in euros: the euro rate.
        assert result.wacc.risk_free_rate_source == "ecb_live"
        fred.get_risk_free_rate.assert_not_awaited()

    def test_sensitivity_gives_no_upside_across_currencies(self):
        company = _company("EUR", "USD", 95.0)
        result = DCFService(company.database).compute_sensitivity(
            "ACME", "fcf", [Decimal("0.08")], [Decimal("0.02")]
        )
        assert result.matrix[0][0].upside_pct is None
