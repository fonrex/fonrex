"""Use cases backed by specialized financial-data providers."""

import logging

from concurrency import run_sync
from use_cases.errors import (
    DependencyUnavailable,
    InvalidInput,
    ResourceNotFound,
    UpstreamFailure,
)

logger = logging.getLogger(__name__)


class GetInsiderTransactions:
    def __init__(self, provider, cache=None):
        self.provider = provider
        self.cache = cache

    async def execute(self, ticker: str, limit: int = 20, refresh: bool = False):
        if not self.provider:
            raise DependencyUnavailable("Provider SECEdgar non disponible")

        cache_key = (
            self.cache.generate_key(ticker, cache_type="insider_transactions")
            if self.cache
            else None
        )
        if cache_key and self.cache.enabled and not refresh:
            cached = await run_sync(self.cache.get, cache_key)
            if cached:
                return cached

        try:
            result = await self.provider.fetch(ticker=ticker, limit=limit)
        except Exception as exc:
            logger.error("Erreur récupération transactions insiders %s: %s", ticker, exc)
            raise UpstreamFailure(str(exc)) from exc
        if result is None:
            raise DependencyUnavailable("Impossible de récupérer les transactions SEC")

        data = result.model_dump(mode="json")
        if cache_key and self.cache:
            await run_sync(
                self.cache.set,
                cache_key,
                data,
                cache_type="insider_transactions",
            )
        return data


class GetEtfDetails:
    def __init__(self, provider, database=None, cache=None):
        self.provider = provider
        self.database = database
        self.cache = cache

    async def execute(self, isin: str, refresh: bool = False):
        if not self.provider:
            raise DependencyUnavailable("Provider JustETF non disponible")

        if self.database:
            context = await run_sync(self.database.get_asset_context, isin=isin)
            if context:
                quote_type = context["details"].get("quote_type", "")
                if quote_type and quote_type.upper() != "ETF":
                    raise ResourceNotFound(f"{isin} n'est pas un ETF (quote_type={quote_type})")

        cache_key = self.cache.generate_key(isin, cache_type="etf_details") if self.cache else None
        if cache_key and self.cache.enabled and not refresh:
            cached = await run_sync(self.cache.get, cache_key)
            if cached:
                return cached

        try:
            result = await self.provider.fetch(isin=isin)
        except Exception as exc:
            logger.error("Erreur récupération ETF %s: %s", isin, exc)
            raise UpstreamFailure(str(exc)) from exc
        if result is None:
            raise DependencyUnavailable(f"Données ETF introuvables pour {isin}")

        data = result.model_dump(mode="json")
        if cache_key and self.cache:
            await run_sync(self.cache.set, cache_key, data, cache_type="etf_details")
        return data


class GetIndexConstituents:
    VALID_INDICES = frozenset({"SP500", "CAC40", "NASDAQ100", "DAX"})

    def __init__(self, provider, index_name_enum, cache=None):
        self.provider = provider
        self.index_name_enum = index_name_enum
        self.cache = cache

    async def execute(self, index_name: str, refresh: bool = False):
        if not self.provider:
            raise DependencyUnavailable("Provider IndexConstituents non disponible")

        index_upper = index_name.upper()
        if index_upper not in self.VALID_INDICES:
            raise InvalidInput(
                f"Indice non supporté: {index_name}. Valeurs: {sorted(self.VALID_INDICES)}"
            )
        if not self.index_name_enum:
            raise DependencyUnavailable("IndexName enum non disponible")

        cache_key = (
            self.cache.generate_key(index_upper, cache_type="index_constituents")
            if self.cache
            else None
        )
        if cache_key and self.cache.enabled and not refresh:
            cached = await run_sync(self.cache.get, cache_key)
            if cached:
                return cached

        try:
            result = await self.provider.fetch(index_name=self.index_name_enum(index_upper))
        except Exception as exc:
            logger.error("Erreur récupération indice %s: %s", index_upper, exc)
            raise UpstreamFailure(str(exc)) from exc
        if result is None:
            raise DependencyUnavailable(f"Données indice introuvables pour {index_upper}")

        data = result.model_dump(mode="json")
        if cache_key and self.cache:
            await run_sync(
                self.cache.set,
                cache_key,
                data,
                cache_type="index_constituents",
            )
        return data
