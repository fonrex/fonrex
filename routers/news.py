"""HTTP routes for financial news aggregation."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from news.news_service import NewsService
from schemas.news import NewsFeedResponse, NewsResponse
from settings import env_int

router = APIRouter(prefix="/news", tags=["News"])

# Largest number of articles one request may ask for.
NEWS_MAX_LIMIT = env_int("NEWS_MAX_LIMIT", 100, minimum=1)
# Number of articles returned for a ticker when the request gives no limit.
NEWS_DEFAULT_LIMIT = min(env_int("NEWS_DEFAULT_LIMIT", 20, minimum=1), NEWS_MAX_LIMIT)
NEWS_FEED_DEFAULT_LIMIT = min(50, NEWS_MAX_LIMIT)


def get_news_service(request: Request) -> NewsService:
    service = getattr(request.app.state, "news_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="NewsService indisponible")
    return service


@router.get("/stats")
async def get_news_stats(service: NewsService = Depends(get_news_service)):
    """Return global news persistence statistics."""
    return await service.get_stats()


@router.get("/feed", response_model=NewsFeedResponse)
async def get_news_feed(
    limit: int = Query(default=NEWS_FEED_DEFAULT_LIMIT, ge=1, le=NEWS_MAX_LIMIT),
    language: Optional[str] = None,
    tickers: Optional[str] = None,
    service: NewsService = Depends(get_news_service),
):
    """Return the latest persisted financial news."""
    ticker_list = [ticker.strip() for ticker in tickers.split(",")] if tickers else None
    return await service.get_feed(
        limit=limit,
        language=language,
        ticker_filter=ticker_list,
    )


@router.get("/{ticker}", response_model=NewsResponse)
async def get_ticker_news(
    ticker: str,
    limit: int = Query(default=NEWS_DEFAULT_LIMIT, ge=1, le=NEWS_MAX_LIMIT),
    language: Optional[str] = None,
    force_refresh: bool = False,
    service: NewsService = Depends(get_news_service),
):
    """Aggregate financial news for one ticker."""
    return await service.get_news(
        ticker=ticker,
        limit=limit,
        language=language,
        force_refresh=force_refresh,
    )


@router.post("/{ticker}/refresh")
async def refresh_ticker_news(
    ticker: str,
    background_tasks: BackgroundTasks,
    service: NewsService = Depends(get_news_service),
):
    """Queue a cache-bypassing news refresh for one ticker."""
    background_tasks.add_task(
        service.get_news,
        ticker=ticker,
        limit=50,
        force_refresh=True,
    )
    return {"status": "queued", "ticker": ticker}
