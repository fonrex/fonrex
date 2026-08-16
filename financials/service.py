import asyncio
import json
import logging
from typing import List

import redis.asyncio as redis

from concurrency import run_sync
from financials.models import StandardFinancials, StockSummary
from financials.providers.base import BaseProvider
from financials.providers.BourseDirect_provider import BourseDirectProvider
from financials.providers.yfinance_provider import YFinanceProvider

logger = logging.getLogger(__name__)


class FinancialsAggregator:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

        # Providers appelés avec le Ticker
        self.ticker_providers: List[BaseProvider] = [
            YFinanceProvider(),
        ]

        # Providers appelés avec l'ISIN (après récupération via Ticker)
        self.isin_providers: List[BaseProvider] = [
            BourseDirectProvider(),
        ]

        self.cache_ttl = 86400  # 24 heures

    async def get_financials(self, ticker: str) -> StandardFinancials:
        """
        Récupère les données financières agrégées pour un ticker.
        Vérifie d'abord le cache, sinon interroge les providers en parallèle.
        """
        cache_key = f"financials:{ticker}"

        # 1. Vérification du cache
        if self.redis:
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.info(f"Cache hit pour {ticker}")
                return StandardFinancials(**json.loads(cached_data))

        # 2. Appel des providers par Ticker (Step 1)
        logger.info(f"Step 1: Récupération des données Ticker pour {ticker}")
        ticker_tasks = [provider.get_financials(ticker) for provider in self.ticker_providers]
        ticker_results_raw = await asyncio.gather(*ticker_tasks, return_exceptions=True)

        valid_results = []
        found_isin = None

        # Traitement des résultats Ticker
        for res in ticker_results_raw:
            if isinstance(res, Exception):
                logger.error(f"Erreur provider Ticker: {res}")
            elif res:
                valid_results.append(res)
                # On essaie de capturer l'ISIN du premier résultat qui en a un
                if not found_isin and res.isin:
                    found_isin = res.isin

        # 3. Appel des providers par ISIN (Step 2) - Si ISIN trouvé
        if found_isin and self.isin_providers:
            logger.info(f"Step 2: ISIN trouvé ({found_isin}), appel des providers ISIN")
            isin_tasks = [provider.get_financials(found_isin) for provider in self.isin_providers]
            isin_results_raw = await asyncio.gather(*isin_tasks, return_exceptions=True)

            for res in isin_results_raw:
                if isinstance(res, Exception):
                    logger.error(f"Erreur provider ISIN: {res}")
                elif res:
                    valid_results.append(res)
        elif self.isin_providers:
            logger.warning(f"Aucun ISIN trouvé pour {ticker}, saut des providers ISIN")

        if not valid_results:
            logger.warning(f"Aucune donnée trouvée pour {ticker}")
            return StandardFinancials()

        # 4. Fusion des données (Merging)
        final_data = {}
        # On fusionne tous les champs de StandardFinancials
        fields = StandardFinancials.model_fields.keys()

        for field in fields:
            value = None
            # Priorité: on parcourt les résultats valides dans l'ordre (Ticker puis ISIN)
            # ou l'inverse selon ce qu'on veut.
            # Ici valid_results contient [Res_Ticker1, Res_Ticker2, ..., Res_ISIN1, Res_ISIN2...]
            # Si on veut que BourseDirect (ISIN) ait la priorité ou l'inverse, il faut gérer l'ordre dans valid_results.
            # Supposons que l'ordre d'ajout dans valid_results définit la priorité (premier arrivé = prioritaire).
            for result in valid_results:
                val = getattr(result, field, None)
                if val is not None:
                    value = val
                    break
            final_data[field] = value

        aggregated_financials = StandardFinancials(**final_data)

        # 4. Mise en cache
        if self.redis:
            await self.redis.set(
                cache_key, aggregated_financials.model_dump_json(), ex=self.cache_ttl
            )

        return aggregated_financials

    async def get_market_overview(self) -> List[StockSummary]:
        """
        Renvoie une liste simplifiée pour l'aperçu du marché.
        Pour l'instant, basé sur une liste statique de tickers populaires.
        """
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "BRK-B", "JPM", "V"]

        # On pourrait optimiser en faisant un appel batch si le provider le supporte
        # Ici on fait des appels parallèles simples pour récupérer les infos de base

        async def fetch_summary(ticker):
            # On utilise YFinanceProvider juste pour récupérer le prix et le nom rapidement
            # Note: YFinanceProvider.get_financials ne renvoie pas le prix/nom pour l'instant
            # On va faire un appel direct léger ici ou étendre le provider.
            # Pour simplifier, on utilise yfinance directement ici.
            import yfinance as yf

            try:
                stock = await run_sync(yf.Ticker, ticker)
                info = await run_sync(lambda: stock.info)
                return StockSummary(
                    ticker=ticker,
                    name=info.get("shortName"),
                    sector=info.get("sector"),
                    price=info.get("currentPrice"),
                    change_percent=info.get(
                        "regularMarketChangePercent"
                    ),  # ou autre champ de variation
                )
            except Exception as e:
                logger.error(f"Erreur overview pour {ticker}: {e}")
                return None

        tasks = [fetch_summary(t) for t in tickers]
        results = await asyncio.gather(*tasks)

        return [r for r in results if r is not None]
