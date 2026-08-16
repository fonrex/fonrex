# -*- coding: utf-8 -*-
"""
Unit tests for BoursoramaNewsProvider.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from news.providers.boursorama_news import BoursoramaNewsProvider

SAMPLE_BOURSORAMA_HTML = """
<div class="c-list-info-item">
  <a href="/cours/1rPAIR/actualites/airbus-order">
    <h3 class="c-link">Airbus annonce une commande record</h3>
  </a>
  <time datetime="2026-05-16T09:30:00Z">16 mai 2026</time>
  <span class="c-list-info-item__source">Reuters</span>
</div>
<div class="c-list-info-item">
  <a href="/actualites/another-article">
    <span class="title">Titre Article 2</span>
  </a>
  <time>il y a 3 h</time>
</div>
<div class="c-list-info-item">
  <a href="https://example.com/article3">
    <h4 class="headline">Titre Article 3</h4>
  </a>
  <time>16/05/2026</time>
  <span class="provider">AFP</span>
</div>
"""


def test_boursorama_build_url():
    p = BoursoramaNewsProvider()
    assert (
        p._build_url("https://www.boursorama.com/cours/AIR", None, None)
        == "https://www.boursorama.com/cours/AIR/actualites"
    )
    assert (
        p._build_url(None, "1rPAIR", None) == "https://www.boursorama.com/cours/1rPAIR/actualites"
    )
    assert p._build_url(None, None, "AIR.PA") == "https://www.boursorama.com/recherche/?q=AIR"
    assert p._build_url(None, None, None) is None


def test_boursorama_parse_fr_date():
    p = BoursoramaNewsProvider()
    now = datetime.now(tz=timezone.utc)

    # ISO 8601
    assert p._parse_fr_date("2026-05-16T09:30:00Z") == datetime(
        2026, 5, 16, 9, 30, tzinfo=timezone.utc
    )

    # Relative hours
    dt_hours = p._parse_fr_date("il y a 4 h")
    assert dt_hours is not None
    assert abs((now - dt_hours).total_seconds() - 4 * 3600) < 10

    # Relative minutes
    dt_mins = p._parse_fr_date("il y a 15 min")
    assert dt_mins is not None
    assert abs((now - dt_mins).total_seconds() - 15 * 60) < 10

    # Yesterday
    dt_yesterday = p._parse_fr_date("hier")
    assert dt_yesterday is not None
    assert 12 * 3600 < (now - dt_yesterday).total_seconds() < 36 * 3600

    # Month parsing
    assert p._parse_fr_date("15 mai 2026") == datetime(2026, 5, 15, tzinfo=timezone.utc)

    # Slash date
    assert p._parse_fr_date("16/05/2026") == datetime(2026, 5, 16, tzinfo=timezone.utc)

    # Invalid date
    assert p._parse_fr_date("date inconnue") is None


def test_boursorama_parse_html():
    p = BoursoramaNewsProvider()
    items = p._parse(SAMPLE_BOURSORAMA_HTML, limit=10)

    assert len(items) == 3
    assert items[0].title == "Airbus annonce une commande record"
    assert items[0].url == "https://www.boursorama.com/cours/1rPAIR/actualites/airbus-order"
    assert items[0].source == "Reuters"
    assert items[0].published_at == datetime(2026, 5, 16, 9, 30, tzinfo=timezone.utc)
    assert items[0].language == "fr"
    assert items[0].provider == "boursorama"

    assert items[1].title == "Titre Article 2"
    assert items[1].url == "https://www.boursorama.com/actualites/another-article"
    assert items[1].published_at is not None

    assert items[2].title == "Titre Article 3"
    assert items[2].url == "https://example.com/article3"
    assert items[2].source == "AFP"
    assert items[2].published_at == datetime(2026, 5, 16, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_boursorama_fetch():
    p = BoursoramaNewsProvider()
    with patch.object(p, "_get", new_callable=AsyncMock, return_value=SAMPLE_BOURSORAMA_HTML):
        items = await p.fetch(ticker="AIR.PA")
        assert len(items) == 3

    with patch.object(p, "_get", new_callable=AsyncMock, return_value=None):
        items = await p.fetch(ticker="AIR.PA")
        assert items == []
