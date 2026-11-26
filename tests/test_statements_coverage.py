"""``scripts/statements_coverage.py``: the share of instruments that hold each line.

The counts are tested on fiscal years stored the way the sources store them: three
rows per year for Yahoo (income statement, balance sheet, cash flow), the figures of
a filing for the SEC and the ESEF. No test reaches a database or a source.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from financials.fiscal_years import fiscal_years
from financials.xbrl_mapping import Figure, FiscalYear
from models import FinancialStatement
from scripts import statements_coverage as report
from scripts.statements_coverage import Latest, coverage, figures_of, is_financial, render


def yahoo_rows(end: date, **balance):
    """A fiscal year as the enrichment stores it: three rows, each with its own columns."""
    return [
        FinancialStatement(
            asset_id=1,
            statement_type="income",
            period_type="annual",
            period_end=end,
            revenue=Decimal(1000),
            income_before_tax=Decimal(150),
        ),
        FinancialStatement(
            asset_id=1,
            statement_type="balance",
            period_type="annual",
            period_end=end,
            total_assets=Decimal(5000),
            **balance,
        ),
        FinancialStatement(
            asset_id=1,
            statement_type="cashflow",
            period_type="annual",
            period_end=end,
            operating_cashflow=Decimal(260),
            stock_repurchases=Decimal(-60),
        ),
    ]


def filed_year(**values):
    year = FiscalYear(date(2025, 12, 31), "EUR")
    for column, value in values.items():
        year.figures[column] = Figure(
            Decimal(value), "ifrs-full", column, "EUR", "f", "ESEF", date(2025, 12, 31)
        )
    return year


class TestFiguresOf:
    def test_the_three_rows_of_a_yahoo_year_together(self):
        rows = yahoo_rows(date(2024, 12, 31)) + yahoo_rows(
            date(2025, 12, 31), total_current_assets=Decimal(1800)
        )
        (latest,) = fiscal_years(rows, limit=1)
        figures = figures_of(latest)
        # The balance sheet, the income statement and the cash flow of the latest year.
        assert figures["total_current_assets"] == Decimal(1800)
        assert figures["income_before_tax"] == Decimal(150)
        assert figures["stock_repurchases"] == Decimal(-60)
        assert figures["accounts_payable"] is None

    def test_a_year_of_a_filing(self):
        figures = figures_of(filed_year(ppe_net=900))
        assert figures["ppe_net"] == Decimal(900)
        assert figures["total_current_assets"] is None
        assert set(figures) == set(report.LINES)


class TestCoverage:
    def latests(self):
        return [
            Latest("yahoo", 1, False, {"total_current_assets": Decimal(1), "ppe_net": None}),
            Latest("yahoo", 2, True, {"total_current_assets": None, "ppe_net": Decimal(2)}),
            Latest("esef", 1, False, {"total_current_assets": Decimal(3)}),
        ]

    def test_filled_among_the_instruments_of_each_source(self):
        counts = coverage(self.latests(), lines=("total_current_assets", "ppe_net"))
        assert counts["yahoo"] == {"total_current_assets": (1, 2), "ppe_net": (1, 2)}
        assert counts["esef"] == {"total_current_assets": (1, 1), "ppe_net": (0, 1)}
        assert counts["sec"] == {"total_current_assets": (0, 0), "ppe_net": (0, 0)}

    def test_without_the_financial_sector(self):
        counts = coverage(self.latests(), without_financials=True, lines=("total_current_assets",))
        assert counts["yahoo"] == {"total_current_assets": (1, 1)}

    def test_the_table(self):
        counts = coverage(self.latests(), lines=("total_current_assets",))
        assert render(counts, "Every instrument").splitlines() == [
            "### Every instrument",
            "",
            "| Line | Yahoo | SEC | ESEF |",
            "|---|---|---|---|",
            "| `total_current_assets` | 50 % (1/2) | — | 100 % (1/1) |",
        ]

    def test_the_financial_sector(self):
        assert is_financial("Financial Services", None)
        assert is_financial(None, "Financials")
        assert not is_financial("Technology", "Information Technology")
        assert not is_financial(None, None)


class TestScript:
    def test_the_arguments(self):
        assert vars(report.parse_arguments([])) == {"ticker": None, "json": False}
        arguments = report.parse_arguments(["--ticker", "AAPL", "--json"])
        assert (arguments.ticker, arguments.json) == (["AAPL"], True)

    async def test_the_report_reads_the_database_only(self, monkeypatch, capsys):
        class Resources:
            session_factory = None

            async def close(self):
                pass

        async def shares(session_factory, tickers):
            return [(1, False), (2, True)]

        async def latest_years(session_factory, instruments):
            return [Latest("sec", 1, False, {"total_current_assets": Decimal(1)})]

        monkeypatch.setattr(report.AsyncDatabaseResources, "create", lambda: Resources())
        monkeypatch.setattr(report, "shares", shares)
        monkeypatch.setattr(report, "latest_years", latest_years)

        assert await report.main(["--json"]) == 0
        answer = json.loads(capsys.readouterr().out)
        assert answer["instruments"] == 2
        assert answer["all"]["sec"]["total_current_assets"] == [1, 1]
        assert answer["all"]["sec"]["ppe_net"] == [0, 1]

    async def test_no_database(self, monkeypatch, capsys):
        monkeypatch.setattr(report.AsyncDatabaseResources, "create", lambda: None)
        assert await report.main([]) == 2
        assert "DATABASE_URL" in capsys.readouterr().err

    async def test_nothing_is_asked_to_the_sources(self):
        # The resolvers of the report never reach the SEC nor GLEIF.
        from financials.sec_cik import SecTableError

        with pytest.raises(SecTableError):
            await report._no_sec_table()
        assert await report._no_gleif("NL0000235190") is None
