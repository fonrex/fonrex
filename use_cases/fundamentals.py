"""Fundamental-data application use cases.

This module depends only on application-owned ports. Concrete SQLAlchemy,
yfinance, provider, and presentation adapters are composed by the HTTP layer.
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from concurrency import run_sync
from use_cases.errors import DependencyUnavailable, InvalidInput, ResourceNotFound
from use_cases.ports import (
    AssetProfile,
    AssetProfileEnricherPort,
    AsyncJsonCachePort,
    DeepFundamentalsEnricherPort,
    FundamentalPayload,
    FundamentalsFormatterPort,
    FundamentalsRepositoryPort,
    ProviderRunnerPort,
    SecEdgarProviderPort,
    SourceSymbolResolverPort,
    SyncJsonCachePort,
    TickerNormalizer,
)

logger = logging.getLogger(__name__)

YAHOO = "yahoofinance"
# Insider transactions are an extra: they never hold the answer longer than this.
INSIDER_TIMEOUT_SECONDS = 15.0
# Finding the Yahoo symbol of a listing the first time: a search and a few quotes.
SYMBOL_LOOKUP_TIMEOUT_SECONDS = 15.0
# Instruments that have no insiders filing with the SEC.
NO_INSIDER_QUOTE_TYPES = {"ETF", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE"}

# Sections stored in the database, under the names the formatter reads them.
DEEP_SECTIONS = {
    "highlights": "highlights",
    "statements": "financial_statements",
    "analyst_ratings": "analyst_ratings",
    "earnings_history": "earnings_history",
    "earnings_trend": "earnings_trend",
    "esg_scores": "esg_scores",
    "etf_details": "etf_details",
    "etf_holdings": "etf_holdings",
}

# ``sections`` of ``/fundamental/deep`` and the key each one fills in the answer.
DEEP_SECTION_KEYS = {
    "highlights": "highlights",
    "statements": "statements",
    "earnings": "earnings_history",
    "ratings": "analyst_ratings",
}


def _text(record: Mapping[str, object] | None, key: str) -> str | None:
    """A text field of a record read from the database, or None when it is blank."""
    value = (record or {}).get(key)
    return value if isinstance(value, str) and value else None


def _identifier(record: Mapping[str, object] | None, key: str) -> int | None:
    """An identifier of a record read from the database, or None when there is none."""
    value = (record or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value else None


def _requested_sections(
    answer: FundamentalPayload, requested: set[str], want_all: bool
) -> FundamentalPayload:
    """Keep, from a complete deep answer, the sections a request asked for."""
    if want_all:
        return answer
    left_out = {key for section, key in DEEP_SECTION_KEYS.items() if section not in requested}
    return {key: value for key, value in answer.items() if key not in left_out}


async def yahoo_symbol_of(
    symbols: SourceSymbolResolverPort | None,
    asset_profile: Mapping[str, object] | None,
    ticker: str | None,
    refresh: bool = False,
) -> tuple[str | None, str | None]:
    """Symbol to ask Yahoo with, or the reason Yahoo is not asked.

    A ticker of the catalogue is not a Yahoo symbol (``SPFF`` alone is a US fund,
    ``EUCO`` is ``SYBC.DE``): for a listing of the catalogue only the symbol
    verified for it is used, and Yahoo is not asked without one. A ticker that
    is not in the catalogue is used as the caller typed it.
    """
    listing_id = _identifier(asset_profile, "listing_id")
    if symbols is None or not listing_id:
        return ticker, None
    try:
        resolution = await asyncio.wait_for(
            symbols.resolve(listing_id, refresh=refresh), timeout=SYMBOL_LOOKUP_TIMEOUT_SECONDS
        )
    except TimeoutError:
        # The lookup is one source among others: too slow, it is a source that
        # did not answer, not the failure of the whole request.
        return None, f"Yahoo symbol lookup too slow for {ticker}"
    if resolution.symbol:
        return resolution.symbol, None
    return None, resolution.reason or f"No verified Yahoo symbol for {ticker}"


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
        symbols: SourceSymbolResolverPort | None = None,
    ) -> None:
        self.database = database
        self.redis = redis
        self.provider_runner = provider_runner
        self.formatter = formatter
        self.profile_enricher = profile_enricher
        self.ticker_normalizer = ticker_normalizer
        self.sec_edgar_provider = sec_edgar_provider
        self.symbols = symbols

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

        provider_params = []
        if provider:
            provider_params = [p.strip() for p in provider.split(",") if p.strip()]

        if not ticker:
            if not isin:
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
            # Only the ISIN is given: find the corresponding ticker. The ISIN
            # stands for it when none is found.
            logger.info(f"Searching ticker for ISIN: {isin}")
            ticker = isin
            if db_service:
                asset_details = await run_sync(
                    db_service.get_asset_details,
                    isin=isin,
                    exchange=exchange,
                    currency=currency,
                )
                found_ticker = _text(asset_details, "ticker")
                if found_ticker:
                    ticker = found_ticker
                    logger.info(f"Ticker found in DB for ISIN {isin}: {ticker}")

        # If ticker provided but no ISIN, try to find the ISIN in the database
        if ticker and not isin:
            if db_service:
                asset_details = await run_sync(
                    db_service.get_asset_details,
                    ticker=ticker,
                    exchange=exchange,
                    currency=currency,
                )
                found_isin = _text(asset_details, "isin")
                if found_isin:
                    isin = found_isin
                    logger.info(f"ISIN found in DB for {ticker}: {isin}")

        # Automatic conversion of Google Finance tickers to Yahoo Finance format
        original_ticker = ticker
        if ":" in ticker and self.ticker_normalizer:
            ticker = self.ticker_normalizer(ticker)
            logger.info(f"Ticker converted: {original_ticker} -> {ticker}")

        # Retrieve data
        results: FundamentalPayload = {}

        # Build cache key. The providers asked are part of it: without them, the
        # answer of one provider was served to a request for all of them.
        providers_key = ",".join(sorted(name.lower() for name in provider_params)) or "all"
        cache_key = f"fundamental:{ticker}:{exchange}:{currency}:{fmt}:{providers_key}"
        if not nocache and redis_client:
            cached = await redis_client.get(cache_key)
            if cached:
                try:
                    return FundamentalResult(json.loads(cached), cache_hit=True)
                except (json.JSONDecodeError, TypeError, UnicodeError) as exc:
                    logger.warning("Invalid fundamentals cache entry %s: %s", cache_key, exc)

        raw_providers: FundamentalPayload = {}  # Stores source provider links

        # 1. Enrich from local database (Assets) and retrieve mappings
        asset_profile: AssetProfile | None = None
        asset_mappings: Mapping[str, object] = {}
        provider_default_tickers: dict[str, str] = {}
        verified_symbols: dict[str, str] = {}
        refused_providers: dict[str, str] = {}
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

                yahoo_symbol, yahoo_refusal = await yahoo_symbol_of(
                    self.symbols, asset_profile, ticker
                )
                if asset_profile.get("listing_id") and self.symbols is not None:
                    if yahoo_symbol:
                        verified_symbols[YAHOO] = yahoo_symbol
                    elif yahoo_refusal:
                        # Always the case: without a symbol, the lookup gives its reason.
                        refused_providers[YAHOO] = yahoo_refusal

                if self.profile_enricher and not yahoo_refusal:
                    try:
                        await self.profile_enricher.enrich(
                            asset_profile, ticker, symbol=verified_symbols.get(YAHOO)
                        )
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
                            profile_isin = _text(asset_profile, "isin")
                            if not isin and profile_isin:
                                isin = profile_isin

                results["asset_profile"] = asset_profile
                asset_mappings = asset_context["mappings"]
                profile_ticker = _text(asset_profile, "ticker")
                if isin and ticker == isin and profile_ticker and profile_ticker != isin:
                    provider_default_tickers["msn"] = profile_ticker

        if self.provider_runner is None:
            raise DependencyUnavailable("Financial provider runner unavailable")

        # 2. Providers and insider transactions, at the same time
        (provider_results, raw_providers), insider_result = await asyncio.gather(
            self.provider_runner.run(
                ticker=ticker,
                isin=isin,
                provider_params=provider_params,
                asset_mappings=asset_mappings,
                provider_default_tickers=provider_default_tickers,
                asset_profile=asset_profile,
                verified_symbols=verified_symbols,
                refused_providers=refused_providers,
            ),
            self._insider_transactions(ticker, isin, asset_profile),
        )

        # Merge provider results
        successful_providers = [
            name
            for name, payload in provider_results.items()
            if payload and not (isinstance(payload, dict) and payload.get("error"))
        ]
        provider_used = ",".join(successful_providers) if successful_providers else None
        results.update(provider_results)
        if insider_result:
            results["SECEdgar"] = insider_result

        # 3. Deep data (statements, earnings, ratings, ...) stored in the database
        deep_sections: FundamentalPayload = {}
        profile_asset_id = _identifier(asset_profile, "asset_id")
        if db_service and profile_asset_id:
            deep_data = await run_sync(db_service.get_deep_fundamentals, profile_asset_id)
            if deep_data:
                results["Financials"] = deep_data.get("statements", {})
                results["AnalystRatings"] = deep_data.get("analyst_ratings", {})
                results["EarningsHistory"] = deep_data.get("earnings_history", [])
                deep_sections = {
                    name: deep_data[section]
                    for section, name in DEEP_SECTIONS.items()
                    if deep_data.get(section)
                }

        # 4. Final rendering & cache
        results["raw_providers"] = raw_providers

        # Final formatting according to the fmt parameter
        if fmt == "eodhd":
            if self.formatter is None:
                raise DependencyUnavailable("Fundamentals formatter unavailable")
            # The formatter reads the stored sections under their own names: handed
            # over under other names, every stored figure was left out of the answer.
            final_response: FundamentalPayload = self.formatter.to_eodhd(
                {**results, **deep_sections}
            )
        else:
            # Raw format (default, or when asked)
            final_response = results

        # Redis cache when enabled (TTL 1h)
        if redis_client and not nocache:
            await redis_client.setex(cache_key, 3600, json.dumps(final_response, default=str))

        return FundamentalResult(final_response, provider_used=provider_used)

    async def _insider_transactions(
        self, ticker: str, isin: str | None, asset_profile: Mapping[str, object] | None = None
    ) -> object | None:
        """Insider transactions filed with the SEC, for a share listed in the US."""
        if not self.sec_edgar_provider or not ticker:
            return None
        if not self._files_with_the_sec(ticker, isin, asset_profile or {}):
            return None
        try:
            # Limit to 10 transactions for speed
            result = await asyncio.wait_for(
                self.sec_edgar_provider.fetch(ticker=ticker, limit=10),
                timeout=INSIDER_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("SEC insider transactions too slow for %s", ticker)
            return None
        # Plain data: the answer is also stored as JSON in the cache.
        if hasattr(result, "model_dump"):
            plain: object = result.model_dump(mode="json")
            return plain
        return result

    @staticmethod
    def _files_with_the_sec(
        ticker: str, isin: str | None, asset_profile: Mapping[str, object]
    ) -> bool:
        """Whether the ticker can be looked up at the SEC without naming another company.

        A ticker without suffix is not enough: ``SPFF`` is a fund quoted in EUR in
        the catalogue and another fund in the US. A listing quoted in USD is asked,
        whatever its ISIN: Accenture (an Irish ISIN) files with the SEC.
        """
        quote_type = str(asset_profile.get("quote_type") or "").upper()
        if quote_type in NO_INSIDER_QUOTE_TYPES:
            return False
        if isin and isin.upper().startswith("US"):
            return True
        currency = str(asset_profile.get("currency") or "").upper()
        if currency:
            return currency == "USD"
        if isin and len(isin) == 12:
            return False
        return "." not in ticker or ticker.upper().endswith((".US", ".NAS"))


class GetDeepFundamentals:
    def __init__(
        self,
        database: FundamentalsRepositoryPort,
        cache: SyncJsonCachePort | None = None,
        enricher: DeepFundamentalsEnricherPort | None = None,
        symbols: SourceSymbolResolverPort | None = None,
    ) -> None:
        self.database = database
        self.cache = cache
        self.enricher = enricher
        self.symbols = symbols

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
                    "error": "Missing parameter",
                    "message": "The ticker or isin parameter is required",
                    "examples": {
                        "ticker": "/fundamental/deep?ticker=AIR.PA",
                        "isin": "/fundamental/deep?isin=NL0000235190",
                    },
                }
            )

        requested_sections = set(s.strip().lower() for s in sections.split(",") if s.strip())
        want_all = "all" in requested_sections or not requested_sections

        # 1. Resolve the asset
        asset_profile: AssetProfile | None = None
        asset_id: int | None = None
        resolved_ticker = ticker

        if db_service:
            asset_context = await run_sync(db_service.get_asset_context, ticker=ticker, isin=isin)
            if asset_context:
                asset_profile = asset_context["details"]
                asset_id = _identifier(asset_profile, "asset_id")
                resolved_ticker = _text(asset_profile, "ticker") or ticker

        if not asset_id:
            raise ResourceNotFound(f"Asset not found for ticker={ticker}, isin={isin}")

        # 2. Redis cache. The entry holds every section: the request
        # receives the ones it asked for. Cached as asked, an answer limited to
        # some sections was served to the requests for the others.
        cache_hit = False
        # Keyed by instrument: a ticker is not an identity (AIR is AAR Corp in New
        # York and Airbus in Paris), and both answers were stored under deep:AIR.
        cache_key_prefix = f"deep:{asset_id}"

        if cache_service and cache_service.enabled and not refresh:
            cached = await run_sync(cache_service.get, cache_key_prefix)
            if cached:
                cache_hit = True
                cached_meta = cached.get("meta", {})
                cached["meta"] = {
                    **(cached_meta if isinstance(cached_meta, Mapping) else {}),
                    "source": "yfinance",
                    "cache_hit": True,
                }
                return _requested_sections(cached, requested_sections, want_all)

        # 3. Cache miss or refresh → run YFinanceEnricher, with the Yahoo
        # symbol verified for the listing: the ticker of the catalogue may be the
        # symbol of another instrument, whose figures would be stored under this one.
        yahoo_symbol, yahoo_refusal = None, None
        if not cache_hit and self.enricher:
            yahoo_symbol, yahoo_refusal = await yahoo_symbol_of(
                self.symbols, asset_profile, resolved_ticker, refresh=refresh
            )
            if yahoo_symbol:
                # The listing the symbol was verified for, which receives Yahoo's
                # exchange and currency when blank. Without a resolver the ticker
                # is used as typed: it is verified for no listing.
                verified_listing_id = (
                    _identifier(asset_profile, "listing_id") if self.symbols is not None else None
                )
                enrich_result = await self.enricher.enrich(
                    asset_id, yahoo_symbol, listing_id=verified_listing_id
                )
                logger.info("Deep enrichment of %s: %s", yahoo_symbol, enrich_result)
            else:
                logger.warning("Deep fundamentals not refreshed: %s", yahoo_refusal)

        # 4. Read the data from PostgreSQL
        response: FundamentalPayload = {}

        if asset_profile:
            response["asset_profile"] = {
                "isin": asset_profile.get("isin"),
                "ticker": asset_profile.get("ticker"),
                "name": asset_profile.get("name"),
                "exchange": asset_profile.get("exchange"),
                "currency": asset_profile.get("currency"),
            }

        response.update(await run_sync(db_service.get_deep_sections, asset_id, set(), True))

        # 5. Metadata
        meta: dict[str, object] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "yfinance",
            "cache_hit": False,
        }
        response["meta"] = meta
        if yahoo_symbol:
            meta["symbol"] = yahoo_symbol
        if yahoo_refusal:
            # What follows is what the database already held, not a fresh answer:
            # it is said, and it is not kept in the cache as if it were one.
            meta["source"] = "database"
            meta["note"] = yahoo_refusal

        # 6. Cache it in Redis
        if cache_service and cache_service.enabled and not yahoo_refusal:
            await run_sync(
                cache_service.set,
                cache_key_prefix,
                response,
                cache_type="highlights",
            )

        return _requested_sections(response, requested_sections, want_all)
