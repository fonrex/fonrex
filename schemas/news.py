#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Schémas Pydantic pour les news financières agrégées.
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
    """Article de news normalisé — format de sortie API."""

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
    Article brut retourné par un provider avant normalisation.
    Tous les champs sont optionnels — les providers ne fournissent pas tous
    les mêmes informations.
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
    """Réponse de GET /news/{ticker}."""

    ticker: str
    isin: Optional[str] = None
    count: int
    providers: List[str] = []
    cached: bool = False
    articles: List[NewsArticleSchema] = []


class NewsFeedResponse(BaseModel):
    """Réponse de GET /news/feed — feed global multi-actifs."""

    count: int
    from_date: Optional[datetime] = None
    to_date: Optional[datetime] = None
    articles: List[NewsArticleSchema] = []
