#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tests for NewsService and news providers.

Strategy:
- Mock httpx for HTTP providers
- Mock yfinance for YFinanceNewsProvider
- fakeredis for the cache
- SQLite in-memory + SAAsync for persistence
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from schemas.news import RawNewsItem

# ── Data Fixtures ────────────────────────────────────────────────────────

SAMPLE_YF_NEWS = [
    {
        "uuid": "abc123",
        "title": "Airbus reçoit commande record",
        "publisher": "Reuters",
        "link": "https://reuters.com/article/abc123",
        "providerPublishTime": 1716800000,
        "type": "STORY",
        "thumbnail": {"resolutions": [{"url": "https://img.com/1.jpg"}]},
        "relatedTickers": ["AIR.PA"],
    }
]

SAMPLE_HTML_ZONEBOURSE = """
<ul class="liste-actus">
  <li>
    <h3><a href="/actualites/airbus-commande">Airbus commande record</a></h3>
    <span class="date">16/05/2026</span>
    <span class="source">Reuters</span>
  </li>
</ul>
"""

SAMPLE_HTML_BOURSORAMA = """
<div class="c-list-info-item">
  <a href="/cours/1rPAIR/actualites/airbus-order">
    <h3 class="c-link">Airbus annonce une commande record</h3>
  </a>
  <time datetime="2026-05-16T09:30:00Z">16 mai 2026</time>
  <span class="c-list-info-item__source">Reuters</span>
</div>
"""


# ── YFinanceNewsProvider ───────────────────────────────────────────────────────


class TestYFinanceNewsProvider:
    @pytest.mark.asyncio
    async def test_fetch_returns_raw_news_items(self):
        """ticker.news returned → list of normalized RawNewsItem."""
        from news.providers.yfinance_news import YFinanceNewsProvider

        provider = YFinanceNewsProvider()
        with patch.object(provider, "_fetch_sync", return_value=SAMPLE_YF_NEWS):
            items = await provider.fetch(ticker="AIR.PA")

        assert len(items) == 1
        assert items[0].title == "Airbus reçoit commande record"
        assert items[0].source == "Reuters"
        assert items[0].provider == "yfinance"
        assert items[0].url == "https://reuters.com/article/abc123"

    @pytest.mark.asyncio
    async def test_epoch_timestamp_converted_to_datetime(self):
        """providerPublishTime=1716800000 → published_at UTC datetime."""
        from news.providers.yfinance_news import YFinanceNewsProvider

        provider = YFinanceNewsProvider()
        with patch.object(provider, "_fetch_sync", return_value=SAMPLE_YF_NEWS):
            items = await provider.fetch(ticker="AIR.PA")

        assert items[0].published_at is not None
        assert items[0].published_at == datetime.fromtimestamp(1716800000, tz=timezone.utc)

    @pytest.mark.asyncio
    async def test_missing_thumbnail_handled(self):
        """No thumbnail → image_url=None, no error."""
        from news.providers.yfinance_news import YFinanceNewsProvider

        news_no_thumb = [{**SAMPLE_YF_NEWS[0]}]
        news_no_thumb[0].pop("thumbnail", None)

        provider = YFinanceNewsProvider()
        with patch.object(provider, "_fetch_sync", return_value=news_no_thumb):
            items = await provider.fetch(ticker="AIR.PA")

        assert items[0].image_url is None

    @pytest.mark.asyncio
    async def test_empty_news_returns_empty_list(self):
        """ticker.news=[] → return [] without error."""
        from news.providers.yfinance_news import YFinanceNewsProvider

        provider = YFinanceNewsProvider()
        with patch.object(provider, "_fetch_sync", return_value=[]):
            items = await provider.fetch(ticker="AIR.PA")

        assert items == []

    @pytest.mark.asyncio
    async def test_yfinance_error_returns_empty_list(self):
        """yfinance exception → return [] with warning log."""
        from news.providers.yfinance_news import YFinanceNewsProvider

        provider = YFinanceNewsProvider()
        with patch.object(provider, "_fetch_sync", side_effect=Exception("YF crash")):
            items = await provider.fetch(ticker="AIR.PA")

        assert items == []

    @pytest.mark.asyncio
    async def test_no_ticker_returns_empty(self):
        """Neither ticker nor isin → return []."""
        from news.providers.yfinance_news import YFinanceNewsProvider

        provider = YFinanceNewsProvider()
        items = await provider.fetch()
        assert items == []


# ── GoogleFinanceNewsProvider ──────────────────────────────────────────────────


class TestGoogleFinanceNewsProvider:
    def _provider(self):
        from news.providers.google_finance_news import GoogleFinanceNewsProvider

        return GoogleFinanceNewsProvider()

    def test_resolve_google_symbol_paris(self):
        """AIR.PA → ("AIR", "EPA")"""
        p = self._provider()
        symbol, exchange = p._resolve_google_symbol("AIR.PA")
        assert symbol == "AIR"
        assert exchange == "EPA"

    def test_resolve_google_symbol_xetra(self):
        """BMW.DE → ("BMW", "ETR")"""
        p = self._provider()
        symbol, exchange = p._resolve_google_symbol("BMW.DE")
        assert symbol == "BMW"
        assert exchange == "ETR"

    def test_resolve_google_symbol_us(self):
        """AAPL → ("AAPL", "NASDAQ")"""
        p = self._provider()
        symbol, exchange = p._resolve_google_symbol("AAPL")
        assert symbol == "AAPL"
        assert exchange == "NASDAQ"

    def test_parse_relative_date_hours(self):
        """ "1 hour ago" → now - 1h (precision ±1min)"""
        p = self._provider()
        result = p._parse_relative_date("1 hour ago")
        now = datetime.now(tz=timezone.utc)
        expected = now - timedelta(hours=1)
        assert result is not None
        diff = abs((result - expected).total_seconds())
        assert diff < 60

    def test_parse_relative_date_days(self):
        """ "2 days ago" → now - 2d"""
        p = self._provider()
        result = p._parse_relative_date("2 days ago")
        now = datetime.now(tz=timezone.utc)
        expected = now - timedelta(days=2)
        assert result is not None
        diff = abs((result - expected).total_seconds())
        assert diff < 60

    def test_parse_relative_date_absolute(self):
        """ "Jan 15, 2025" → 2025-01-15"""
        p = self._provider()
        result = p._parse_relative_date("Jan 15, 2025")
        assert result is not None
        assert result.year == 2025
        assert result.month == 1
        assert result.day == 15

    @pytest.mark.asyncio
    async def test_cloudflare_403_returns_empty(self):
        """_get() returns None (403) → return [] without exception."""
        from news.providers.google_finance_news import GoogleFinanceNewsProvider

        p = GoogleFinanceNewsProvider()
        with patch.object(p, "_get", new_callable=AsyncMock, return_value=None):
            items = await p.fetch(ticker="AAPL")

        assert items == []


# ── ZoneBourseNewsProvider ────────────────────────────────────────────────────


class TestZoneBourseNewsProvider:
    def _provider(self):
        from news.providers.zonebourse_news import ZoneBourseNewsProvider

        return ZoneBourseNewsProvider()

    @pytest.mark.asyncio
    async def test_parse_html_extracts_articles(self):
        """HTML ZoneBourse → articles correctly extracted."""
        p = self._provider()
        with patch.object(p, "_get", new_callable=AsyncMock, return_value=SAMPLE_HTML_ZONEBOURSE):
            items = await p.fetch(provider_url="https://www.zonebourse.com/cours/action/AIRBUS-123")

        assert len(items) >= 1
        assert any("airbus" in item.title.lower() for item in items)
        assert all(item.language == "fr" for item in items)

    def test_parse_date_slash(self):
        """16/05/2026 → 2026-05-16"""
        p = self._provider()
        result = p._parse_zonebourse_date("16/05/2026")
        assert result is not None
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 16

    def test_parse_date_relative_hours(self):
        """ "il y a 2h" (2h ago) → now - 2h"""
        p = self._provider()
        result = p._parse_zonebourse_date("il y a 2h")
        now = datetime.now(tz=timezone.utc)
        assert result is not None
        diff = abs((result - (now - timedelta(hours=2))).total_seconds())
        assert diff < 60

    def test_parse_date_hier(self):
        """ "hier" (yesterday) → yesterday"""
        p = self._provider()
        result = p._parse_zonebourse_date("hier")
        now = datetime.now(tz=timezone.utc)
        expected_day = (now - timedelta(days=1)).date()
        assert result is not None
        assert result.date() == expected_day

    def test_parse_date_french_month(self):
        """ "15 mai 2026" (May 15, 2026) → 2026-05-15"""
        p = self._provider()
        result = p._parse_zonebourse_date("15 mai 2026")
        assert result is not None
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 15


# ── NewsService ───────────────────────────────────────────────────────────────


def _make_raw_item(
    title: str = "Test Article",
    url: str = "https://example.com/article",
    provider: str = "test",
    published_at: datetime = None,
    language: str = "en",
) -> RawNewsItem:
    return RawNewsItem(
        title=title,
        url=url,
        provider=provider,
        published_at=published_at or datetime.now(tz=timezone.utc),
        language=language,
    )


class TestNewsServiceDeduplicate:
    """Tests on deduplication logic (no DB required)."""

    def _service(self):
        from news.news_service import NewsService

        db_mock = MagicMock()
        return NewsService(db_session=db_mock, redis_client=None)

    def test_deduplicate_by_exact_url(self):
        """2 articles with the same URL → only 1 kept."""
        svc = self._service()
        items = [
            _make_raw_item(url="https://example.com/article", title="Article A"),
            _make_raw_item(url="https://example.com/article", title="Article B"),
        ]
        result = svc._deduplicate(items)
        assert len(result) == 1

    def test_deduplicate_url_with_utm_params(self):
        """Same URL with/without UTM → only 1 kept."""
        svc = self._service()
        items = [
            _make_raw_item(url="https://example.com/article?utm_source=twitter"),
            _make_raw_item(url="https://example.com/article?utm_source=facebook"),
        ]
        result = svc._deduplicate(items)
        assert len(result) == 1

    def test_deduplicate_url_trailing_slash(self):
        """URL with/without trailing slash → only 1 kept."""
        svc = self._service()
        items = [
            _make_raw_item(url="https://example.com/article/"),
            _make_raw_item(url="https://example.com/article"),
        ]
        result = svc._deduplicate(items)
        assert len(result) == 1

    def test_deduplicate_by_similar_title(self):
        """2 articles with titles >85% similar → only 1 kept (the newest)."""
        svc = self._service()
        older = _make_raw_item(
            url="https://example.com/1",
            title="Airbus receives record order for 500 aircraft",
            published_at=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
        )
        newer = _make_raw_item(
            url="https://example.com/2",
            title="Airbus receives record order for 500 aircrafts",  # très similaire
            published_at=datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc),
        )
        result = svc._deduplicate([older, newer])
        assert len(result) == 1
        # The most recent is kept
        assert result[0].url == "https://example.com/2"

    def test_different_articles_not_deduplicated(self):
        """2 different articles → both are kept."""
        svc = self._service()
        items = [
            _make_raw_item(
                url="https://example.com/1",
                title="Airbus receives record order",
            ),
            _make_raw_item(
                url="https://example.com/2",
                title="Boeing reports quarterly loss",
            ),
        ]
        result = svc._deduplicate(items)
        assert len(result) == 2

    def test_sorted_by_published_at_desc(self):
        """Articles sorted by published_at DESC."""
        svc = self._service()
        items = [
            _make_raw_item(
                url="https://example.com/old",
                title="Old news",
                published_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
            ),
            _make_raw_item(
                url="https://example.com/new",
                title="New news",
                published_at=datetime(2026, 5, 16, tzinfo=timezone.utc),
            ),
        ]
        result = svc._deduplicate(items)
        assert result[0].url == "https://example.com/new"

    def test_normalize_url_lowercase(self):
        """_normalize_url lowercase + strip UTM + strip fragment."""
        svc = self._service()
        url = "https://Example.com/Article?utm_source=tw#section"
        norm = svc._normalize_url(url)
        assert "Example" not in norm
        assert "utm_source" not in norm
        assert "#section" not in norm


class TestNewsServiceCache:
    """Tests on Redis cache."""

    @pytest.mark.asyncio
    async def test_get_news_cache_hit(self):
        """
        Second identical call → returned from Redis (cached=True).
        No provider called.
        """
        from news.news_service import NewsService

        cached_data = {
            "ticker": "AAPL",
            "isin": None,
            "count": 1,
            "providers": ["yfinance"],
            "cached": False,
            "articles": [
                {
                    "id": None,
                    "title": "Apple news",
                    "url": "https://example.com/1",
                    "summary": None,
                    "image_url": None,
                    "source": None,
                    "provider": "yfinance",
                    "author": None,
                    "published_at": None,
                    "fetched_at": None,
                    "sentiment": None,
                    "sentiment_score": None,
                    "related_tickers": [],
                    "language": "en",
                }
            ],
        }

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=json.dumps(cached_data))

        db_mock = MagicMock()
        svc = NewsService(db_session=db_mock, redis_client=redis_mock)

        result = await svc.get_news("AAPL")

        assert result.cached is True
        assert result.count == 1
        redis_mock.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_news_cache_miss_calls_providers(self):
        """
        Cache miss → providers are called.
        """
        from news.news_service import NewsService

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.setex = AsyncMock()

        db_mock = AsyncMock()
        db_mock.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(
                    return_value=MagicMock(
                        first=MagicMock(return_value=None), all=MagicMock(return_value=[])
                    )
                )
            )
        )
        db_mock.commit = AsyncMock()
        db_mock.rollback = AsyncMock()

        svc = NewsService(db_session=db_mock, redis_client=redis_mock)

        # Replace all providers with mocks returning []
        for p in svc._providers:
            p.fetch = AsyncMock(return_value=[])

        result = await svc.get_news("AAPL")

        assert result.cached is False
        assert result.ticker == "AAPL"


class TestNewsServiceProviderFailure:
    """Tests on provider error resilience."""

    @pytest.mark.asyncio
    async def test_provider_exception_does_not_block(self):
        """
        One provider raises an exception → the others continue.
        """
        from news.news_service import NewsService

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.setex = AsyncMock()

        db_mock = AsyncMock()
        db_mock.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
            )
        )
        db_mock.commit = AsyncMock()
        db_mock.rollback = AsyncMock()

        svc = NewsService(db_session=db_mock, redis_client=redis_mock)

        good_item = _make_raw_item(
            title="Good article", url="https://ok.com/1", provider="good_provider"
        )

        # First provider raises an exception, second returns an article
        svc._providers[0].fetch = AsyncMock(side_effect=RuntimeError("crash"))
        for p in svc._providers[1:]:
            p.fetch = AsyncMock(return_value=[good_item])

        result = await svc.get_news("AAPL")

        # At least one article returned from functional providers
        assert result.count >= 1

    @pytest.mark.asyncio
    async def test_all_providers_fail_returns_empty(self):
        """All providers fail → empty response without exception."""
        from news.news_service import NewsService

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.setex = AsyncMock()

        db_mock = AsyncMock()
        db_mock.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
            )
        )
        db_mock.commit = AsyncMock()
        db_mock.rollback = AsyncMock()

        svc = NewsService(db_session=db_mock, redis_client=redis_mock)
        for p in svc._providers:
            p.fetch = AsyncMock(side_effect=RuntimeError("all crash"))

        result = await svc.get_news("UNKNOWN")
        assert result.count == 0
        assert result.articles == []


class TestNewsServiceUpsert:
    """Tests on database persistence."""

    @pytest.mark.asyncio
    async def test_upsert_persists_articles(self):
        """After get_news → _upsert_articles is called."""
        from news.news_service import NewsService

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.setex = AsyncMock()

        db_mock = AsyncMock()
        db_mock.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(
                    return_value=MagicMock(
                        first=MagicMock(return_value=MagicMock(id=1, isin="NL000")),
                        all=MagicMock(return_value=[]),
                    )
                )
            )
        )
        db_mock.commit = AsyncMock()
        db_mock.rollback = AsyncMock()

        svc = NewsService(db_session=db_mock, redis_client=redis_mock)
        upsert_called = []

        async def track_upsert(items, asset_id, ticker):
            upsert_called.append((asset_id, len(items)))
            return len(items)

        svc._upsert_articles = track_upsert

        good_item = _make_raw_item(title="Article", url="https://test.com/1", provider="yfinance")
        for p in svc._providers:
            p.fetch = AsyncMock(return_value=[good_item])

        await svc.get_news("AIR.PA")

        assert len(upsert_called) >= 1


class TestNewsServiceLanguageFilter:
    """Tests on language filtering."""

    @pytest.mark.asyncio
    async def test_language_filter_applied(self):
        """get_news(language='fr') → only articles with language='fr'."""
        from news.news_service import NewsService

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.setex = AsyncMock()

        db_mock = AsyncMock()
        db_mock.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
            )
        )
        db_mock.commit = AsyncMock()
        db_mock.rollback = AsyncMock()

        svc = NewsService(db_session=db_mock, redis_client=redis_mock)

        fr_item = _make_raw_item(
            title="Article FR", url="https://fr.com/1", provider="zonebourse", language="fr"
        )
        en_item = _make_raw_item(
            title="Article EN", url="https://en.com/1", provider="yfinance", language="en"
        )

        for p in svc._providers:
            p.fetch = AsyncMock(return_value=[fr_item, en_item])

        result = await svc.get_news("AIR.PA", language="fr")
        for article in result.articles:
            assert article.language == "fr"
