"""``financials/esef_statements.py`` without database: the choices made before any query.

Storing the filings and their values, and reading the annual statements back, are
tested on PostgreSQL (``tests/test_timescale_integration.py``) with the real filings of
``tests/fixtures/esef/``. No test reaches GLEIF or filings.xbrl.org.
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from financials.esef_lei import LeiResolution
from financials.esef_statements import (
    ESEF_FORM,
    MISSING,
    READ,
    REFUSED,
    EsefLoad,
    EsefStatements,
    StoredFiling,
    fact_row,
    filings_to_read,
    is_annual,
)
from financials.providers.esef import parse_filings
from financials.xbrl_mapping import COLUMNS_BY_NAME, IFRS

FIXTURES = Path(__file__).parent / "fixtures" / "esef"
TOTAL = "529900S21EQ1BO4ESM68"
BNP = "R0MUWSFPU8MPRO8K5P83"


def filings(key: str, lei: str):
    return list(
        parse_filings(json.loads((FIXTURES / f"{key}_filings.json").read_text()), lei).filings
    )


def service(resolution=LeiResolution(None, "The instrument has no ISIN"), **options):
    resolver, provider = MagicMock(), MagicMock()
    resolver.resolve = AsyncMock(return_value=resolution)
    provider.filings = AsyncMock()
    provider.report = AsyncMock()
    return EsefStatements(None, provider, resolver, **options), provider, resolver


class TestFilingsToRead:
    def test_one_filing_per_period_the_one_processed_last(self):
        chosen = filings_to_read(filings("total", TOTAL), {})
        # One per language in the index: the French ones were processed last.
        assert [f.fxo_id for f in chosen] == [
            f"{TOTAL}-{year}-12-31-ESEF-FR-0" for year in (2022, 2023, 2024, 2025)
        ]

    def test_the_second_version_of_a_filing(self):
        chosen = [f.fxo_id[21:] for f in filings_to_read(filings("bnp", BNP), {})]
        assert chosen == [
            "2021-12-31-ESEF-FR-0",
            "2022-12-31-ESEF-FR-1",
            "2023-12-31-ESEF-FR-1",
            "2024-12-31-ESEF-FR-1",
            "2025-12-31-ESEF-FR-1",
        ]

    def test_a_filing_read_is_not_read_again(self):
        listed = filings("total", TOTAL)
        stored = {
            f.fxo_id: StoredFiling(f.fxo_id, f.sha256, status)
            for f, status in zip(
                listed, [READ, REFUSED, MISSING, READ, READ, READ, READ, READ], strict=True
            )
        }
        assert filings_to_read(listed, stored) == []

    def test_a_filing_listed_but_never_read_is_read(self):
        listed = filings("total", TOTAL)
        stored = {f.fxo_id: StoredFiling(f.fxo_id, f.sha256, None) for f in listed}
        assert len(filings_to_read(listed, stored)) == 4

    def test_a_filing_whose_file_changed_is_read_again(self):
        listed = filings("total", TOTAL)
        stored = {f.fxo_id: StoredFiling(f.fxo_id, f.sha256, READ) for f in listed}
        changed = f"{TOTAL}-2024-12-31-ESEF-FR-0"
        stored[changed] = StoredFiling(changed, "an older file", READ)
        assert [f.fxo_id for f in filings_to_read(listed, stored)] == [changed]

    def test_force_reads_every_period_again(self):
        listed = filings("total", TOTAL)
        stored = {f.fxo_id: StoredFiling(f.fxo_id, f.sha256, READ) for f in listed}
        assert len(filings_to_read(listed, stored, force=True)) == 4

    def test_a_filing_without_date_of_processing_or_period(self):
        listed = filings("total", TOTAL)
        undated = [replace(f, processed=None, listed_period_end=None) for f in listed[:2]]
        # Without period, each filing is its own period; without date, the name decides.
        assert [f.fxo_id for f in filings_to_read(undated, {})] == sorted(f.fxo_id for f in undated)


class TestAnnualFacts:
    @pytest.mark.parametrize(
        ("days", "annual"), [(0, True), (365, True), (366, True), (181, False), (90, False)]
    )
    def test_what_makes_the_annual_statements(self, days, annual):
        assert is_annual(days) is annual

    def test_a_stored_value_as_the_mapping_reads_it(self):
        stored = SimpleNamespace(
            lei=TOTAL,
            taxonomy="ifrs-full",
            concept="RevenueFromContractsWithCustomers",
            unit="USD",
            period_end=date(2025, 12, 31),
            duration_days=365,
            period_start=date(2025, 1, 1),
            value=Decimal("201196000000"),
            fxo_id=f"{TOTAL}-2025-12-31-ESEF-FR-0",
            report_end=date(2025, 12, 31),
        )
        row = fact_row(stored)
        assert (row.cik, row.accession, row.form, row.filed) == (
            TOTAL,
            stored.fxo_id,
            ESEF_FORM,
            date(2025, 12, 31),
        )
        assert row.key == (
            "ifrs-full",
            "RevenueFromContractsWithCustomers",
            "USD",
            stored.period_end,
            365,
        )


class TestMapping:
    @pytest.mark.parametrize(
        ("column", "concepts"),
        [
            ("revenue", ("Revenue", "RevenueFromContractsWithCustomers", "RevenueFromSaleOfGoods")),
            ("net_income", ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss")),
            ("total_equity", ("EquityAttributableToOwnersOfParent", "Equity")),
        ],
    )
    def test_the_ifrs_concepts_of_european_filings(self, column, concepts):
        ifrs = tuple(c for t, c in COLUMNS_BY_NAME[column].concepts if t == IFRS)
        # The share of the owners of the parent first: the group total only without it.
        assert ifrs == concepts


class TestWithoutLei:
    async def test_filings_xbrl_org_is_never_asked(self):
        statements, provider, resolver = service()
        load = await statements.refresh(7)
        assert (load.status, load.lei, load.reason) == (
            "no_lei",
            None,
            "The instrument has no ISIN",
        )
        resolver.resolve.assert_awaited_once_with(7, refresh=False)
        provider.filings.assert_not_awaited()

    async def test_recheck_asks_gleif_again(self):
        statements, _, resolver = service()
        await statements.refresh(7, recheck_lei=True)
        resolver.resolve.assert_awaited_once_with(7, refresh=True)

    async def test_no_fiscal_years_whether_read_or_not(self):
        statements, provider, _ = service()
        assert await statements.annual(7) == (await statements.refresh(7), [])
        load, years = await statements.annual(7, refresh=False)
        assert (load.status, years) == ("no_lei", [])
        provider.filings.assert_not_awaited()


class TestRefreshDelay:
    def test_follows_the_environment(self, monkeypatch):
        monkeypatch.setenv("ESEF_REFRESH_HOURS", "72")
        assert service()[0].refresh_after == timedelta(hours=72)

    def test_an_unusable_value_keeps_a_day(self, monkeypatch):
        monkeypatch.setenv("ESEF_REFRESH_HOURS", "0")
        assert service()[0].refresh_after == timedelta(hours=24)

    def test_a_reading_without_time_zone_is_in_utc(self):
        now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
        statements = service(now=lambda: now)[0]
        assert statements._is_recent(datetime(2026, 10, 19, 1, 0))
        assert not statements._is_recent(datetime(2026, 10, 18, 1, 0))
        assert not statements._is_recent(None)


class TestScript:
    def test_the_arguments(self):
        from scripts import load_esef

        assert vars(load_esef.parse_arguments([])) == {
            "ticker": None,
            "force": False,
            "recheck_lei": False,
        }
        arguments = load_esef.parse_arguments(
            ["--ticker", "AIR.PA", "TTE.PA", "--force", "--recheck-lei"]
        )
        assert (arguments.ticker, arguments.force, arguments.recheck_lei) == (
            ["AIR.PA", "TTE.PA"],
            True,
            True,
        )

    def test_a_line_per_share(self):
        from scripts import load_esef

        lei = "MINO79WLOO247M1IL051"
        read = EsefLoad(
            "fetched",
            lei,
            "AIRBUS SE",
            None,
            5,
            date(2025, 12, 31),
            532,
            read=(f"{lei}-2021-12-31-ESEF-NL-0", f"{lei}-2025-12-31-ESEF-NL-0"),
            refused={f"{lei}-2023-12-31-ESEF-NL-0": "EsefError: not JSON"},
        )
        assert load_esef.describe("AIR.PA", read) == (
            f"AIR.PA         fetched   {lei}  5 filings, 532 values, last period 2025-12-31"
            f"  (2 read)  (refused: {lei}-2023-12-31-ESEF-NL-0)"
        )
        assert load_esef.describe(
            "SAP.DE", EsefLoad("failed", "529900D6BF99LW9R2E68", reason="down")
        ) == ("SAP.DE         failed    529900D6BF99LW9R2E68  0 filings, nothing stored  (down)")
        assert load_esef.describe(
            "CW8.PA", EsefLoad("no_lei", reason="ETF files no annual report")
        ) == ("CW8.PA         no LEI    ETF files no annual report")


class TestScriptStops:
    async def test_after_errors_in_a_row_the_reading_stops(self, monkeypatch, capsys):
        from scripts import load_esef

        class Resources:
            session_factory = None

            async def close(self):
                pass

        async def shares(session_factory, tickers):
            return [(f"S{number:03d}", number) for number in range(1, 31)]

        async def refresh(self, asset_id, force=False, *, recheck_lei=False):
            if asset_id == 1:
                return EsefLoad("no_lei", reason="An issuer of JP files no ESEF report")
            raise ConnectionError("the database system is in recovery mode")

        monkeypatch.setattr(load_esef.AsyncDatabaseResources, "create", lambda: Resources())
        monkeypatch.setattr(load_esef, "instruments", shares)
        monkeypatch.setattr(load_esef.EsefStatements, "refresh", refresh)

        assert await load_esef.main([]) == 1
        lines = capsys.readouterr().out.splitlines()
        errors = [line for line in lines if " error " in line]
        assert len(errors) == load_esef.ERRORS_IN_A_ROW == 20
        assert errors[-1].startswith("S021           error     ConnectionError:")
        assert lines[-2] == "30 shares: 0 read, 1 without LEI, 20 failed"
        assert lines[-1] == (
            "Stopped at S021: 20 errors in a row. Check the database, then run the script "
            "again: the shares read are not read again."
        )

    async def test_a_reading_between_errors_starts_the_count_again(self, monkeypatch, capsys):
        from scripts import load_esef

        class Resources:
            session_factory = None

            async def close(self):
                pass

        async def shares(session_factory, tickers):
            return [(f"S{number:03d}", number) for number in range(1, 41)]

        async def refresh(self, asset_id, force=False, *, recheck_lei=False):
            if asset_id == 15:
                return EsefLoad("no_lei", reason="no ISIN")
            raise ConnectionError("down")

        monkeypatch.setattr(load_esef.AsyncDatabaseResources, "create", lambda: Resources())
        monkeypatch.setattr(load_esef, "instruments", shares)
        monkeypatch.setattr(load_esef.EsefStatements, "refresh", refresh)

        assert await load_esef.main([]) == 1
        lines = capsys.readouterr().out.splitlines()
        # 14 errors, a share without LEI, then 20 errors: stopped at the 35th share.
        assert lines[-1].startswith("Stopped at S035")


class TestReportEnd:
    """The period of a filing comes from its facts, not from a single late one."""

    def test_a_fact_dated_after_the_closing_is_not_the_period(self):
        from financials.esef_statements import report_end_of
        from financials.providers.esef import parse_xbrl_json

        name = "MINO79WLOO247M1IL051-2025-12-31-ESEF-NL-0"
        report = parse_xbrl_json((FIXTURES / f"{name}.json").read_bytes(), name[:20], name)
        assert report_end_of(report) == date(2025, 12, 31)
        # Shares at the date of the general meeting, tagged at that date.
        late = replace(report.facts[0], period_end=date(2026, 4, 15), duration_days=0)
        report = replace(report, facts=(*report.facts, late))
        assert report.period_ends[0] == date(2026, 4, 15)
        assert report_end_of(report) == date(2025, 12, 31)

    def test_a_filing_of_few_facts_takes_its_latest_date(self):
        from financials.esef_statements import report_end_of
        from financials.providers.esef import parse_xbrl_json

        name = "MINO79WLOO247M1IL051-2025-12-31-ESEF-NL-0"
        report = parse_xbrl_json((FIXTURES / f"{name}.json").read_bytes(), name[:20], name)
        report = replace(report, facts=report.facts[:3])
        assert report_end_of(report) == report.period_ends[0]


class TestEarningsPerShare:
    """AB InBev tagged its 2023 earnings per share 265 for 2.65 (US dollars)."""

    @staticmethod
    def years(eps_by_year, profit=Decimal("6000000000")):
        from financials.xbrl_mapping import Figure, FiscalYear

        def figure(value):
            return Figure(Decimal(value), "ifrs-full", "c", "USD", "f", "ESEF", date(2025, 12, 31))

        result = []
        for year, eps in eps_by_year.items():
            fiscal_year = FiscalYear(date(year, 12, 31), "USD")
            fiscal_year.figures["net_income"] = figure(profit)
            if eps is not None:
                fiscal_year.figures["eps_basic"] = figure(eps)
                fiscal_year.figures["eps_diluted"] = figure(eps)
            result.append(fiscal_year)
        return result

    def test_an_error_of_scale_is_left_out_and_said(self):
        from financials.esef_statements import check_per_share

        years = check_per_share(
            self.years({2025: "3.10", 2024: "2.86", 2023: "265", 2022: "2.98", 2021: "2.65"})
        )
        wrong = next(year for year in years if year.period_end.year == 2023)
        assert "eps_basic" not in wrong.figures and "eps_diluted" not in wrong.figures
        assert wrong.figures["net_income"]
        assert wrong.warnings[0] == (
            "eps_basic 265 left out: its ratio to the net income is 91 times the one of the "
            "other years (an error of scale in the filing)"
        )
        assert all(not year.warnings for year in years if year.period_end.year != 2023)

    def test_years_that_change_much_but_not_in_scale_are_kept(self):
        from financials.esef_statements import check_per_share

        # A loss year, a profit halved, shares bought back: within a factor of 20.
        years = self.years({2025: "4.0", 2024: "-1.5", 2023: "2.0", 2022: "6.0"})
        years[1].figures["net_income"] = (
            years[1]
            .figures["net_income"]
            .__class__(
                Decimal("-2200000000"), "ifrs-full", "c", "USD", "f", "ESEF", date(2025, 12, 31)
            )
        )
        assert all(not year.warnings for year in check_per_share(years))

    def test_a_year_near_break_even_is_not_judged(self):
        from financials.esef_statements import check_per_share

        # 0.0004 a share rounded to the cent: 25 times the ratio of the other years.
        years = self.years({2025: "3.10", 2024: "0.01", 2023: "2.98", 2022: "2.65", 2021: "2.9"})
        years[1].figures["net_income"] = replace(
            years[1].figures["net_income"], value=Decimal("800000")
        )
        checked = check_per_share(years)
        assert all(not year.warnings for year in checked)
        assert checked[1].value("eps_basic") == Decimal("0.01")

    def test_too_few_years_to_judge(self):
        from financials.esef_statements import check_per_share

        years = check_per_share(self.years({2025: "3.10", 2024: "265", 2023: "2.98"}))
        assert all("eps_basic" in year.figures for year in years)

    def test_years_without_earnings_per_share_or_profit(self):
        from financials.esef_statements import check_per_share

        years = self.years({2025: "3.1", 2024: None, 2023: "2.9", 2022: "300", 2021: "2.7"})
        del years[4].figures["net_income"]
        checked = check_per_share(years)
        # 2021 has no profit, 2024 no EPS: three ratios only, nothing judged.
        assert all(not year.warnings for year in checked)

    def test_an_even_number_of_other_years(self):
        from financials.esef_statements import check_per_share

        years = check_per_share(
            self.years({2025: "3", 2024: "3.2", 2023: "300", 2022: "2.8", 2021: "3.1", 2020: "2.9"})
        )
        assert [year.period_end.year for year in years if year.warnings] == [2023]

    def test_a_year_without_earnings_per_share_among_others(self):
        from financials.esef_statements import check_per_share

        years = check_per_share(
            self.years({2025: "3", 2024: None, 2023: "300", 2022: "2.8", 2021: "3.1", 2020: "2.9"})
        )
        assert [year.period_end.year for year in years if year.warnings] == [2023]
