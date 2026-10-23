"""From the XBRL facts of the SEC to annual statements (``financials/xbrl_mapping.py``).

The answers are the reduced real ones of ``tests/fixtures/sec/`` (Apple, a bank,
two IFRS filers); ``expected.json`` gives what a reading must find. Storing the
facts is tested on PostgreSQL. No test reaches the SEC.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from financials.providers.sec_edgar import SECEdgarProvider
from financials.xbrl_mapping import (
    COLUMNS,
    COLUMNS_BY_NAME,
    FactRow,
    XbrlError,
    annual_statements,
    facts_to_rows,
    reporting_currency,
)
from models import FinancialStatement

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
COMPANIES = {
    "companyfacts_aapl.json": "0000320193",
    "companyfacts_jpm.json": "0000019617",
    "companyfacts_sap.json": "0001000184",
    "companyfacts_nvs.json": "0001114448",
}


def facts(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def years(name: str):
    return {
        year.period_end: year
        for year in annual_statements(facts_to_rows(COMPANIES[name], facts(name)))
    }


class TestColumns:
    def test_every_column_is_a_column_of_the_statements(self):
        stored = {column.name for column in FinancialStatement.__table__.columns}
        assert {column.name for column in COLUMNS} <= stored

    def test_each_column_names_us_gaap_and_ifrs_concepts(self):
        for column in COLUMNS:
            taxonomies = {taxonomy for taxonomy, _ in column.concepts}
            assert taxonomies == {"us-gaap", "ifrs-full"}, column.name
            assert column.statement in ("income", "balance", "cashflow")

    def test_figures_the_sec_does_not_publish_are_not_mapped(self):
        # EBITDA and free cash flow are computed, never filed: they are not official.
        assert not {"ebitda", "ebit", "free_cashflow", "net_debt"} & set(COLUMNS_BY_NAME)


class TestFacts:
    def test_only_annual_reports_and_fiscal_years_are_kept(self):
        rows = facts_to_rows("0000320193", facts("companyfacts_aapl.json"))
        revenue = [row for row in rows if row.concept.startswith("RevenueFromContract")]

        # The 10-Q facts (quarters, nine months) are left out; two fiscal years remain.
        assert [(row.period_end, row.duration_days, row.form) for row in revenue] == [
            (date(2024, 9, 28), 363, "10-K"),
            (date(2025, 9, 27), 363, "10-K"),
        ]
        assert {row.form for row in rows} == {"10-K"}

    def test_one_value_per_period_the_latest_filing(self):
        rows = facts_to_rows("0000320193", facts("companyfacts_aapl.json"))
        fy2024 = next(
            row
            for row in rows
            if row.concept.startswith("RevenueFromContract") and row.period_end == date(2024, 9, 28)
        )
        # Filed in the 10-K of 2024, then again in the 10-K of 2025: the 2025 filing is kept.
        assert (fy2024.accession, fy2024.filed, fy2024.frame) == (
            "0000320193-25-000079",
            date(2025, 10, 31),
            "CY2024",
        )
        assert fy2024.value == Decimal("391035000000")

    def test_a_restatement_replaces_the_first_figure(self):
        rows = facts_to_rows("0001000184", facts("companyfacts_sap.json"))
        assets = next(
            row for row in rows if row.concept == "Assets" and row.period_end == date(2024, 12, 31)
        )
        assert (assets.value, assets.filed) == (Decimal("74237000000"), date(2026, 2, 26))
        assert assets.duration_days == 0 and assets.period_start is None

    def test_unmapped_concepts_and_taxonomies_are_left_out(self):
        answer = {
            "facts": {
                "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": []}}},
                "us-gaap": {
                    "SomethingElse": {
                        "units": {
                            "USD": [
                                {
                                    "end": "2025-12-31",
                                    "val": 1,
                                    "accn": "a",
                                    "form": "10-K",
                                    "filed": "2026-01-01",
                                }
                            ]
                        }
                    }
                },
            }
        }
        assert facts_to_rows("1", answer) == []

    @pytest.mark.parametrize(
        "fact",
        [
            {"end": "2025-12-31", "val": 1, "accn": "a", "form": "8-K", "filed": "2026-01-01"},
            {"end": "2025-12-31", "val": 1, "form": "10-K", "filed": "2026-01-01"},
            {"end": "bad", "val": 1, "accn": "a", "form": "10-K", "filed": "2026-01-01"},
            {"end": "2025-12-31", "accn": "a", "form": "10-K", "filed": "2026-01-01"},
            {"end": "2025-12-31", "val": "NaN", "accn": "a", "form": "10-K", "filed": "2026-01-01"},
            {
                "start": "2025-07-01",
                "end": "2025-12-31",
                "val": 1,
                "accn": "a",
                "form": "10-K",
                "filed": "2026-01-01",
            },
            "not a fact",
        ],
    )
    def test_a_fact_that_cannot_be_used_is_left_out(self, fact):
        answer = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [fact]}}}}}
        assert facts_to_rows("1", answer) == []

    @pytest.mark.parametrize("answer", [None, [], {"cik": 1}, {"facts": "none"}])
    def test_an_answer_without_facts_is_refused(self, answer):
        with pytest.raises(XbrlError):
            facts_to_rows("1", answer)


class TestAnnualStatements:
    @pytest.mark.parametrize("name", list(COMPANIES))
    def test_the_expected_figures_of_each_company(self, name):
        expected = EXPECTED[name]
        by_end = years(name)
        for end, figures in expected.items():
            if not end[:4].isdigit():
                continue
            year = by_end[date.fromisoformat(end)]
            assert year.currency == expected["currency"]
            for column, value in figures.items():
                wanted = None if value is None else Decimal(str(value))
                assert year.value(column) == wanted, (name, end, column)

    def test_years_are_most_recent_first(self):
        assert list(years("companyfacts_aapl.json")) == [date(2025, 9, 27), date(2024, 9, 28)]

    def test_a_figure_names_the_concept_and_the_filing_it_comes_from(self):
        revenue = years("companyfacts_nvs.json")[date(2025, 12, 31)].figures["revenue"]
        assert (revenue.taxonomy, revenue.concept, revenue.unit, revenue.form) == (
            "ifrs-full",
            "RevenueFromSaleOfGoods",
            "USD",
            "20-F",
        )
        assert revenue.filing_url == (
            "https://www.sec.gov/Archives/edgar/data/1114448/000111444826000004/"
        )

    def test_per_share_figures_are_in_the_currency_per_share(self):
        eps = years("companyfacts_sap.json")[date(2025, 12, 31)].figures["eps_diluted"]
        assert (eps.unit, eps.value) == ("EUR/shares", Decimal("6.1"))

    def test_a_bank_has_no_operating_income_nor_capex(self):
        year = years("companyfacts_jpm.json")[date(2025, 12, 31)]
        assert year.value("operating_income") is None
        assert year.value("capex") is None
        assert year.value("operating_cashflow") == Decimal("-147782000000")

    def test_the_currency_of_the_latest_report_and_never_a_mix(self):
        def row(concept, unit, value, filed):
            return FactRow(
                "1",
                "ifrs-full",
                concept,
                unit,
                date(2025, 12, 31),
                365,
                date(2025, 1, 1),
                Decimal(value),
                f"acc-{filed}",
                "20-F",
                2025,
                "FY",
                date.fromisoformat(filed),
                None,
            )

        rows = [
            # An old convenience translation in US dollars, and the figures in euros.
            row("Revenue", "USD", "40000000000", "2018-02-28"),
            row("Revenue", "EUR", "36800000000", "2026-02-26"),
            row("ProfitLossAttributableToOwnersOfParent", "EUR", "7161000000", "2026-02-26"),
            row("CashFlowsFromUsedInOperatingActivities", "USD", "9000000000", "2026-02-26"),
        ]
        assert reporting_currency(rows) == "EUR"
        (year,) = annual_statements(rows)
        assert (year.currency, year.value("revenue")) == ("EUR", Decimal("36800000000"))
        assert year.value("operating_cashflow") is None  # not mixed in

    def test_a_ratio_or_a_unit_of_the_filer_is_not_a_currency(self):
        def row(concept, unit, value):
            return FactRow(
                "lei",
                "ifrs-full",
                concept,
                unit,
                date(2025, 12, 31),
                365,
                date(2025, 1, 1),
                Decimal(value),
                "f",
                "ESEF",
                2025,
                "FY",
                date(2025, 12, 31),
                None,
            )

        # More ratios and tonnes of CO2 than amounts in the latest filing: still euros.
        rows = [row(f"Ratio{n}", "pure", "0.5") for n in range(4)]
        rows += [row(f"Emissions{n}", "tCO2e", "12") for n in range(3)]
        rows += [row("Revenue", "EUR", "1000"), row("ProfitLoss", "EUR", "100")]
        assert reporting_currency(rows) == "EUR"
        assert reporting_currency(rows[:7]) is None

    def test_no_money_facts_no_years(self):
        assert reporting_currency([]) is None
        assert annual_statements([]) == []


class TestCompanyFacts:
    async def test_read_by_cik_with_the_user_agent_of_the_instance(self, fake_network, monkeypatch):
        monkeypatch.setenv("SEC_EDGAR_EMAIL", "pierre@example.org")
        fake_network.get(
            "/api/xbrl/companyfacts/CIK0000320193.json",
            httpx.Response(200, text=(FIXTURES / "companyfacts_aapl.json").read_text()),
        )

        answer = await SECEdgarProvider().company_facts("320193")

        assert answer["entityName"] == "Apple Inc."
        request = fake_network.requests[0]
        assert str(request.url) == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
        assert request.headers["User-Agent"] == "Fonrex pierre@example.org"

    async def test_an_unknown_cik_gives_no_answer(self, fake_network):
        fake_network.get(
            "/api/xbrl/companyfacts/",
            httpx.Response(404, text=(FIXTURES / "error_unknown_cik.xml").read_text()),
        )
        assert await SECEdgarProvider().company_facts("0000000001") is None
