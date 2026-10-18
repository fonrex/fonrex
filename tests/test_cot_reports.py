"""The Commitments of Traders reports kept week by week (``macro/cot_reports.py``).

What needs no database is tested here: which years a request reads, how a failed
reading is reported, and the signals computed from stored lines. Storing and reading
the lines is tested on PostgreSQL (``tests/test_timescale_integration.py``). No test
reaches the CFTC.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from financials.providers.cftc import DISAGGREGATED, LEGACY, TFF, CftcError, CotFile, parse_cot_zip
from macro.cot_reports import (
    KEY_MARKETS,
    SPECULATORS,
    CotReports,
    StoredPosition,
    StoredWeek,
    category_of,
    is_complete,
    published_on,
    report_of,
    signals,
)

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "cftc"


def reports(read_file=None, now=NOW) -> CotReports:
    return CotReports(None, read_file or AsyncMock(), now=lambda: now)


def load(fetched_at: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(fetched_at=fetched_at)


def week(day: date, net: int, open_interest: int = 1000, category: str = "noncommercial"):
    long = max(net, 0) + 100
    return StoredWeek(day, "MARKET", open_interest, {category: StoredPosition(long, long - net, 0)})


def tuesdays(first: date, nets: list[int]) -> list[StoredWeek]:
    return [week(first + timedelta(days=7 * index), net) for index, net in enumerate(nets)]


class TestReportsAndTypesOfTraders:
    def test_the_three_reports(self):
        assert [report_of(name) for name in (LEGACY, TFF, DISAGGREGATED)] == [
            "legacy",
            "tff",
            "disaggregated",
        ]

    def test_an_unknown_report(self):
        with pytest.raises(ValueError, match="Unknown report 'supplemental'"):
            report_of("supplemental")

    def test_the_speculators_by_default(self):
        assert {name: category_of(name, None) for name in SPECULATORS} == SPECULATORS
        assert category_of(TFF, "asset_manager") == "asset_manager"

    def test_a_type_of_trader_of_another_report(self):
        with pytest.raises(ValueError, match="Unknown type of trader 'managed_money' for tff"):
            category_of(TFF, "managed_money")

    def test_the_key_markets_are_codes(self):
        for codes in KEY_MARKETS.values():
            assert codes and all(len(code) == 6 for code in codes)
            assert len(set(codes)) == len(codes)


class TestYearsOfARequest:
    def test_from_the_first_year_of_the_report_to_the_current_one(self):
        assert reports().years(LEGACY, 1980) == list(range(1986, 2027))
        assert reports().years(TFF, 2000, 2008) == [2006, 2007, 2008]

    def test_never_after_the_current_year(self):
        assert reports().years(DISAGGREGATED, 2025, 2030) == [2025, 2026]
        assert reports().years(LEGACY, 2027) == []


class TestWhatIsReadAgain:
    def test_a_year_of_the_history_files_is_complete_once_read(self):
        assert is_complete(2016, datetime(2016, 12, 30, tzinfo=timezone.utc))
        assert is_complete(1986, datetime(2026, 10, 18))  # stored without zone: UTC
        assert not is_complete(2016, None)

    def test_a_later_year_is_complete_from_the_15th_of_january(self):
        assert is_complete(2025, datetime(2026, 1, 15, tzinfo=timezone.utc))
        assert not is_complete(2025, datetime(2026, 1, 14, 23, 0, tzinfo=timezone.utc))

    def test_a_year_never_read_or_forced(self):
        assert reports()._needs_reading(2026, None, force=False)
        assert reports()._needs_reading(2010, load(NOW), force=True)

    def test_the_current_year_after_the_refresh_delay(self):
        assert not reports()._needs_reading(2026, load(NOW - timedelta(hours=23)), force=False)
        assert reports()._needs_reading(2026, load(NOW - timedelta(hours=24)), force=False)

    def test_the_delay_is_a_setting(self, monkeypatch):
        monkeypatch.setenv("COT_REFRESH_HOURS", "6")
        assert reports()._needs_reading(2026, load(NOW - timedelta(hours=6)), force=False)


class TestTheDayOfPublication:
    @pytest.mark.parametrize(
        ("report_date", "published"),
        [
            (date(2026, 10, 13), date(2026, 10, 16)),  # Tuesday → Friday
            (date(2012, 12, 24), date(2012, 12, 28)),  # Monday of a holiday week
            (date(2009, 11, 11), date(2009, 11, 13)),  # Wednesday
            (date(2001, 12, 28), date(2002, 1, 4)),  # a Friday report: the next Friday
        ],
    )
    def test_the_friday_after(self, report_date, published):
        assert published_on(report_date) == published


def file_of(report: str, years: tuple[int, int]) -> CotFile:
    return CotFile(report, "file.zip", years[0], years[1], ())


class TestWhichFilesARequestReads:
    def service(self, stored: dict[int, datetime] | None = None, outcome=None):
        service = reports()
        loads = {year: load(moment) for year, moment in (stored or {}).items()}
        service._loads = AsyncMock(return_value=loads)
        service._read = AsyncMock(side_effect=outcome or (lambda report, year: (None, True)))
        service.stored_range = AsyncMock(return_value=(None, None))
        return service

    @staticmethod
    def asked(service) -> list:
        return [call.args for call in service._read.await_args_list]

    async def test_the_history_file_is_read_once_for_all_its_years(self):
        service = self.service()
        result = await service.refresh(LEGACY, 2014, 2018)
        assert self.asked(service) == [(LEGACY, 2014), (LEGACY, 2017), (LEGACY, 2018)]
        assert result.years_read == [2014, 2015, 2016, 2017, 2018]
        assert result.status == "fetched"

    async def test_the_years_already_complete_are_not_asked(self):
        complete = datetime(2026, 1, 20, tzinfo=timezone.utc)
        stored = {year: complete for year in range(2006, 2026)}
        service = self.service(stored)
        result = await service.refresh(TFF, 2006)
        assert self.asked(service) == [(TFF, 2026)]
        assert result.years_read == [2026]

    async def test_once_the_cftc_does_not_answer_the_next_years_are_not_asked(self):
        service = self.service(outcome=lambda report, year: ("the CFTC did not answer", False))
        result = await service.refresh(DISAGGREGATED, 2016, 2018)
        assert self.asked(service) == [(DISAGGREGATED, 2016)]
        assert (result.status, result.years_failed) == ("failed", list(range(2006, 2019))[10:])
        assert result.reason == "the CFTC did not answer"

    async def test_a_file_that_cannot_be_read_fails_alone(self):
        def outcome(report, year):
            return ("CftcError: layout changed", True) if year == 2025 else (None, True)

        service = self.service(outcome=outcome)
        result = await service.refresh(LEGACY, 2025)
        assert self.asked(service) == [(LEGACY, 2025), (LEGACY, 2026)]
        assert (result.years_read, result.years_failed) == ([2026], [2025])
        assert result.reason == "CftcError: layout changed"

    async def test_nothing_to_read_asks_nothing(self):
        service = self.service()
        result = await service.refresh(LEGACY, 2030)
        assert (result.status, result.years_read, result.fetched_at) == ("fresh", [], None)
        service._read.assert_not_awaited()

    async def test_the_latest_reading_is_given(self):
        stored = {2025: datetime(2026, 2, 1, tzinfo=timezone.utc), 2026: NOW}
        service = self.service(stored)
        result = await service.refresh(LEGACY, 2025)
        assert result.fetched_at == NOW and result.status == "fresh"

    async def test_ensure_reads_the_years_of_the_period(self):
        service = reports()
        service.refresh = AsyncMock()
        await service.ensure(TFF, date(2024, 6, 1))
        service.refresh.assert_awaited_once_with(TFF, 2024, 2026)

    async def test_an_unknown_report_is_refused(self):
        with pytest.raises(ValueError, match="Unknown report"):
            await reports().refresh("supplemental", 2026)


class TestAFailedReading:
    async def _reason(self, outcome):
        read_file = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else None
        read_file = read_file or AsyncMock(return_value=outcome)
        service = reports(read_file)
        service._store = AsyncMock(return_value=None)
        result = await service._read(LEGACY, 2026)
        read_file.assert_awaited_once_with(LEGACY, 2026)
        return result

    async def test_a_file_that_cannot_be_read(self):
        reason = await self._reason(CftcError("the legacy file has 130 columns"))
        assert reason == ("CftcError: the legacy file has 130 columns", True)

    async def test_the_cftc_does_not_answer(self):
        assert await self._reason(None) == ("the CFTC did not answer", False)

    async def test_an_unexpected_error(self):
        assert await self._reason(RuntimeError("boom")) == ("RuntimeError: boom", False)
        assert await self._reason(TimeoutError()) == ("TimeoutError", False)

    async def test_a_file_read_is_stored(self):
        cot_file = file_of(LEGACY, (2026, 2026))
        service = reports(AsyncMock(return_value=cot_file))
        service._store = AsyncMock(return_value=None)
        assert await service._read(LEGACY, 2026) == (None, True)
        service._store.assert_awaited_once_with(cot_file)


class TestSignals:
    def test_net_share_and_change(self):
        result = signals(tuesdays(date(2026, 9, 1), [100, 250, -50]), "noncommercial", 2)
        assert [s.net for s in result] == [100, 250, -50]
        assert [s.change for s in result] == [None, 150, -300]
        assert [s.net_share for s in result] == [Decimal("0.1"), Decimal("0.25"), Decimal("-0.05")]
        assert result[0].published_on == date(2026, 9, 4)
        assert (result[0].long, result[0].short, result[0].spreading) == (200, 100, 0)

    def test_the_cot_index_once_the_window_is_covered(self):
        nets = [0, 100, 50, 25, 100, 200]
        result = signals(tuesdays(date(2026, 1, 6), nets), "noncommercial", 3)
        # The window of 3 weeks is covered from the third report on.
        assert [s.cot_index for s in result] == [
            None,
            None,
            Decimal("50.0"),  # 50 between 0 and 100
            Decimal("0.0"),  # 25, the lowest of 100, 50, 25
            Decimal("100.0"),
            Decimal("100.0"),
        ]

    def test_a_net_position_that_did_not_move_has_no_index(self):
        result = signals(tuesdays(date(2026, 1, 6), [7, 7, 7]), "noncommercial", 2)
        assert [s.cot_index for s in result] == [None, None, None]

    def test_the_window_is_a_length_of_time(self):
        # Reports twice a month: three of them span a month, more than a 3-week window.
        days = [date(1990, 1, 15), date(1990, 1, 31), date(1990, 2, 15), date(1990, 2, 28)]
        weeks = [week(day, net) for day, net in zip(days, [0, 100, 40, 60], strict=True)]
        result = signals(weeks, "noncommercial", 3)
        # 1990-01-31: the window (from 10 January) is covered by the report of the 15th.
        # 1990-02-15: the window (from 25 January) holds 100 and 40, not the 0 of January.
        assert [s.cot_index for s in result] == [
            None,
            Decimal("100.0"),
            Decimal("0.0"),
            Decimal("100.0"),
        ]

    def test_lines_out_of_order_and_without_the_type_of_trader(self):
        lines = tuesdays(date(2026, 1, 6), [10, 20])
        other = StoredWeek(
            date(2026, 1, 20), "MARKET", 10, {"commercial": StoredPosition(1, 1, None)}
        )
        result = signals([lines[1], other, lines[0]], "noncommercial", 1)
        assert [s.report_date for s in result] == [date(2026, 1, 6), date(2026, 1, 13)]
        assert signals([other], "noncommercial") == []

    def test_no_open_interest(self):
        assert (
            signals([week(date(2026, 1, 6), 0, open_interest=0)], "noncommercial")[0].net_share
            is None
        )

    def test_a_window_of_no_week(self):
        with pytest.raises(ValueError, match="at least one week"):
            signals([], "noncommercial", 0)

    def test_on_a_year_of_real_reports(self):
        cot_file = parse_cot_zip((FIXTURES / "deacot2025.zip").read_bytes(), LEGACY, 2025)
        weeks = [
            StoredWeek(
                line.report_date,
                line.name,
                line.open_interest,
                {p.category: StoredPosition(p.long, p.short, p.spreading) for p in line.positions},
            )
            for line in cot_file.weeks
            if line.code == "088691"
        ]
        result = signals(weeks, "noncommercial", 26)
        assert len(result) == 52
        assert all(s.cot_index is None for s in result[:25])
        assert all(Decimal(0) <= s.cot_index <= Decimal(100) for s in result[25:])
        assert all(s.published_on.weekday() == 4 for s in result)
        last = result[-1]
        assert last.net == last.long - last.short and last.report_date == date(2025, 12, 30)


class TestScript:
    def test_the_three_reports_and_the_whole_history_by_default(self):
        from scripts import load_cot

        arguments = load_cot.parse_arguments([])
        assert (arguments.report, arguments.start_year, arguments.end_year, arguments.force) == (
            ["legacy", "tff", "disaggregated"],
            1986,
            None,
            False,
        )

    def test_some_years_of_one_report(self):
        from scripts import load_cot

        arguments = load_cot.parse_arguments(
            ["--report", "tff", "--start-year", "2020", "--end-year", "2021", "--force"]
        )
        assert (arguments.report, arguments.start_year, arguments.end_year, arguments.force) == (
            ["tff"],
            2020,
            2021,
            True,
        )

    @pytest.mark.parametrize(
        "argv",
        [["--report", "supplemental"], ["--start-year", "2026", "--end-year", "2020"]],
    )
    def test_refused(self, argv, capsys):
        from scripts import load_cot

        with pytest.raises(SystemExit) as stopped:
            load_cot.parse_arguments(argv)
        assert stopped.value.code == 2

    def test_a_line_per_report(self):
        from macro.cot_reports import ReportLoad
        from scripts import load_cot

        fetched = ReportLoad(
            "legacy",
            "fetched",
            first_day=date(1986, 1, 15),
            last_day=date(2026, 10, 13),
            years_read=list(range(1986, 2027)),
        )
        fresh = ReportLoad("tff", "fresh", first_day=date(2006, 6, 13), last_day=date(2026, 10, 13))
        failed = ReportLoad(
            "disaggregated",
            "failed",
            years_read=[2017, 2018],
            years_failed=[2019, 2020, 2021, 2022],
            reason="the CFTC did not answer",
        )

        assert load_cot.describe(fetched) == (
            "legacy        fetched  1986-01-15 → 2026-10-13  (41 years read: 1986–2026)"
        )
        assert load_cot.describe(fresh) == "tff           fresh    2006-06-13 → 2026-10-13"
        assert load_cot.describe(failed) == (
            "disaggregated failed   nothing stored  (2 years read: 2017, 2018)"
            "  (not read: 2019–2022, the CFTC did not answer)"
        )

    async def test_without_database_the_script_stops(self, monkeypatch, capsys):
        from scripts import load_cot

        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("ASYNC_DATABASE_URL", raising=False)

        assert await load_cot.main([]) == 2
        assert "DATABASE_URL" in capsys.readouterr().err
