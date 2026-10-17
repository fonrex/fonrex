"""The yield curves of the US Treasury kept day by day (``macro/treasury_curves.py``).

What needs no database is tested here: which years a request reads, and how a
failed reading is reported. Storing and reading the rates is tested on
PostgreSQL (``tests/test_timescale_integration.py``). No test reaches the Treasury.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from financials.providers.treasury import NOMINAL, REAL, CurveFile, TreasuryError
from macro.treasury_curves import CurveLoad, TreasuryCurves, curve_of, is_complete

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


def curves(read_year=None, now=NOW) -> TreasuryCurves:
    return TreasuryCurves(None, read_year or AsyncMock(), now=lambda: now)


def load(fetched_at: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(fetched_at=fetched_at)


class TestCurves:
    def test_the_two_curves(self):
        assert (curve_of(NOMINAL), curve_of(REAL)) == ("nominal", "real")

    def test_an_unknown_curve(self):
        with pytest.raises(ValueError, match="Unknown curve 'zero': nominal, real"):
            curve_of("zero")


class TestYearsOfARequest:
    def test_from_the_first_year_of_the_curve_to_the_current_one(self):
        assert curves().years(NOMINAL, 1985) == list(range(1990, 2027))
        assert curves().years(REAL, 1990, 2005) == [2003, 2004, 2005]

    def test_never_after_the_current_year(self):
        assert curves().years(NOMINAL, 2025, 2030) == [2025, 2026]
        assert curves().years(NOMINAL, 2027) == []

    def test_the_current_year_follows_the_clock(self):
        new_year = datetime(2027, 1, 1, 0, 30, tzinfo=timezone.utc)
        assert curves(now=new_year).years(NOMINAL, 2026) == [2026, 2027]


class TestWhatIsReadAgain:
    def test_a_year_read_after_its_end_is_complete(self):
        assert is_complete(2025, datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc))
        assert is_complete(2025, datetime(2026, 3, 1))  # stored without zone: UTC

    def test_the_31st_of_december_may_come_on_the_first_of_january(self):
        assert not is_complete(2025, datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc))
        assert not is_complete(2025, None)

    def test_a_year_never_read_or_forced(self):
        assert curves()._needs_reading(2026, None, force=False)
        complete = load(datetime(2026, 1, 5, tzinfo=timezone.utc))
        assert curves()._needs_reading(2025, complete, force=True)

    def test_a_complete_year_is_never_read_again(self):
        complete = load(datetime(2026, 1, 5, tzinfo=timezone.utc))
        assert not curves()._needs_reading(2025, complete, force=False)

    def test_the_current_year_after_the_refresh_delay(self):
        recent = load(NOW - timedelta(hours=11, minutes=59))
        old = load(NOW - timedelta(hours=12))
        assert not curves()._needs_reading(2026, recent, force=False)
        assert curves()._needs_reading(2026, old, force=False)

    def test_a_past_year_read_before_its_end(self):
        before_the_end = load(datetime(2025, 12, 31, 20, 0, tzinfo=timezone.utc))
        assert curves()._needs_reading(2025, before_the_end, force=False)

    def test_the_delay_is_a_setting(self, monkeypatch):
        monkeypatch.setenv("TREASURY_REFRESH_HOURS", "48")
        assert curves().refresh_after == timedelta(hours=48)
        monkeypatch.setenv("TREASURY_REFRESH_HOURS", "0")
        assert curves().refresh_after == timedelta(hours=12)  # out of 1..720: the default


class TestAFailedReading:
    async def _reason(self, outcome) -> tuple[str | None, bool]:
        read_year = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else None
        read_year = read_year or AsyncMock(return_value=outcome)
        service = curves(read_year)
        service._store = AsyncMock(return_value=None)
        result = await service._read(NOMINAL, 2026)
        read_year.assert_awaited_once_with(NOMINAL, 2026)
        return result

    async def test_a_file_that_cannot_be_read(self):
        reason = await self._reason(TreasuryError("unknown maturity column '15 Yr'"))
        assert reason == ("TreasuryError: unknown maturity column '15 Yr'", True)

    async def test_the_treasury_does_not_answer(self):
        assert await self._reason(None) == ("the Treasury did not answer", False)

    async def test_an_unexpected_error(self):
        assert await self._reason(RuntimeError("boom")) == ("RuntimeError: boom", False)
        assert await self._reason(TimeoutError()) == ("TimeoutError", False)

    async def test_a_file_read_is_stored(self):
        curve_file = CurveFile(NOMINAL, 2026, ("10Y",), ())
        read_year = AsyncMock(return_value=curve_file)
        service = curves(read_year)
        service._store = AsyncMock(return_value=None)
        assert await service._read(NOMINAL, 2026) == (None, True)
        service._store.assert_awaited_once_with(NOMINAL, 2026, curve_file)


class TestQuestionsRefusedBeforeTheDatabase:
    async def test_an_unknown_maturity(self):
        with pytest.raises(ValueError, match="Unknown maturity '15Y'"):
            await curves().history(NOMINAL, ["10Y", "15Y"])

    async def test_an_unknown_curve(self):
        with pytest.raises(ValueError, match="Unknown curve"):
            await curves().curve_on("zero")
        with pytest.raises(ValueError, match="Unknown curve"):
            await curves().refresh("zero", 2026)

    async def test_nothing_to_read_asks_nothing(self):
        read_year = AsyncMock()
        service = curves(read_year)
        service.stored_range = AsyncMock(return_value=(None, None))
        result = await service.refresh(NOMINAL, 2030)
        assert (result.status, result.years_read, result.fetched_at) == ("fresh", [], None)
        read_year.assert_not_awaited()

    async def test_ensure_reads_the_years_of_the_period(self):
        service = curves()
        service.refresh = AsyncMock()
        await service.ensure(REAL, date(2024, 6, 1))
        service.refresh.assert_awaited_once_with(REAL, 2024, 2026)


class TestScript:
    def test_both_curves_and_the_whole_history_by_default(self):
        from scripts import load_treasury_curve

        arguments = load_treasury_curve.parse_arguments([])
        assert (arguments.curve, arguments.start_year, arguments.end_year, arguments.force) == (
            ["nominal", "real"],
            1990,
            None,
            False,
        )

    def test_some_years_of_one_curve(self):
        from scripts import load_treasury_curve

        arguments = load_treasury_curve.parse_arguments(
            ["--curve", "real", "--start-year", "2020", "--end-year", "2021", "--force"]
        )
        assert (arguments.curve, arguments.start_year, arguments.end_year, arguments.force) == (
            ["real"],
            2020,
            2021,
            True,
        )

    @pytest.mark.parametrize(
        "argv",
        [["--curve", "zero"], ["--start-year", "2026", "--end-year", "2020"]],
    )
    def test_refused(self, argv, capsys):
        from scripts import load_treasury_curve

        with pytest.raises(SystemExit) as stopped:
            load_treasury_curve.parse_arguments(argv)
        assert stopped.value.code == 2

    def test_a_line_per_curve(self):
        from scripts import load_treasury_curve

        fetched = CurveLoad(
            "nominal",
            "fetched",
            first_day=date(1990, 1, 2),
            last_day=date(2026, 10, 16),
            years_read=list(range(1990, 2027)),
        )
        fresh = CurveLoad("real", "fresh", first_day=date(2003, 1, 2), last_day=date(2026, 10, 16))
        failed = CurveLoad(
            "real",
            "failed",
            years_read=[2003, 2004],
            years_failed=[2005, 2006, 2007, 2008],
            reason="the Treasury did not answer",
        )

        assert load_treasury_curve.describe(fetched) == (
            "nominal  fetched  1990-01-02 → 2026-10-16  (37 years read: 1990–2026)"
        )
        assert load_treasury_curve.describe(fresh) == "real     fresh    2003-01-02 → 2026-10-16"
        assert load_treasury_curve.describe(failed) == (
            "real     failed   nothing stored  (2 years read: 2003, 2004)"
            "  (not read: 2005–2008, the Treasury did not answer)"
        )

    async def test_without_database_the_script_stops(self, monkeypatch, capsys):
        from scripts import load_treasury_curve

        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("ASYNC_DATABASE_URL", raising=False)

        assert await load_treasury_curve.main([]) == 2
        assert "DATABASE_URL" in capsys.readouterr().err
