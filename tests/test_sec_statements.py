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
