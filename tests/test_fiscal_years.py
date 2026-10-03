"""A valuation reads fiscal years, not statement rows.

``financial_statements`` holds three rows per fiscal year (income statement,
balance sheet, cash flow), each filling only its own columns. The valuation and
the solvency ratios used to take "the latest rows": the debt came without the
interest, the free cash flow of one year stood for an average of three.

These tests store statements the way the enrichment really stores them, then
run the readers on that shape.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from financials.enrichment.yfinance_enricher import YFinanceEnricher
from financials.fiscal_years import FISCAL_YEAR_FIELDS, fiscal_years
from models import Asset, AssetListing, Base, FinancialStatement, FundamentalsHighlights
from schemas.dcf import DCFRequest
from valuation.dcf_service import FISCAL_YEARS, DCFService

YEARS = [2024, 2023, 2022, 2021, 2020, 2019]
# Free cash flow of each year: 10 % more every year, 1 000 in 2024.
FREE_CASH_FLOW = {year: Decimal(1000) / Decimal("1.1") ** (2024 - year) for year in YEARS}


def _row(statement_type, year, **figures):
    return SimpleNamespace(
        statement_type=statement_type,
        period_end=date(year, 12, 31),
        **{field: figures.get(field) for field in FISCAL_YEAR_FIELDS if field != "period_end"},
    )


class TestFiscalYears:
    def test_three_statements_of_a_year_become_one_row(self):
        rows = [
            _row("cashflow", 2024, free_cashflow=Decimal(50)),
            _row("income", 2024, interest_expense=Decimal(4), revenue=Decimal(900)),
            _row("balance", 2024, total_debt=Decimal(100)),
        ]

        (year,) = fiscal_years(rows)

        assert year.period_end == date(2024, 12, 31)
        assert (year.total_debt, year.interest_expense, year.free_cashflow) == (100, 4, 50)
        assert year.revenue == 900
        assert year.capex is None  # not published: missing, not zero

    def test_most_recent_year_first_whatever_the_order_of_the_rows(self):
        rows = [_row("income", year, revenue=Decimal(year)) for year in (2021, 2024, 2022, 2023)]

        assert [year.revenue for year in fiscal_years(rows)] == [2024, 2023, 2022, 2021]

    def test_limit_counts_fiscal_years_not_rows(self):
        rows = [
            _row(kind, year, revenue=Decimal(year))
            for year in YEARS
            for kind in ("income", "balance", "cashflow")
        ]

        years = fiscal_years(rows, limit=5)

        assert [year.period_end.year for year in years] == [2024, 2023, 2022, 2021, 2020]

    def test_a_year_with_one_statement_only_keeps_what_it_has(self):
        rows = [_row("income", 2024, revenue=Decimal(900)), _row("balance", 2023, total_debt=Decimal(7))]

        latest, previous = fiscal_years(rows)

        assert (latest.revenue, latest.total_debt) == (900, None)
        assert (previous.revenue, previous.total_debt) == (None, 7)

    def test_a_figure_given_twice_is_read_from_the_statement_that_owns_it(self):
        rows = [
            _row("cashflow", 2024, net_income=Decimal(11)),
            _row("income", 2024, net_income=Decimal(10)),
        ]

        assert fiscal_years(rows)[0].net_income == 10

    def test_no_row_no_year(self):
        assert fiscal_years([]) == []


# ── On the shape really stored ────────────────────────────────────────────────


def _yahoo_statements():
    """Annual statements as yfinance returns them: one column per fiscal year."""
    dates = {year: pd.Timestamp(f"{year}-12-31") for year in YEARS}
    income = {
        dates[year]: {
            "Total Revenue": 10_000,
            "EBITDA": 2_500,
            "EBIT": 2_000,
            "Operating Income": 2_000,
            "Net Income": 1_200,
            "Diluted EPS": 12.0,
            "Interest Expense": 80,
            "Tax Provision": 400,
            "Basic Average Shares": 100.0,
            "Diluted Average Shares": 100.0,
        }
        for year in YEARS
    }
    balance = {
        dates[year]: {
            "Total Assets": 9_000,
            "Total Equity Gross Minority Interest": 4_000,
            "Total Debt": 2_000,
            "Cash And Cash Equivalents": 500,
        }
        for year in YEARS
    }
    cashflow = {
        dates[year]: {
            "Operating Cash Flow": float(FREE_CASH_FLOW[year]) + 300,
            "Free Cash Flow": float(FREE_CASH_FLOW[year]),
            "Capital Expenditure": -300,
            "Cash Dividends Paid": -200,
        }
        for year in YEARS
    }
    ticker = MagicMock()
    ticker.income_stmt = pd.DataFrame(income)
    ticker.balance_sheet = pd.DataFrame(balance)
    ticker.cashflow = pd.DataFrame(cashflow)
    ticker.quarterly_income_stmt = pd.DataFrame()
    ticker.quarterly_balance_sheet = pd.DataFrame()
    ticker.quarterly_cashflow = pd.DataFrame()
    return ticker


@pytest.fixture
def stored():
    """A database holding the statements of one company, written by the enricher."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine))
    database = MagicMock()
    database.get_session.side_effect = lambda: Session()

    session = Session()
    session.add(Asset(id=1, ticker="ACME", name="Acme", currency="EUR", is_active=True))
    session.add(
        AssetListing(
            asset_id=1, ticker="ACME", exchange="XPAR", currency="EUR", is_primary=True, is_active=True
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
    session.commit()
    session.close()

    enricher = YFinanceEnricher(database)
    enricher._fetch_statements(1, _yahoo_statements())
    yield SimpleNamespace(database=database, Session=Session, enricher=enricher)
    Session.remove()
    engine.dispose()


class TestStoredShape:
    def test_a_fiscal_year_is_stored_as_three_rows(self, stored):
        session = stored.Session()
        rows = session.query(FinancialStatement).filter_by(period_end=date(2024, 12, 31)).all()

        assert sorted(row.statement_type for row in rows) == ["balance", "cashflow", "income"]
        by_type = {row.statement_type: row for row in rows}
        assert by_type["balance"].interest_expense is None
        assert by_type["income"].total_debt is None

    def test_enrichment_stores_the_figures_the_valuation_reads(self, stored):
        session = stored.Session()
        income = (
            session.query(FinancialStatement)
            .filter_by(statement_type="income", period_end=date(2024, 12, 31))
            .one()
        )
        cashflow = (
            session.query(FinancialStatement)
            .filter_by(statement_type="cashflow", period_end=date(2024, 12, 31))
            .one()
        )

        assert (income.interest_expense, income.tax_provision, income.ebit) == (80, 400, 2000)
        assert (income.shares_basic, income.shares_diluted) == (100, 100)
        assert cashflow.dividends_paid == -200


class TestValuationOnStoredStatements:
    def _valuation(self, stored, models=("fcf",)):
        service = DCFService(stored.database)
        return service._compute_dcf_sync("ACME", DCFRequest(models=list(models)))

    def test_statements_cover_five_fiscal_years(self, stored):
        years = DCFService._annual_statements(stored.Session(), 1)

        assert len(years) == FISCAL_YEARS
        assert [year.period_end.year for year in years] == [2024, 2023, 2022, 2021, 2020]
        assert all(year.total_debt == 2000 and year.interest_expense == 80 for year in years)

    def test_cost_of_debt_and_tax_rate_come_from_the_statements(self, stored):
        wacc = self._valuation(stored).wacc

        # Interest 80 on a debt of 2 000; tax 400 on an operating profit of 2 000.
        assert wacc.cost_of_debt == Decimal("0.0400")
        assert wacc.tax_rate == Decimal("0.2000")
        # Debt 2 000 next to a market capitalisation of 8 000.
        assert wacc.weight_debt == Decimal("0.2000")

    def test_free_cash_flow_base_is_the_average_of_three_fiscal_years(self, stored):
        result = self._valuation(stored).models["fcf"]
        growth = Decimal("0.10")  # measured over the five fiscal years
        base = sum(FREE_CASH_FLOW[year] for year in (2024, 2023, 2022)) / 3

        first_year = base * (1 + growth)

        assert result.projected_values[0] == first_year.quantize(Decimal("0.01"))
        assert not any("growth rate not computable" in warning for warning in result.warnings)

    def test_net_debt_is_deducted_from_the_enterprise_value(self, stored):
        result = self._valuation(stored).models["fcf"]

        enterprise_value = sum(result.present_values) + result.pv_terminal
        expected = (enterprise_value - (Decimal(2000) - Decimal(500))) / 100

        assert abs(result.intrinsic_value_per_share - expected) < Decimal("0.02")

    def test_dividend_model_reads_the_dividends_of_the_cash_flow_statement(self, stored):
        result = self._valuation(stored, models=("ddm",)).models["ddm"]

        # 200 paid for 100 shares: 2 per share, flat over the years (growth 0 %).
        assert result.projected_values[0] == Decimal("2.00")

    def test_sensitivity_matrix_uses_the_same_fiscal_years(self, stored):
        service = DCFService(stored.database)

        matrix = service.compute_sensitivity("ACME", "fcf", [Decimal("0.08")], [Decimal("0.02")])

        base = float(sum(FREE_CASH_FLOW[year] for year in (2024, 2023, 2022)) / 3)
        flows = [base * 1.10**year for year in range(1, 6)]
        present = sum(flow / 1.08**year for year, flow in enumerate(flows, start=1))
        terminal = flows[-1] * 1.02 / (0.08 - 0.02) / 1.08**5
        expected = (present + terminal - (2000 - 500)) / 100

        assert float(matrix.matrix[0][0].intrinsic_value) == pytest.approx(expected, abs=0.01)


class TestSolvencyOnStoredStatements:
    def test_ratios_put_the_balance_sheet_and_the_income_statement_together(self, stored):
        stored.enricher._fetch_solvency_ratios(1)

        highlights = stored.Session().query(FundamentalsHighlights).filter_by(asset_id=1).one()

        assert highlights.debt_to_equity_ratio == Decimal("0.5")  # 2 000 / 4 000
        assert highlights.interest_coverage_ratio == Decimal("25")  # 2 000 / 80
        assert highlights.net_debt_to_ebitda == Decimal("0.6")  # (2 000 - 500) / 2 500
        assert highlights.cost_of_debt_source == "calculated"
        assert highlights.actual_cost_of_debt == Decimal("0.04")
