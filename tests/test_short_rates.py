"""The 3-month rates of the OECD kept month by month (``macro/short_rates.py``).

The months a reading asks for and the command line script are tested here;
storing and reading the rates, and the script from the answer of the OECD to the
tables, are tested on PostgreSQL (``tests/test_timescale_integration.py``).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from financials.providers.oecd import ShortRate
from macro.short_rates import (
    _MAX_PARAMETERS,
    FAILED,
    FETCHED,
    FIRST_PERIOD,
    REVISED_MONTHS,
    RatesLoad,
    ShortRates,
    StoredArea,
    months_before,
)

NOW = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)


def service(**kwargs) -> ShortRates:
    return ShortRates(None, None, now=lambda: NOW, **kwargs)  # type: ignore[arg-type]


class TestMonths:
    @pytest.mark.parametrize(
        ("day", "months", "expected"),
        [
            (date(2026, 11, 20), 0, date(2026, 11, 1)),
            (date(2026, 11, 20), 10, date(2026, 1, 1)),
            (date(2026, 11, 20), 11, date(2025, 12, 1)),
            (date(2026, 1, 31), 1, date(2025, 12, 1)),
            (date(2026, 11, 20), 24, date(2024, 11, 1)),
        ],
    )
    def test_months_before(self, day, months, expected):
        assert months_before(day, months) == expected

    def test_the_first_reading_takes_the_whole_history(self):
        assert service().first_month(None) == FIRST_PERIOD == date(1990, 1, 1)

    def test_the_next_ones_ask_again_for_the_last_months(self):
        assert REVISED_MONTHS == 24
        assert service().first_month(NOW - timedelta(days=3)) == date(2024, 11, 1)

    def test_never_before_the_first_month(self):
        early = ShortRates(None, None, now=lambda: datetime(1991, 3, 1))  # type: ignore[arg-type]
        assert early.first_month(datetime(1991, 2, 1)) == FIRST_PERIOD


class RecordingSession:
    """A session that keeps the statements it is given and stores nothing."""

    def __init__(self):
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return self

    async def execute(self, statement):
        self.statements.append(statement)
        return []

    async def merge(self, instance):
        return instance


class TestStore:
    async def test_the_whole_history_in_statements_asyncpg_accepts(self):
        """asyncpg refuses a statement of more than 32,767 parameters: the first reading,
        about 45 areas × 430 months, failed on its first INSERT."""
        session = RecordingSession()
        rates = ShortRates(lambda: session, None, now=lambda: NOW)  # type: ignore[arg-type]
        history = [
            ShortRate(f"A{area:02d}", months_before(date(2026, 10, 1), month), Decimal("0.02"), "A")
            for area in range(45)
            for month in range(430)
        ]

        assert await rates._store(history) == NOW

        inserts = [s for s in session.statements if isinstance(s, Insert)]
        parameters = [len(s.compile(dialect=postgresql.dialect()).params) for s in inserts]
        assert len(inserts) > 1
        assert max(parameters) <= _MAX_PARAMETERS
        assert sum(parameters) == 9 * len(history)


class TestRefreshDelay:
    def test_a_day_by_default(self, monkeypatch):
        monkeypatch.delenv("OECD_REFRESH_HOURS", raising=False)
        assert service().refresh_after == timedelta(hours=24)

    def test_chosen_in_the_environment(self, monkeypatch):
        monkeypatch.setenv("OECD_REFRESH_HOURS", "6")
        assert service().refresh_after == timedelta(hours=6)


class TestRetryAfterAFailure:
    """A failed reading is not asked again at once: every request needing a rate would
    download again, one after the other behind the lock, and spend the quota of the
    OECD (about 60 downloads an hour)."""

    @staticmethod
    def rates(clock, reader):
        rates = ShortRates(None, reader, now=lambda: clock["now"])  # type: ignore[arg-type]
        rates.last_reading = AsyncMock(return_value=None)  # type: ignore[method-assign]
        rates._store = AsyncMock(side_effect=lambda _rates: clock["now"])  # type: ignore[method-assign]
        return rates

    def test_fifteen_minutes_by_default(self, monkeypatch):
        monkeypatch.delenv("OECD_RETRY_MINUTES", raising=False)
        assert service().retry_after == timedelta(minutes=15)

    def test_chosen_in_the_environment(self, monkeypatch):
        monkeypatch.setenv("OECD_RETRY_MINUTES", "5")
        assert service().retry_after == timedelta(minutes=5)

    async def test_not_asked_again_before_the_delay(self, monkeypatch):
        monkeypatch.delenv("OECD_RETRY_MINUTES", raising=False)
        clock, reader = {"now": NOW}, AsyncMock(return_value=None)
        rates = self.rates(clock, reader)

        first = await rates.refresh()
        clock["now"] = NOW + timedelta(minutes=14)
        second = await rates.refresh()

        assert reader.await_count == 1
        assert (first.status, second.status) == (FAILED, FAILED)
        assert second.reason == (
            "the OECD did not answer (refused, over its limit, or no rate) "
            "(at 2026-11-20 21:00 UTC; asked again 15 minutes after)"
        )

        ok = [ShortRate("EA20", date(2026, 10, 1), Decimal("0.02"), "A")]
        reader.return_value = ok
        clock["now"] = NOW + timedelta(minutes=15)
        assert (await rates.refresh()).status == FETCHED
        assert reader.await_count == 2

    async def test_force_asks_at_once(self, monkeypatch):
        monkeypatch.delenv("OECD_RETRY_MINUTES", raising=False)
        clock, reader = {"now": NOW}, AsyncMock(side_effect=TimeoutError())
        rates = self.rates(clock, reader)

        await rates.refresh()
        await rates.refresh(force=True)

        assert reader.await_count == 2

    async def test_a_success_forgets_the_failure(self, monkeypatch):
        monkeypatch.delenv("OECD_RETRY_MINUTES", raising=False)
        clock, reader = {"now": NOW}, AsyncMock(return_value=None)
        rates = self.rates(clock, reader)
        await rates.refresh()

        reader.return_value = [ShortRate("EA20", date(2026, 10, 1), Decimal("0.02"), "A")]
        assert (await rates.refresh(force=True)).status == FETCHED
        # Read again a day later, the OECD refusing is a new failure, not the old one.
        clock["now"] = NOW + timedelta(days=1)
        reader.return_value = None
        assert (await rates.refresh()).reason == (
            "the OECD did not answer (refused, over its limit, or no rate)"
        )


class TestScript:
    def test_arguments_by_default(self):
        from scripts import load_short_rates

        arguments = load_short_rates.parse_arguments([])
        assert (arguments.start, arguments.force) == (None, False)

    def test_a_first_month_and_force(self):
        from scripts import load_short_rates

        arguments = load_short_rates.parse_arguments(["--start", "2020-03", "--force"])
        assert (arguments.start, arguments.force) == (date(2020, 3, 1), True)

    @pytest.mark.parametrize(
        "argv", [["--start", "2020"], ["--start", "2020-13"], ["--start", "1989-12"]]
    )
    def test_a_month_refused(self, argv, capsys):
        from scripts import load_short_rates

        with pytest.raises(SystemExit):
            load_short_rates.parse_arguments(argv)
        assert "--start" in capsys.readouterr().err

    def test_the_lines_printed(self):
        from scripts import load_short_rates

        load = RatesLoad(FETCHED, NOW, date(1990, 1, 1), received=1234, areas=["CHE", "EA20"])
        areas = [
            StoredArea("CHE", date(1990, 1, 1), date(2026, 10, 1), NOW),
            StoredArea("XYZ", None, None, NOW),
        ]
        assert load_short_rates.describe(load, areas) == [
            "fetched  1234 rates of 2 areas from 1990-01",
            "  CHE    1990-01 → 2026-10",
            "  XYZ    nothing stored",
        ]
        failed = RatesLoad(FAILED, NOW, date(2024, 11, 1), reason="the OECD did not answer")
        assert load_short_rates.describe(failed, []) == ["failed   (the OECD did not answer)"]

    async def test_without_database(self, monkeypatch, capsys):
        from scripts import load_short_rates

        monkeypatch.setattr(load_short_rates.AsyncDatabaseResources, "create", lambda: None)
        assert await load_short_rates.main([]) == 2
        assert "DATABASE_URL" in capsys.readouterr().err
