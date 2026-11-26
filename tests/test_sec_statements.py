"""``financials/sec_statements.py`` without database: what happens before any query.

Storing and reading the facts is tested on PostgreSQL
(``tests/test_timescale_integration.py``). No test reaches the SEC.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from financials.sec_cik import CikResolution
from financials.sec_statements import SecStatements


def service(resolution=CikResolution(None, "No US listing in the catalogue"), **options):
    resolver, provider = MagicMock(), MagicMock()
    resolver.resolve = AsyncMock(return_value=resolution)
    provider.company_facts = AsyncMock()
    return SecStatements(None, provider, resolver, **options), provider, resolver


class TestWithoutCik:
    async def test_the_sec_is_never_asked(self):
        statements, provider, resolver = service()

        load = await statements.refresh(7)

        assert (load.status, load.cik, load.reason) == (
            "no_cik",
            None,
            "No US listing in the catalogue",
        )
        resolver.resolve.assert_awaited_once_with(7, refresh=False)
        provider.company_facts.assert_not_awaited()

    async def test_no_fiscal_years_whether_read_or_not(self):
        statements, provider, _ = service()
        assert await statements.annual(7) == (await statements.refresh(7), [])
        load, years = await statements.annual(7, refresh=False)
        assert (load.status, years) == ("no_cik", [])
        provider.company_facts.assert_not_awaited()


class TestRefreshDelay:
    def test_follows_the_environment(self, monkeypatch):
        monkeypatch.setenv("SEC_XBRL_REFRESH_HOURS", "72")
        assert service()[0].refresh_after == timedelta(hours=72)

    def test_an_unusable_value_keeps_a_day(self, monkeypatch):
        monkeypatch.setenv("SEC_XBRL_REFRESH_HOURS", "0")
        assert service()[0].refresh_after == timedelta(hours=24)

    def test_a_reading_without_time_zone_is_in_utc(self):
        now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        statements = service(now=lambda: now)[0]
        assert statements._is_recent(datetime(2026, 10, 16, 1, 0))
        assert not statements._is_recent(now - timedelta(hours=25))
        assert not statements._is_recent(None)


class TestScript:
    """``scripts/load_sec.py``: read the SEC filings of the catalogue again."""

    def test_the_arguments(self):
        from scripts import load_sec

        assert vars(load_sec.parse_arguments([])) == {
            "ticker": None,
            "force": False,
            "recheck_cik": False,
        }
        arguments = load_sec.parse_arguments(
            ["--ticker", "AAPL", "JPM", "--force", "--recheck-cik"]
        )
        assert (arguments.ticker, arguments.force, arguments.recheck_cik) == (
            ["AAPL", "JPM"],
            True,
            True,
        )

    def test_a_line_per_share(self):
        from datetime import date

        from financials.sec_statements import SecLoad
        from scripts import load_sec

        read = SecLoad("fetched", "0000320193", "Apple Inc.", None, date(2025, 10, 31), 412)
        assert load_sec.describe("AAPL", read) == (
            "AAPL           fetched   0000320193  412 facts, last filed 2025-10-31"
        )
        assert load_sec.describe("JPM", SecLoad("failed", "0000019617", reason="down")) == (
            "JPM            failed    0000019617  nothing stored  (down)"
        )
        assert load_sec.describe(
            "AIR.PA", SecLoad("no_cik", reason="No US listing in the catalogue")
        ) == ("AIR.PA         no CIK    No US listing in the catalogue")

    async def test_force_and_recheck_reach_every_company(self, monkeypatch, capsys):
        from financials.sec_statements import SecLoad
        from scripts import load_sec

        class Resources:
            session_factory = None

            async def close(self):
                pass

        calls = []

        async def shares(session_factory, tickers):
            return [("AAPL", 1), ("AIR.PA", 2), ("NOPE", None)]

        async def refresh(self, asset_id, force=False, *, recheck_cik=False):
            calls.append((asset_id, force, recheck_cik))
            if asset_id == 2:
                return SecLoad("no_cik", reason="No US listing in the catalogue")
            return SecLoad("fetched", "0000320193", "Apple Inc.", None, None, 10)

        monkeypatch.setattr(load_sec.AsyncDatabaseResources, "create", lambda: Resources())
        monkeypatch.setattr(load_sec, "instruments", shares)
        monkeypatch.setattr(load_sec.SecStatements, "refresh", refresh)

        assert await load_sec.main(["--force"]) == 1  # NOPE is not in the catalogue
        assert calls == [(1, True, False), (2, True, False)]
        lines = capsys.readouterr().out.splitlines()
        assert lines[-1] == "3 shares: 1 read, 1 without CIK, 0 failed, 1 unknown"

    async def test_after_errors_in_a_row_the_reading_stops(self, monkeypatch, capsys):
        from scripts import load_sec

        class Resources:
            session_factory = None

            async def close(self):
                pass

        async def shares(session_factory, tickers):
            return [(f"S{number:03d}", number) for number in range(1, 31)]

        async def refresh(self, asset_id, force=False, *, recheck_cik=False):
            raise ConnectionError("the database system is in recovery mode")

        monkeypatch.setattr(load_sec.AsyncDatabaseResources, "create", lambda: Resources())
        monkeypatch.setattr(load_sec, "instruments", shares)
        monkeypatch.setattr(load_sec.SecStatements, "refresh", refresh)

        assert await load_sec.main([]) == 1
        lines = capsys.readouterr().out.splitlines()
        assert len([line for line in lines if " error " in line]) == load_sec.ERRORS_IN_A_ROW
        assert lines[-1].startswith("Stopped at S020: 20 errors in a row.")
