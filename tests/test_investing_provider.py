# -*- coding: utf-8 -*-
"""
Unit tests for InvestingComNewsProvider.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from news.providers.investing_news import InvestingComNewsProvider

SAMPLE_INVESTING_HTML = """
<div class="articleItem">
  <a href="/news/stock-market-news/apple-soars-12345">
    <span class="title">Apple soars after earnings beat</span>
  </a>
  <time datetime="2026-05-16T14:00:00Z">3 hours ago</time>
  <span class="provider">Reuters</span>
  <img src="https://img.com/apple.jpg"/>
</div>
<div class="articleItem">
  <a href="https://example.com/item2">
    <h3 class="headline">Another market event</h3>
  </a>
  <time datetime="2026-05-16T15:00:00Z">2 hours ago</time>
  <span class="details">Bloomberg</span>
  <img data-src="https://img.com/item2.jpg"/>
</div>
"""

SAMPLE_CLOUDFLARE_HTML = """
<html>
  <head><title>Just a moment...</title></head>
  <body>
    <div class="cf-browser-verification">Verifying your browser...</div>
  </body>
</html>
"""


def test_investing_build_url():
    p = InvestingComNewsProvider()
    assert (
        p._build_url("https://www.investing.com/equities/apple-computer-inc-news", None)
        == "https://www.investing.com/equities/apple-computer-inc-news"
    )
    assert (
        p._build_url("https://www.investing.com/equities/apple-computer-inc", None)
        == "https://www.investing.com/equities/apple-computer-inc-news"
    )
    assert (
        p._build_url(None, "apple-computer-inc")
        == "https://www.investing.com/equities/apple-computer-inc-news"
    )
    assert (
        p._build_url("https://www.investing.com/equities/tesla-motors", None)
        == "https://www.investing.com/equities/tesla-motors-news"
    )
    assert p._build_url(None, "eads") == "https://www.investing.com/equities/eads-news"
    assert p._build_url(None, None) is None


def test_investing_build_url_only_follows_investing_equity_pages():
    p = InvestingComNewsProvider()
    # Another site, or a path outside /equities/, is never followed.
    assert p._build_url("http://169.254.169.254/latest/meta-data", None) is None
    assert p._build_url("https://evil.example/equities/eads", None) is None
    assert p._build_url(None, "../../admin") is None
    assert p._build_url(None, "eads/../admin") is None
    # A refused URL falls back to the provider ticker, as for ZoneBourse.
    assert (
        p._build_url("https://evil.example/equities/eads", "eads")
        == "https://www.investing.com/equities/eads-news"
    )
    # The French edition is kept.
    assert (
        p._build_url("https://fr.investing.com/equities/eads", None)
        == "https://fr.investing.com/equities/eads-news"
    )


def test_investing_parse_html():
    p = InvestingComNewsProvider()
    items = p._parse(SAMPLE_INVESTING_HTML, limit=10)

    assert len(items) == 2
    assert items[0].title == "Apple soars after earnings beat"
    assert items[0].url == "https://www.investing.com/news/stock-market-news/apple-soars-12345"
    assert items[0].source == "Reuters"
    assert items[0].published_at == datetime(2026, 5, 16, 14, 0, tzinfo=timezone.utc)
    assert items[0].image_url == "https://img.com/apple.jpg"
    assert items[0].language == "en"
    assert items[0].provider == "investing_com"

    assert items[1].title == "Another market event"
    assert items[1].url == "https://example.com/item2"
    assert items[1].source == "Bloomberg"
    assert items[1].published_at == datetime(2026, 5, 16, 15, 0, tzinfo=timezone.utc)
    assert items[1].image_url == "https://img.com/item2.jpg"


@pytest.mark.asyncio
async def test_investing_cloudflare_detection():
    p = InvestingComNewsProvider()
    with patch.object(p, "_get", new_callable=AsyncMock, return_value=SAMPLE_CLOUDFLARE_HTML):
        items = await p.fetch(provider_ticker="apple-computer-inc")
        assert items == []


@pytest.mark.asyncio
async def test_investing_fetch_nominal():
    p = InvestingComNewsProvider()
    with patch.object(p, "_get", new_callable=AsyncMock, return_value=SAMPLE_INVESTING_HTML):
        items = await p.fetch(provider_ticker="apple-computer-inc")
        assert len(items) == 2


def test_investing_parse_current_article_item_layout():
    html = """
    <article data-test="article-item" class="flex py-6">
      <figure><a href="https://www.investing.com/news/stock-market-news/tesla-registrations-4990675">
        <img src="https://content-media.investing.com/trkd-images/LYNXNPEMA10O0_L.jpg" data-test="item-image">
      </a></figure>
      <div>
        <a data-test="article-title-link" href="/news/stock-market-news/tesla-registrations-4990675">
          Tesla’s monthly registrations surge in Northern Europe
        </a>
        <ul>
          <li><span>By</span><span data-test="news-provider-name">Reuters</span></li>
          <li><time data-test="article-publish-date" datetime="2026-11-02 11:25:50">1 hour ago</time></li>
        </ul>
      </div>
    </article>
    """
    items = InvestingComNewsProvider()._parse(html, limit=10)
    assert len(items) == 1
    assert items[0].title == "Tesla’s monthly registrations surge in Northern Europe"
    assert (
        items[0].url
        == "https://www.investing.com/news/stock-market-news/tesla-registrations-4990675"
    )
    assert items[0].source == "Reuters"
    assert items[0].published_at == datetime(2026, 11, 2, 11, 25, 50, tzinfo=timezone.utc)
    assert (
        items[0].image_url == "https://content-media.investing.com/trkd-images/LYNXNPEMA10O0_L.jpg"
    )
