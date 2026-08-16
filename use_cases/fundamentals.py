"""Fundamental-data application use cases.

This module depends only on application-owned ports. Concrete SQLAlchemy,
yfinance, provider, and presentation adapters are composed by the HTTP layer.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from concurrency import run_sync
from use_cases.errors import DependencyUnavailable, InvalidInput, ResourceNotFound
from use_cases.ports import (
    AssetProfileEnricherPort,
    AsyncJsonCachePort,
    DeepFundamentalsEnricherPort,
    FundamentalPayload,
    FundamentalsFormatterPort,
    FundamentalsRepositoryPort,
    ProviderRunnerPort,
    SecEdgarProviderPort,
    SyncJsonCachePort,
    TickerNormalizer,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundamentalResult:
    data: FundamentalPayload
    provider_used: str | None = None
    cache_hit: bool = False


class GetFundamentals:
    def __init__(
        self,
        database: FundamentalsRepositoryPort | None = None,
        redis: AsyncJsonCachePort | None = None,
        provider_runner: ProviderRunnerPort | None = None,
        formatter: FundamentalsFormatterPort | None = None,
        profile_enricher: AssetProfileEnricherPort | None = None,
        ticker_normalizer: TickerNormalizer | None = None,
        sec_edgar_provider: SecEdgarProviderPort | None = None,
    ) -> None:
        self.database = database
        self.redis = redis
        self.provider_runner = provider_runner
        self.formatter = formatter
        self.profile_enricher = profile_enricher
        self.ticker_normalizer = ticker_normalizer
        self.sec_edgar_provider = sec_edgar_provider

    async def execute(
        self,
        ticker: str | None = None,
        isin: str | None = None,
        exchange: str | None = None,
        currency: str | None = None,
        provider: str | None = None,
        fmt: str = "eodhd",
        nocache: bool = False,
    ) -> FundamentalResult:
        db_service = self.database
        redis_client = self.redis
        sec_edgar_provider = self.sec_edgar_provider

        provider_params = []
        if provider:
            provider_params = [p.strip() for p in provider.split(",") if p.strip()]

        if not ticker and not isin:
            raise InvalidInput(
                {
                    "error": "Missing parameter",
                    "message": "The ticker or isin parameter is required",
                    "examples": {
                        "ticker": "/fundamental?ticker=AAPL",
                        "isin": "/fundamental?isin=FR0004125920",
                        "listing": "/fundamental?ticker=GOVY&currency=CHF",
                        "provider_specific": "/fundamental?ticker=AAPL&provider=zonebourse",
                        "multiple_providers": "/fundamental?ticker=AAPL&provider=googlefinance,gurufocus",
                    },
                }
            )

        # If ISIN provided, try to find the corresponding ticker
        if isin and not ticker:
            logger.info(f"Searching ticker for ISIN: {isin}")
            if db_service:
                asset_details = await run_sync(
                    db_service.get_asset_details,
                    isin=isin,
                    exchange=exchange,
                    currency=currency,
                )
                if asset_details and asset_details.get("ticker"):
                    ticker = asset_details.get("ticker")
                    logger.info(f"Ticker found in DB for ISIN {isin}: {ticker}")
                else:
                    ticker = isin
            else:
                ticker = isin
            ticker = ticker or isin

        # If ticker provided but no ISIN, try to find the ISIN in the database
        if ticker and not isin:
            if db_service:
                asset_details = await run_sync(
                    db_service.get_asset_details,
                    ticker=ticker,
                    exchange=exchange,
                    currency=currency,
                )
                if asset_details and asset_details.get("isin"):
                    isin = asset_details.get("isin")
                    logger.info(f"ISIN found in DB for {ticker}: {isin}")

        # Automatic conversion of Google Finance tickers to Yahoo Finance format
        original_ticker = ticker
        if ":" in ticker and self.ticker_normalizer:
            ticker = self.ticker_normalizer(ticker)
            logger.info(f"Ticker converted: {original_ticker} -> {ticker}")

        # Retrieve data
        results = {}

        # Build cache key
        cache_key = f"fundamental:{ticker}:{exchange}:{currency}:{fmt}"
        if not nocache and redis_client:
            cached = await redis_client.get(cache_key)
            if cached:
                try:
                    return FundamentalResult(json.loads(cached), cache_hit=True)
                except (json.JSONDecodeError, TypeError, UnicodeError) as exc:
                    logger.warning("Invalid fundamentals cache entry %s: %s", cache_key, exc)

        raw_providers = {}  # Stores source provider links

        # 1. Enrich from local database (Assets) and retrieve mappings
        asset_mappings = {}
        provider_default_tickers = {}
        if db_service:
            asset_context = await run_sync(
                db_service.get_asset_context,
                ticker=ticker,
                isin=isin,
                exchange=exchange,
                currency=currency,
            )

            if asset_context:
                asset_profile = asset_context["details"]

                if self.profile_enricher:
                    try:
                        await self.profile_enricher.enrich(asset_profile, ticker)
                    except TimeoutError:
                        logger.warning(f"yfinance profile enrichment too slow for {ticker}")
                    else:
                        refreshed_context = await run_sync(
                            db_service.get_asset_context,
                            ticker=ticker,
                            isin=isin,
                            exchange=exchange,
                            currency=currency,
                        )
                        if refreshed_context:
                            asset_context = refreshed_context
                            asset_profile = asset_context["details"]
                            if not isin and asset_profile.get("isin"):
                                isin = asset_profile.get("isin")

                results["asset_profile"] = asset_profile
                asset_mappings = asset_context["mappings"]
                if (
                    isin
                    and ticker == isin
                    and asset_profile.get("ticker")
                    and asset_profile.get("ticker") != isin
                ):
                    provider_default_tickers["msn"] = asset_profile["ticker"]

        if self.provider_runner is None:
            raise DependencyUnavailable("Financial provider runner unavailable")
        provider_results, raw_providers = await self.provider_runner.run(
            ticker=ticker,
            isin=isin,
            provider_params=provider_params,
            asset_mappings=asset_mappings,
            provider_default_tickers=provider_default_tickers,
            asset_profile=results.get("asset_profile"),
        )

        # Merge provider results
        successful_providers = [
            name
            for name, payload in provider_results.items()
            if payload and not (isinstance(payload, dict) and payload.get("error"))
        ]
        provider_used = ",".join(successful_providers) if successful_providers else None
        results.update(provider_results)

        # 2. Additional enrichments for premium rendering
        # 2.1 Insider Transactions (SEC Edgar) — US tickers only
        if sec_edgar_provider and ticker:
            # Heuristic US detection (ticker without dot, or ending in .US / .NAS)
            is_us = (
                "." not in ticker
                or ticker.upper().endswith(".US")
                or ticker.upper().endswith(".NAS")
            )
            if is_us:
                # Limit to 10 transactions for speed
                insider_result = await sec_edgar_provider.fetch(ticker=ticker, limit=10)
                if insider_result:
                    results["SECEdgar"] = insider_result

        # 2.2 Deep data (Statements, Earnings, Ratings) from the database
        if results.get("asset_profile") and results["asset_profile"].get("asset_id"):
            asset_id = results["asset_profile"]["asset_id"]
            deep_data = await run_sync(db_service.get_deep_fundamentals, asset_id)
            if deep_data:
                # Inject missing sections into results for the formatter
                results["Financials"] = deep_data.get("statements", {})
                results["AnalystRatings"] = deep_data.get("analyst_ratings", {})
                results["EarningsHistory"] = deep_data.get("earnings_history", [])

        # 3. Final rendering & cache
        results["raw_providers"] = raw_providers

        # Final formatting according to the fmt parameter
        if fmt == "eodhd":
            if self.formatter is None:
                raise DependencyUnavailable("Fundamentals formatter unavailable")
            final_response = self.formatter.to_eodhd(results)
        else:
            # Format brut (par défaut ou si spécifié)
            final_response = results

        # Mise en cache Redis si activé (TTL 1h)
        if redis_client and not nocache:
            await redis_client.setex(cache_key, 3600, json.dumps(final_response, default=str))

        return FundamentalResult(final_response, provider_used=provider_used)


class GetDeepFundamentals:
    def __init__(
        self,
        database: FundamentalsRepositoryPort,
        cache: SyncJsonCachePort | None = None,
        enricher: DeepFundamentalsEnricherPort | None = None,
    ) -> None:
        self.database = database
        self.cache = cache
        self.enricher = enricher

    async def execute(
        self,
        ticker: str | None = None,
        isin: str | None = None,
        refresh: bool = False,
        sections: str = "all",
    ) -> FundamentalPayload:
        db_service = self.database
        cache_service = self.cache
        if not ticker and not isin:
            raise InvalidInput(
                {
                    "error": "Paramètre manquant",
                    "message": "Le paramètre ticker ou isin est requis",
                    "examples": {
                        "ticker": "/fundamental/deep?ticker=AIR.PA",
                        "isin": "/fundamental/deep?isin=NL0000235190",
                    },
                }
            )

        requested_sections = set(s.strip().lower() for s in sections.split(",") if s.strip())
        want_all = "all" in requested_sections or not requested_sections

        # 1. Résoudre l'actif
        asset_profile = None
        asset_id = None
        resolved_ticker = ticker

        if db_service:
            asset_context = await run_sync(db_service.get_asset_context, ticker=ticker, isin=isin)
            if asset_context:
                asset_profile = asset_context["details"]
                asset_id = asset_profile.get("asset_id")
                resolved_ticker = asset_profile.get("ticker") or ticker

        if not asset_id:
            raise ResourceNotFound(f"Actif introuvable pour ticker={ticker}, isin={isin}")

        # 2. Vérifier le cache Redis
        cache_hit = False
        cache_key_prefix = f"deep:{resolved_ticker}"

        if cache_service and cache_service.enabled and not refresh:
            cached = await run_sync(cache_service.get, cache_key_prefix)
            if cached:
                cache_hit = True
                cached["meta"] = {
                    "fetched_at": cached.get("meta", {}).get("fetched_at"),
                    "source": "yfinance",
                    "cache_hit": True,
                }
                return cached

        # 3. Si cache miss ou refresh → appeler YFinanceEnricher
        if not cache_hit and self.enricher:
            enrich_result = await self.enricher.enrich(asset_id, resolved_ticker)
            logger.info("Enrichissement deep pour %s: %s", resolved_ticker, enrich_result)

        # 4. Lire les données depuis PostgreSQL
        response = {}

        if asset_profile:
            response["asset_profile"] = {
                "isin": asset_profile.get("isin"),
                "ticker": asset_profile.get("ticker"),
                "name": asset_profile.get("name"),
                "exchange": asset_profile.get("exchange"),
                "currency": asset_profile.get("currency"),
            }

        response.update(
            await run_sync(
                db_service.get_deep_sections,
                asset_id,
                requested_sections,
                want_all,
            )
        )

        # 5. Métadonnées
        response["meta"] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "yfinance",
            "cache_hit": False,
        }

        # 6. Mettre en cache Redis
        if cache_service and cache_service.enabled:
            await run_sync(
                cache_service.set,
                cache_key_prefix,
                response,
                cache_type="highlights",
            )

        return response
