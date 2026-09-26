import logging
import random
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


class BarronsProvider(BaseProvider):
    """
    Barrons Provider to extract Rating and Sentiment.
    """

    BASE_URL = "https://www.barrons.com/market-data/stocks/{ticker}"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, http2=True) as client:
            try:
                # Basic normalization
                url_ticker = ticker.split(":")[-1].lower()
                url = self.BASE_URL.format(ticker=url_ticker)

                html = await self._fetch_page(client, url)
                if not html:
                    return None

                metrics = FinancialMetrics(ticker=ticker, provider_url=url)
                
                # Extract rating and sentiment
                rating, sentiment = self._parse_rating_sentiment(html)
                
                # As FinancialMetrics may not have a dedicated field for "Sentiment",
                # we can put it in analyst rating or add it. 
                # Let's map rating to morningstar_rating or a custom field if we added one.
                # Actually, the user asked to "récupérer la valeur Rating et la note Sentiment pour l'afficher dans fundamental".
                # I will add `rating` and `sentiment` to FinancialMetrics in models.py
                metrics.rating = rating
                metrics.sentiment = sentiment

                return metrics
            except Exception as e:
                logger.error(f"Barrons error {ticker}: {e}")
                return None

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> Optional[HTMLParser]:
        for _ in range(self.max_retries):
            try:
                headers = {
                    "User-Agent": random.choice(USER_AGENTS),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1"
                }
                resp = await client.get(url, headers=headers)
                if resp.status_code in (200, 301, 302):
                    return HTMLParser(resp.text)
            except Exception:
                pass
        return None

    def _parse_rating_sentiment(self, parser: HTMLParser):
        rating = None
        sentiment = None
        
        # Heuristic search for Rating and Sentiment
        # Usually they are labeled "Rating" or "Analyst Rating" and "Sentiment" or "News Sentiment"
        for node in parser.css("div, span, li"):
            text = node.text(strip=True).lower()
            if not text:
                continue
                
            if "rating" in text and len(text) < 30 and not rating:
                # Look for the value in siblings or next elements
                val_node = node.next
                if val_node and val_node.text(strip=True):
                    rating = val_node.text(strip=True)
                else:
                    # Look in sibling css
                    val_node = node.css_first(".value, .data, span")
                    if val_node and val_node != node:
                        rating = val_node.text(strip=True)

            if "sentiment" in text and len(text) < 30 and not sentiment:
                val_node = node.next
                if val_node and val_node.text(strip=True):
                    sentiment = val_node.text(strip=True)
                else:
                    val_node = node.css_first(".value, .data, span")
                    if val_node and val_node != node:
                        sentiment = val_node.text(strip=True)
                        
        # Alternative fallback: look for specific known classes if heuristics fail
        if not rating:
            rating_node = parser.css_first(".rating-value, [class*='RatingValue'], .analyst-rating")
            if rating_node:
                rating = rating_node.text(strip=True)
                
        if not sentiment:
            sentiment_node = parser.css_first(".sentiment-value, [class*='SentimentValue']")
            if sentiment_node:
                sentiment = sentiment_node.text(strip=True)

        return rating, sentiment
