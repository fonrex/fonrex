#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
NewsService — Agrège les news depuis plusieurs providers.

Design :
- Appels async en parallèle sur tous les providers disponibles
- Déduplication par URL (clé unique) puis par titre similaire
- Persistance dans news_articles (PostgreSQL)
- Cache Redis 30min par (ticker, limit)
- Fallback gracieux : si un provider échoue, les autres continuent
"""

import asyncio
import difflib
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import Asset, AssetListing, AssetMapping, NewsArticle
from news.providers.boursorama_news import BoursoramaNewsProvider
from news.providers.google_finance_news import GoogleFinanceNewsProvider
from news.providers.investing_news import InvestingComNewsProvider
from news.providers.marketwatch_news import MarketWatchNewsProvider
from news.providers.msn_finance_news import MSNFinanceNewsProvider
from news.providers.yfinance_news import YFinanceNewsProvider
from news.providers.zonebourse_news import ZoneBourseNewsProvider
from schemas.news import NewsArticleSchema, NewsFeedResponse, NewsResponse, RawNewsItem

logger = logging.getLogger(__name__)

REDIS_NEWS_KEY = "news:{ticker}:{limit}"
REDIS_FEED_KEY = "news:feed:{limit}:{language}"
REDIS_NEWS_TTL = int(__import__("os").environ.get("NEWS_CACHE_TTL", 1800))

_UTM_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_eid",
    "mc_cid",
}


class NewsService:
    """Orchestrateur d'agrégation de news financières."""

    PROVIDERS = [
        YFinanceNewsProvider,
        GoogleFinanceNewsProvider,
        ZoneBourseNewsProvider,
        BoursoramaNewsProvider,
        InvestingComNewsProvider,
        MarketWatchNewsProvider,
        MSNFinanceNewsProvider,
    ]

    def __init__(self, db_session=None, redis_client=None, session_factory=None):
        # ``db_session`` remains supported for isolated unit tests. Production
        # uses the factory so concurrent requests never share an AsyncSession.
        self.db = db_session
        self._session_factory = session_factory
        self.redis = redis_client
        self._providers = [P() for P in self.PROVIDERS]

    @asynccontextmanager
    async def _session(self):
        if self._session_factory is not None:
            async with self._session_factory() as session:
                yield session
            return
        if self.db is None:
            raise RuntimeError("NewsService database session is not configured")
        yield self.db

    # ── Point d'entrée principal ─────────────────────────────────────────────

    async def get_news(
        self,
        ticker: str,
        limit: int = 20,
        language: Optional[str] = None,
        force_refresh: bool = False,
        asset_id: Optional[int] = None,
    ) -> NewsResponse:
        """
        Retourne les news d'un ticker depuis tous les providers.
        """
        cache_key = REDIS_NEWS_KEY.format(ticker=ticker, limit=limit)

        # 1. Cache Redis
        if not force_refresh:
            cached = await self._get_cache(cache_key)
            if cached:
                return NewsResponse(**{**cached, "cached": True})

        # 2. Résoudre l'asset
        asset = await self._resolve_asset(ticker)
        if asset:
            asset_id = asset_id or asset.id
            isin = asset.isin
        else:
            isin = None

        # 3. Mappings providers
        mappings: Dict = {}
        if asset_id:
            mappings = await self._get_provider_mappings(asset_id)

        # 4. Lancer tous les providers en parallèle
        raw_items = await self._fetch_from_all_providers(ticker, mappings, limit * 2)

        # 5. Filtrer par langue si demandé
        if language:
            raw_items = [i for i in raw_items if not i.language or i.language == language]

        # 6. Dédupliquer
        deduped = self._deduplicate(raw_items)[:limit]

        # 7. Persister
        if asset_id:
            await self._upsert_articles(deduped, asset_id, ticker)

        # 8. Construire la réponse
        providers_used = list({i.provider for i in deduped})
        articles = [
            NewsArticleSchema(
                title=item.title,
                url=item.url,
                summary=item.summary,
                image_url=item.image_url,
                source=item.source,
                provider=item.provider,
                author=item.author,
                published_at=item.published_at,
                related_tickers=item.related_tickers or [],
                language=item.language or "en",
            )
            for item in deduped
        ]

        response = NewsResponse(
            ticker=ticker,
            isin=isin,
            count=len(articles),
            providers=providers_used,
            cached=False,
            articles=articles,
        )

        # 9. Mettre en cache
        await self._set_cache(cache_key, response.model_dump())

        return response

    async def get_feed(
        self,
        limit: int = 50,
        language: Optional[str] = None,
        from_date: Optional[datetime] = None,
        ticker_filter: Optional[List[str]] = None,
    ) -> NewsFeedResponse:
        """
        Retourne un feed global des dernières news depuis news_articles en base.
        """
        query = select(NewsArticle).order_by(NewsArticle.published_at.desc())

        if language:
            query = query.where(NewsArticle.language == language)

        if from_date:
            query = query.where(NewsArticle.published_at >= from_date)

        if ticker_filter:
            # Filtre sur related_tickers (ARRAY @> ARRAY)
            query = query.where(
                NewsArticle.related_tickers.overlap(ticker_filter)  # type: ignore[attr-defined]
            )

        query = query.limit(limit)

        try:
            async with self._session() as db:
                result = await db.execute(query)
                rows = result.scalars().all()
        except Exception as exc:
            logger.error("[NewsService] get_feed DB error: %s", exc)
            rows = []

        articles = [NewsArticleSchema.model_validate(row) for row in rows]
        oldest = articles[-1].published_at if articles else None
        newest = articles[0].published_at if articles else None

        return NewsFeedResponse(
            count=len(articles),
            from_date=oldest,
            to_date=newest,
            articles=articles,
        )

    # ── Orchestration providers ──────────────────────────────────────────────

    async def _fetch_from_all_providers(
        self,
        ticker: str,
        mappings: Dict,
        limit: int,
    ) -> List[RawNewsItem]:
        """
        Lance tous les providers en parallèle via asyncio.gather.
        """
        tasks = []
        for provider in self._providers:
            mapping_info = mappings.get(provider.name, {})
            tasks.append(
                self._safe_fetch(
                    provider,
                    ticker=ticker,
                    provider_url=mapping_info.get("url"),
                    provider_ticker=mapping_info.get("ticker"),
                    limit=limit,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_items: List[RawNewsItem] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("[NewsService] Provider exception (should not happen): %s", result)
            elif isinstance(result, list):
                all_items.extend(result)

        return all_items

    async def _safe_fetch(self, provider, **kwargs) -> List[RawNewsItem]:
        """Wrapper qui garantit qu'un provider ne propage jamais d'exception."""
        try:
            return await provider.fetch(**kwargs) or []
        except Exception as exc:
            logger.warning("[NewsService] Provider %s échoué: %s", provider.name, exc)
            return []

    # ── Déduplication ────────────────────────────────────────────────────────

    @staticmethod
    def _to_utc(dt: Optional[datetime]) -> datetime:
        """Normalise un datetime en UTC-aware. Naive → assume UTC."""
        if dt is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _deduplicate(self, items: List[RawNewsItem]) -> List[RawNewsItem]:
        """
        Étape 1 : déduplication stricte par URL normalisée.
        Étape 2 : déduplication par titre similaire (> 85%).
        Tri final par published_at DESC (nulls en dernier).
        """
        # Étape 1 — URL
        seen_urls: dict = {}
        for item in items:
            norm = self._normalize_url(item.url)
            if norm not in seen_urls:
                seen_urls[norm] = item

        deduped = list(seen_urls.values())

        # Étape 2 — titres similaires
        threshold = float(__import__("os").environ.get("NEWS_DEDUP_SIMILARITY", 0.85))
        final: List[RawNewsItem] = []
        for item in deduped:
            normalized_title = re.sub(r"[^\w\s]", "", item.title.lower())
            is_dup = False
            for i, existing in enumerate(final):
                existing_norm = re.sub(r"[^\w\s]", "", existing.title.lower())
                ratio = difflib.SequenceMatcher(None, normalized_title, existing_norm).ratio()
                if ratio >= threshold:
                    # Garder le plus récent (comparaison normalisée UTC)
                    if self._to_utc(item.published_at) > self._to_utc(existing.published_at):
                        final[i] = item
                    is_dup = True
                    break
            if not is_dup:
                final.append(item)

        # Tri par published_at DESC, nulls en dernier
        final.sort(
            key=lambda x: self._to_utc(x.published_at),
            reverse=True,
        )
        return final

    def _normalize_url(self, url: str) -> str:
        """
        Normalise une URL :
        - Lowercase
        - Supprime les paramètres UTM et tracking
        - Supprime les fragments
        - Retire le trailing slash
        """
        try:
            parsed = urlparse(url.lower())
            qs = parse_qs(parsed.query, keep_blank_values=False)
            clean_qs = {k: v for k, v in qs.items() if k not in _UTM_PARAMS}
            clean_query = urlencode(clean_qs, doseq=True)
            normalized = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path.rstrip("/"),
                    "",
                    clean_query,
                    "",
                )
            )
            return normalized
        except Exception:
            return url.lower().rstrip("/")

    # ── Persistance ──────────────────────────────────────────────────────────

    async def _upsert_articles(
        self,
        items: List[RawNewsItem],
        asset_id: int,
        ticker: str,
    ) -> int:
        """
        Upsert des articles dans news_articles.
        ON CONFLICT (url) → met à jour title, summary, published_at.
        Traite par batch de 50.
        """
        if not items:
            return 0

        total = 0
        batch_size = 50

        async with self._session() as db:
            for i in range(0, len(items), batch_size):
                batch = items[i : i + batch_size]
                rows = []
                for item in batch:
                    if not item.url:
                        continue
                    rows.append(
                        {
                            "asset_id": asset_id,
                            "title": item.title[:500],
                            "summary": item.summary,
                            "url": item.url[:1000],
                            "image_url": item.image_url,
                            "source": item.source,
                            "provider": item.provider,
                            "author": item.author,
                            "published_at": item.published_at,
                            "related_tickers": item.related_tickers or [],
                            "language": item.language or "en",
                        }
                    )

                if not rows:
                    continue

                try:
                    stmt = pg_insert(NewsArticle).values(rows)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["url"],
                        set_={
                            "title": stmt.excluded.title,
                            "summary": stmt.excluded.summary,
                            "published_at": stmt.excluded.published_at,
                        },
                    )
                    await db.execute(stmt)
                    await db.commit()
                    total += len(rows)
                except Exception as exc:
                    logger.error("[NewsService] Upsert batch error: %s", exc)
                    await db.rollback()

        return total

    # ── Cache Redis ──────────────────────────────────────────────────────────

    async def _get_cache(self, key: str) -> Optional[dict]:
        if not self.redis:
            return None
        try:
            raw = await self.redis.get(key)
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning("[NewsCache] Lecture échouée: %s", e)
            return None

    async def _set_cache(self, key: str, data: dict) -> None:
        if not self.redis:
            return
        try:
            await self.redis.setex(key, REDIS_NEWS_TTL, json.dumps(data, default=str))
        except Exception as e:
            logger.warning("[NewsCache] Écriture échouée: %s", e)

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _resolve_asset(self, ticker: str) -> Optional[Asset]:
        """Résout l'Asset depuis son ticker via asset_listings."""
        try:
            stmt = (
                select(Asset)
                .join(AssetListing, AssetListing.asset_id == Asset.id)
                .where(AssetListing.ticker == ticker)
                .where(AssetListing.is_active == True)  # noqa: E712
                .limit(1)
            )
            async with self._session() as db:
                result = await db.execute(stmt)
                return result.scalars().first()
        except Exception as exc:
            logger.warning("[NewsService] _resolve_asset(%s) error: %s", ticker, exc)
            return None

    async def _get_provider_mappings(self, asset_id: int) -> Dict:
        """
        Retourne les mappings providers actifs pour un actif.
        Format : {"zonebourse": {"url": "...", "ticker": "..."}, ...}
        """
        try:
            stmt = (
                select(AssetMapping)
                .where(AssetMapping.asset_id == asset_id)
                .where(AssetMapping.is_active == True)  # noqa: E712
            )
            async with self._session() as db:
                result = await db.execute(stmt)
                mappings_rows = result.scalars().all()

            mappings: Dict = {}
            for row in mappings_rows:
                provider_key = row.provider_name.lower()
                mappings[provider_key] = {
                    "url": row.provider_url,
                    "ticker": row.provider_ticker,
                }
            return mappings
        except Exception as exc:
            logger.warning("[NewsService] _get_provider_mappings(%d) error: %s", asset_id, exc)
            return {}

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        """Statistiques sur les news en base."""
        try:
            results = {}
            async with self._session() as db:
                total_q = await db.execute(text("SELECT COUNT(*) FROM news_articles"))
                results["total_articles"] = total_q.scalar()

                provider_q = await db.execute(
                    text(
                        "SELECT provider, COUNT(*) as count "
                        "FROM news_articles GROUP BY provider ORDER BY count DESC"
                    )
                )
                results["by_provider"] = {row[0]: row[1] for row in provider_q}

                lang_q = await db.execute(
                    text(
                        "SELECT language, COUNT(*) as count "
                        "FROM news_articles GROUP BY language ORDER BY count DESC"
                    )
                )
                results["by_language"] = {row[0]: row[1] for row in lang_q}

                last_q = await db.execute(text("SELECT MAX(fetched_at) FROM news_articles"))
                results["last_fetched_at"] = last_q.scalar()

                top_q = await db.execute(
                    text(
                        "SELECT a.ticker, COUNT(n.id) as count "
                        "FROM news_articles n "
                        "JOIN assets a ON a.id = n.asset_id "
                        "GROUP BY a.ticker ORDER BY count DESC LIMIT 10"
                    )
                )
                results["top_assets"] = {row[0]: row[1] for row in top_q}

            return results
        except Exception as exc:
            logger.error("[NewsService] get_stats error: %s", exc)
            return {}
