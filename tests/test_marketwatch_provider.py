# -*- coding: utf-8 -*-
"""
Unit tests for MarketWatchNewsProvider.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from news.providers.marketwatch_news import MarketWatchNewsProvider

SAMPLE_MARKETWATCH_HTML = """
<div class="article__content">
  <a href="/investing/stock/aapl/story/apple-upgrade-123">
    <h3 class="headline">Apple upgraded to buy</h3>
  </a>
  <time datetime="2026-05-16T10:00:00Z">3 hours ago</time>
  <span class="author">John Doe</span>
</div>
<div class="article__content">
  <a href="https://example.com/mw-story">
    <span class="title">Fed raises interest rates</span>
  </a>
  <span class="pubdate" data-est="2026-05-16T11:00:00Z">2 hours ago</span>
  <span class="byline">Jane Smith</span>
</div>
"""


def test_marketwatch_build_url():
    p = MarketWatchNewsProvider()

    # US stock
    url, params = p._build_url("AAPL")
    assert url == "https://www.marketwatch.com/investing/stock/aapl/news"
    assert params is None

    # EU stock with suffix PA (France)
    url_fr, params_fr = p._build_url("AIR.PA")
    assert url_fr == "https://www.marketwatch.com/investing/stock/air/news"
    assert params_fr == {"countrycode": "fr"}

    # EU stock with suffix DE (Germany)
    url_de, params_de = p._build_url("SAP.DE")
    assert url_de == "https://www.marketwatch.com/investing/stock/sap/news"
    assert params_de == {"countrycode": "de"}


def test_marketwatch_parse_html():
    p = MarketWatchNewsProvider()
    items = p._parse(SAMPLE_MARKETWATCH_HTML, limit=10)

    assert len(items) == 2
    assert items[0].title == "Apple upgraded to buy"
    assert (
        items[0].url == "https://www.marketwatch.com/investing/stock/aapl/story/apple-upgrade-123"
    )
    assert items[0].source == "MarketWatch"
    assert items[0].author == "John Doe"
    assert items[0].published_at == datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc)
    assert items[0].language == "en"
    assert items[0].provider == "marketwatch"

    assert items[1].title == "Fed raises interest rates"
    assert items[1].url == "https://example.com/mw-story"
    assert items[1].author == "Jane Smith"
    assert items[1].published_at == datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_marketwatch_fetch_nominal():
    p = MarketWatchNewsProvider()
    with patch.object(p, "_get", new_callable=AsyncMock, return_value=SAMPLE_MARKETWATCH_HTML):
        items = await p.fetch(ticker="AAPL")
        assert len(items) == 2


@pytest.mark.asyncio
async def test_marketwatch_fetch_empty():
    p = MarketWatchNewsProvider()
    # Missing symbol/ticker
    assert await p.fetch(ticker=None, isin=None) == []

    # GET returns None
    with patch.object(p, "_get", new_callable=AsyncMock, return_value=None):
        assert await p.fetch(ticker="AAPL") == []
