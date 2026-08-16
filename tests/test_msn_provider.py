# -*- coding: utf-8 -*-
"""
Unit tests for MSNFinanceNewsProvider.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from news.providers.msn_finance_news import MSNFinanceNewsProvider

SAMPLE_MSN_JSON_RESPONSE = {
    "subCards": [
        {
            "title": "Microsoft reports record revenue",
            "url": "https://reuters.com/msft-revenue",
            "publishedDateTime": "2026-05-16T12:00:00Z",
            "provider": "Reuters",
            "imageUrl": "https://img.com/msft.jpg",
        },
        {
            "headline": "Windows 12 launched",
            "link": "https://ap.com/win12",
            "datePublished": "2026-05-16T13:00:00Z",
            "source": "AP News",
            "thumbnail": {"url": "https://img.com/win12.jpg"},
        },
    ]
}

SAMPLE_MSN_HTML_RESPONSE = """
<div class="newsitem">
  <a href="/en-us/money/news/msft-stock-soars">
    <span class="title">MSFT Stock Soars</span>
  </a>
  <time datetime="2026-05-16T16:00:00Z">2 hours ago</time>
</div>
"""


def test_msn_parse_msn_date():
    p = MSNFinanceNewsProvider()
    assert p._parse_msn_date("2026-05-16T12:00:00Z") == datetime(
        2026, 5, 16, 12, 0, tzinfo=timezone.utc
    )
    assert p._parse_msn_date(None) is None
    assert p._parse_msn_date("invalid-date") is None


def test_msn_extract_image():
    p = MSNFinanceNewsProvider()
    assert p._extract_image({"imageUrl": "https://img.com/1.jpg"}) == "https://img.com/1.jpg"
    assert p._extract_image({"image": {"url": "https://img.com/2.jpg"}}) == "https://img.com/2.jpg"
    assert (
        p._extract_image({"thumbnail": {"imageUrl": "https://img.com/3.jpg"}})
        == "https://img.com/3.jpg"
    )
    assert p._extract_image({}) is None


@pytest.mark.asyncio
async def test_msn_fetch_json_path():
    p = MSNFinanceNewsProvider()
    with patch.object(
        p, "_get_json", new_callable=AsyncMock, return_value=SAMPLE_MSN_JSON_RESPONSE
    ):
        items = await p._fetch_json("MSFT", limit=10)
        assert len(items) == 2
        assert items[0].title == "Microsoft reports record revenue"
        assert items[0].url == "https://reuters.com/msft-revenue"
        assert items[0].source == "Reuters"
        assert items[0].published_at == datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
        assert items[0].image_url == "https://img.com/msft.jpg"

        assert items[1].title == "Windows 12 launched"
        assert items[1].url == "https://ap.com/win12"
        assert items[1].source == "AP News"
        assert items[1].image_url == "https://img.com/win12.jpg"


@pytest.mark.asyncio
async def test_msn_fetch_html_path():
    p = MSNFinanceNewsProvider()
    with patch.object(p, "_get", new_callable=AsyncMock, return_value=SAMPLE_MSN_HTML_RESPONSE):
        items = await p._fetch_html("MSFT", limit=10)
        assert len(items) == 1
        assert items[0].title == "MSFT Stock Soars"
        assert items[0].url == "https://www.msn.com/en-us/money/news/msft-stock-soars"
        assert items[0].published_at == datetime(2026, 5, 16, 16, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_msn_fetch_orchestration():
    p = MSNFinanceNewsProvider()
    # Test JSON path success
    with patch.object(p, "_fetch_json", new_callable=AsyncMock, return_value=[1, 2]) as mock_json:
        with patch.object(p, "_fetch_html", new_callable=AsyncMock) as mock_html:
            items = await p.fetch(ticker="MSFT")
            assert items == [1, 2]
            mock_json.assert_called_once()
            mock_html.assert_not_called()

    # Test JSON path empty fallback to HTML path
    with patch.object(p, "_fetch_json", new_callable=AsyncMock, return_value=[]) as mock_json:
        with patch.object(p, "_fetch_html", new_callable=AsyncMock, return_value=[3]) as mock_html:
            items = await p.fetch(ticker="MSFT")
            assert items == [3]
            mock_json.assert_called_once()
            mock_html.assert_called_once()

    # Test no symbol
    assert await p.fetch(ticker=None, isin=None) == []
