"""The check of the Yahoo statements against the SEC filings (``financials/sec_check.py``).

The SEC side is read from the real answers of ``tests/fixtures/sec/``. The Yahoo
side is stored the way the deep enrichment stores it: three rows a fiscal year
(income statement, balance sheet, cash flow), each filling only its own columns,
read through ``fiscal_years`` (AGENTS.md, rule 18). Yahoo gives payments as
negative cash flows. No test reaches the SEC or Yahoo.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from financials.fiscal_years import fiscal_years
from financials.sec_check import (
    COMPARED,
    MATCH,
    MISMATCH,
    MISSING_SEC,
    MISSING_YAHOO,
    NO_YAHOO_YEAR,
    NOT_COMPARED,
    check_tolerance,
    compare,
)
from financials.xbrl_mapping import annual_statements, facts_to_rows

FIXTURES = Path(__file__).parent / "fixtures" / "sec"


def sec_years(name, cik):
    answer = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return annual_statements(facts_to_rows(cik, answer))


APPLE = sec_years("companyfacts_aapl.json", "0000320193")
JPM = sec_years("companyfacts_jpm.json", "0000019617")
SAP = sec_years("companyfacts_sap.json", "0001000184")


def yahoo(period_end, currency="USD", **figures):
    """One fiscal year as the enrichment stores it: three rows, each its own columns."""
    income = {
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "eps_basic",
        "eps_diluted",
        "shares_basic",
        "shares_diluted",
        "interest_expense",
        "tax_provision",
    }
    balance = {"total_assets", "total_liabilities", "total_equity", "cash_and_equivalents"}
    rows = []
    for statement, columns in (("income", income), ("balance", balance), ("cashflow", None)):
        own = {
            name: Decimal(str(value))
            for name, value in figures.items()
            if (name in columns if columns else name not in income | balance)
        }
        rows.append(
            SimpleNamespace(
                statement_type=statement,
                period_type="annual",
                period_end=period_end,
                currency=currency,
                **own,
            )
        )
    return rows


def by_column(year):
    return {figure.column: figure for figure in year.figures}


class TestApple:
    # Yahoo dates Apple's fiscal year 2025 (ended Saturday 27 September) on the 30th.
    YAHOO = fiscal_years(
        yahoo(
            date(2025, 9, 30),
            revenue=416161000000,
            operating_income=135000000000,  # 1.5 % above the 10-K
            net_income=112010000000,
            eps_diluted=7.46,
            capex=-12715000000,  # a negative cash flow at Yahoo, a payment at the SEC
            dividends_paid=-15421000000,
            total_liabilities=285508000000,
            total_equity=73733000000,
            cash_and_equivalents=55000000000,  # with short-term investments
        )
    )

    def test_each_figure_is_checked_against_the_filing(self):
        latest = compare(APPLE, self.YAHOO, Decimal("0.01"))[0]
        figures = by_column(latest)

        assert (latest.period_end, latest.yahoo_period_end, latest.currency) == (
            date(2025, 9, 27),
            date(2025, 9, 30),
            "USD",
        )
        assert figures["revenue"].status == MATCH
        assert figures["revenue"].difference == Decimal("0")
        assert figures["operating_income"].status == MISMATCH
        assert figures["operating_income"].difference == Decimal("0.014656")
        assert figures["capex"].status == MATCH
        assert figures["dividends_paid"].status == MATCH
        assert figures["total_assets"].status == MISSING_YAHOO
        assert figures["total_assets"].filing_url.endswith("/000032019325000079/")
        assert figures["revenue"].concept == "RevenueFromContractWithCustomerExcludingAssessedTax"

    def test_figures_defined_differently_are_never_compared(self):
        figures = by_column(compare(APPLE, self.YAHOO, Decimal("0.01"))[0])
        assert not set(NOT_COMPARED) & set(figures)
        assert set(figures) <= set(COMPARED)

    def test_a_wider_tolerance_accepts_the_gap(self):
        figures = by_column(compare(APPLE, self.YAHOO, Decimal("0.02"))[0])
        assert figures["operating_income"].status == MATCH

    def test_a_year_yahoo_does_not_have(self):
        older = compare(APPLE, self.YAHOO, Decimal("0.01"))[1]
        assert older.period_end == date(2024, 9, 28)
        assert older.yahoo_period_end is None
        assert (older.figures, older.note) == ([], NO_YAHOO_YEAR)

    def test_a_yahoo_year_more_than_two_weeks_away_is_not_matched(self):
        far = fiscal_years(yahoo(date(2025, 12, 31), revenue=1))
        assert compare(APPLE, far, Decimal("0.01"))[0].yahoo_period_end is None


class TestOtherCompanies:
    def test_a_figure_only_yahoo_gives(self):
        # A bank files no operating income; Yahoo computes one.
        yahoo_jpm = fiscal_years(yahoo(date(2025, 12, 31), operating_income=72000000000))
        figures = by_column(compare(JPM, yahoo_jpm, Decimal("0.01"))[0])
        assert figures["operating_income"].status == MISSING_SEC
        assert figures["operating_income"].yahoo == Decimal("72000000000")
        assert figures["operating_income"].sec is None

    def test_another_currency_is_not_compared(self):
        yahoo_sap = fiscal_years(yahoo(date(2025, 12, 31), currency="USD", revenue=41000000000))
        latest = compare(SAP, yahoo_sap, Decimal("0.01"))[0]
        assert latest.figures == []
        assert latest.note == "Yahoo gives this year in USD, the SEC filing in EUR: not compared"

    def test_an_unknown_yahoo_currency_is_compared(self):
        yahoo_sap = fiscal_years(yahoo(date(2025, 12, 31), currency=None, revenue=36800000000))
        figures = by_column(compare(SAP, yahoo_sap, Decimal("0.01"))[0])
        assert figures["revenue"].status == MATCH

    def test_zero_on_both_sides_agrees_and_zero_against_a_figure_does_not(self):
        from financials.xbrl_mapping import Figure, FiscalYear

        def sec(value):
            year = FiscalYear(date(2025, 12, 31), "USD")
            year.figures["capex"] = Figure(
                Decimal(value),
                "us-gaap",
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "USD",
                "0000000001-26-000001",
                "10-K",
                date(2026, 2, 1),
            )
            return [year]

        both = by_column(
            compare(sec("0"), fiscal_years(yahoo(date(2025, 12, 31), capex=0)), Decimal("0.01"))[0]
        )
        assert (both["capex"].status, both["capex"].difference) == (MATCH, Decimal("0"))
        one = by_column(
            compare(sec("0"), fiscal_years(yahoo(date(2025, 12, 31), capex=-5)), Decimal("0.01"))[0]
        )
        assert (one["capex"].status, one["capex"].difference) == (MISMATCH, None)

    def test_nothing_filed_nothing_checked(self):
        assert compare([], fiscal_years(yahoo(date(2025, 12, 31), revenue=1))) == []


class TestTolerance:
    def test_one_percent_by_default(self, monkeypatch):
        monkeypatch.delenv("SEC_CHECK_TOLERANCE", raising=False)
        assert check_tolerance() == Decimal("0.01")

    def test_follows_the_environment(self, monkeypatch):
        monkeypatch.setenv("SEC_CHECK_TOLERANCE", "0.005")
        assert check_tolerance() == Decimal("0.005")

    @pytest.mark.parametrize("raw", ["1", "-0.01", "2%", "x"])
    def test_an_unusable_value_keeps_one_percent(self, monkeypatch, raw):
        monkeypatch.setenv("SEC_CHECK_TOLERANCE", raw)
        assert check_tolerance() == Decimal("0.01")
