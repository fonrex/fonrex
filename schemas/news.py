#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pydantic schemas of the gathered financial news.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class NewsLanguage(str, Enum):
    en = "en"
    fr = "fr"
    de = "de"
    es = "es"


class NewsSentiment(str, Enum):
    positive = "positive"
    negative = "negative"
    neutral = "neutral"


class NewsArticleSchema(BaseModel):
    """Normalised news article — API output format."""

    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    title: str
    summary: Optional[str] = None
    url: str
    image_url: Optional[str] = None
    source: Optional[str] = None
    provider: str
    author: Optional[str] = None
    published_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    sentiment: Optional[NewsSentiment] = None
    sentiment_score: Optional[float] = None
    related_tickers: Optional[List[str]] = []
    language: Optional[str] = "en"


class RawNewsItem(BaseModel):
    """
    Raw article returned by a provider, before normalisation.
    Every field is optional: the providers do not all give the same
    information.
    """

    title: str
    url: str
    summary: Optional[str] = None
    image_url: Optional[str] = None
    source: Optional[str] = None
    author: Optional[str] = None
    published_at: Optional[datetime] = None
    language: Optional[str] = None
    related_tickers: Optional[List[str]] = []
    provider: str


class NewsResponse(BaseModel):
    """Answer of GET /news/{ticker}."""

    ticker: str
    isin: Optional[str] = None
    count: int
    providers: List[str] = []
    cached: bool = False
    articles: List[NewsArticleSchema] = []


class NewsFeedResponse(BaseModel):
    """Answer of GET /news/feed — global multi-asset feed."""

    count: int
    from_date: Optional[datetime] = None
    to_date: Optional[datetime] = None
    articles: List[NewsArticleSchema] = []
