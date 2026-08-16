"""
TechnicalIndicatorService — Calculates technical indicators from
prices_eod (EOD) or prices_intraday (intraday) via pandas-ta.

Design:
- Loads OHLCV data from TimescaleDB into a pandas DataFrame
- Applies pandas-ta to calculate the requested indicators
- Caches results in Redis (TTL configurable per indicator)
- Returns typed time series via Pydantic

Performance:
- prices_eod   : stable data → Redis TTL 3600s (1h)
- prices_intraday : live data → Redis TTL 60s
- Lazy calculation: only calculate requested indicators
- Batch: calculate multiple indicators in a single DataFrame pass
"""

import asyncio
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
from pydantic import ValidationError

from concurrency import run_sync
from schemas.technical import (
    IndicatorResult,
    MultiIndicatorResult,
    TechnicalSeries,
)
from technical.calculation_engine import TechnicalCalculationEngine
from technical.catalog import CACHE_TTL, INDICATOR_DEFAULTS, INDICATOR_REGISTRY
from technical.contracts import (
    CachePayload,
    IndicatorParams,
    TechnicalCachePort,
    TechnicalMarketDataPort,
)
from technical.errors import (
    IndicatorCalculationFailed,
    InsufficientHistoricalData,
    InvalidIndicator,
    TechnicalDataNotFound,
    UnsupportedIndicatorResolution,
)

logger = logging.getLogger(__name__)


class TechnicalIndicatorService:
    def __init__(
        self,
        market_data: TechnicalMarketDataPort | None,
        cache: TechnicalCachePort | None = None,
    ) -> None:
        self._engine = TechnicalCalculationEngine()
        self._market_data = market_data
        self._cache = cache

    # ── Main entry point — single indicator ─────────────────────────

    async def calculate(
        self,
        ticker: str,
        indicator: str,
        resolution: str = "1D",
        period: int | None = None,  # Override of the default period
        params: IndicatorParams | None = None,  # Override of all parameters
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
        asset_id: int | None = None,
    ) -> IndicatorResult:
        # 1. Normalize indicator name
        indicator_clean = indicator.strip().lower()
        if indicator_clean not in INDICATOR_REGISTRY:
            raise InvalidIndicator(f"Unknown indicator: {indicator_clean}")

        # 2. Build parameters
        params_used = INDICATOR_DEFAULTS[indicator_clean].copy()
        if params:
            params_used.update(params)
        elif period is not None:
            param_list = INDICATOR_REGISTRY[indicator_clean]["params"]
            if param_list:
                first_param = param_list[0]
                params_used[first_param] = period

        # VWAP is only available intraday
        if indicator_clean == "vwap" and resolution in ["1D", "1W", "1M"]:
            raise UnsupportedIndicatorResolution("VWAP only available on intraday data")

        # 3. Check Cache
        cache_key = self._cache_key(
            ticker, indicator_clean, params_used, resolution, from_date, to_date
        )
        cache_enabled = os.environ.get("TECHNICAL_CACHE_ENABLED", "true").lower() == "true"
        if cache_enabled:
            cached_data = await self._get_cache(cache_key)
            if cached_data:
                try:
                    cached_res = IndicatorResult.model_validate(cached_data)
                    cached_res.cached = True
                    return cached_res
                except ValidationError as e:
                    logger.warning(f"Failed to parse cached technical indicator: {e}")

        # 4. Resolve asset_id if not provided
        if not asset_id:
            asset_id = await self._resolve_asset_id(ticker)
            if not asset_id:
                raise TechnicalDataNotFound(f"Ticker not found: {ticker}")

        # 5. Load DataFrame
        df = await self._load_ohlcv_dataframe(
            asset_id=asset_id,
            resolution=resolution,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
        if df.empty:
            raise TechnicalDataNotFound(f"No historical data found for {ticker}")

        # 6. Validate minimum periods
        try:
            self._validate_min_periods(df, indicator_clean, params_used)
        except ValueError as e:
            raise InsufficientHistoricalData(str(e)) from e

        # 7. Calculate indicator
        calc_df = await run_sync(self._calculate_indicator_on_df, df, indicator_clean, params_used)

        # 8. Resolve column names and map to series
        indicator_info = INDICATOR_REGISTRY[indicator_clean]
        col_names = []
        for col_template in indicator_info["cols"]:
            expected_name = col_template.format(**params_used)
            actual_name = None
            if expected_name in calc_df.columns:
                actual_name = expected_name
            else:
                for col in calc_df.columns:
                    if col.lower() == expected_name.lower():
                        actual_name = col
                        break
                if not actual_name:
                    prefix = expected_name.split("_")[0]
                    for col in calc_df.columns:
                        if (
                            col.startswith(prefix)
                            and col not in col_names
                            and col not in df.columns
                        ):
                            actual_name = col
                            break
            if actual_name:
                col_names.append(actual_name)

        series = await run_sync(
            self._df_to_series,
            calc_df,
            col_names,
            indicator_clean,
            params_used,
        )

        # 9. Format response
        from schemas.technical import IndicatorCategory

        result = IndicatorResult(
            ticker=ticker,
            indicator=indicator_clean,
            params=params_used,
            resolution=resolution,
            category=IndicatorCategory(indicator_info["category"]),
            from_date=pd.to_datetime(calc_df.index[0]).to_pydatetime()
            if not calc_df.empty
            else None,
            to_date=pd.to_datetime(calc_df.index[-1]).to_pydatetime()
            if not calc_df.empty
            else None,
            count=len(calc_df),
            series=series,
            cached=False,
            calculated_at=datetime.now(timezone.utc),
        )

        # 10. Cache Redis
        if cache_enabled:
            ttl = CACHE_TTL.get(resolution, 3600)
            asyncio.create_task(self._set_cache(cache_key, result.model_dump(), ttl))

        return result

    # ── Multi-indicators in a single call ───────────────────────────────────

    async def calculate_multi(
        self,
        ticker: str,
        indicators: list[str],  # e.g.: ["sma_20", "ema_50", "rsi_14", "macd"]
        resolution: str = "1D",
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
        include_ohlcv: bool = True,
        asset_id: int | None = None,
    ) -> MultiIndicatorResult:
        # 1. Resolve asset_id if not provided
        if not asset_id:
            asset_id = await self._resolve_asset_id(ticker)
            if not asset_id:
                raise TechnicalDataNotFound(f"Ticker not found: {ticker}")

        # 2. Parse indicators
        parsed_indicators = []
        for ind_str in indicators:
            ind_name, params = self._parse_indicator_name(ind_str)
            parsed_indicators.append((ind_str, ind_name, params))

        # 3. Load DataFrame once (critical optimization)
        df = await self._load_ohlcv_dataframe(
            asset_id=asset_id,
            resolution=resolution,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )

        indicator_results = {}
        errors = {}

        if df.empty:
            return MultiIndicatorResult(
                ticker=ticker,
                resolution=resolution,
                from_date=from_date,
                to_date=to_date,
                count=0,
                ohlcv=[] if include_ohlcv else None,
                indicators={},
                errors={"global": "No historical data found"},
                calculated_at=datetime.now(timezone.utc),
            )

        # 4. Calculate indicators
        for ind_str, ind_name, params in parsed_indicators:
            # check VWAP intraday only
            if ind_name == "vwap" and resolution in ["1D", "1W", "1M"]:
                errors[ind_str] = "VWAP only available on intraday data"
                continue
            try:
                # Validate periods
                self._validate_min_periods(df, ind_name, params)

                # Apply calculation
                calc_df = await run_sync(
                    self._calculate_indicator_on_df, df.copy(), ind_name, params
                )

                # Retrieve expected column names
                indicator_info = INDICATOR_REGISTRY[ind_name]
                col_names = []
                for col_template in indicator_info["cols"]:
                    expected_name = col_template.format(**params)
                    actual_name = None
                    if expected_name in calc_df.columns:
                        actual_name = expected_name
                    else:
                        for col in calc_df.columns:
                            if col.lower() == expected_name.lower():
                                actual_name = col
                                break
                        if not actual_name:
                            prefix = expected_name.split("_")[0]
                            for col in calc_df.columns:
                                if (
                                    col.startswith(prefix)
                                    and col not in col_names
                                    and col not in df.columns
                                ):
                                    actual_name = col
                                    break
                    if actual_name:
                        col_names.append(actual_name)

                # Format series
                series = await run_sync(self._df_to_series, calc_df, col_names, ind_name, params)

                from schemas.technical import IndicatorCategory

                ind_res = IndicatorResult(
                    ticker=ticker,
                    indicator=ind_name,
                    params=params,
                    resolution=resolution,
                    category=IndicatorCategory(indicator_info["category"]),
                    from_date=pd.to_datetime(calc_df.index[0]).to_pydatetime()
                    if not calc_df.empty
                    else None,
                    to_date=pd.to_datetime(calc_df.index[-1]).to_pydatetime()
                    if not calc_df.empty
                    else None,
                    count=len(calc_df),
                    series=series,
                    cached=False,
                    calculated_at=datetime.now(timezone.utc),
                )

                indicator_results[ind_str] = ind_res

                # Cache separate indicator
                cache_enabled = os.environ.get("TECHNICAL_CACHE_ENABLED", "true").lower() == "true"
                if cache_enabled:
                    cache_key = self._cache_key(
                        ticker, ind_name, params, resolution, from_date, to_date
                    )
                    ttl = CACHE_TTL.get(resolution, 3600)
                    asyncio.create_task(self._set_cache(cache_key, ind_res.model_dump(), ttl))

            except (IndicatorCalculationFailed, KeyError, ValueError) as e:
                logger.error(f"Error calculating {ind_str} for {ticker}: {e}")
                errors[ind_str] = str(e)

        # Assemble OHLCV bars if requested
        ohlcv_bars = None
        if include_ohlcv:
            from schemas.technical import OHLCVBar

            ohlcv_bars = []
            for t, row in df.iterrows():
                t_dt = t.to_pydatetime() if hasattr(t, "to_pydatetime") else t
                ohlcv_bars.append(
                    OHLCVBar(
                        t=t_dt,
                        o=Decimal(str(round(row["open"], 4))) if pd.notna(row["open"]) else None,
                        h=Decimal(str(round(row["high"], 4))) if pd.notna(row["high"]) else None,
                        l=Decimal(str(round(row["low"], 4))) if pd.notna(row["low"]) else None,
                        c=Decimal(str(round(row["close"], 4))) if pd.notna(row["close"]) else None,
                        v=int(row["volume"]) if pd.notna(row["volume"]) else None,
                    )
                )

        return MultiIndicatorResult(
            ticker=ticker,
            resolution=resolution,
            from_date=pd.to_datetime(df.index[0]).to_pydatetime() if not df.empty else None,
            to_date=pd.to_datetime(df.index[-1]).to_pydatetime() if not df.empty else None,
            count=len(df),
            ohlcv=ohlcv_bars,
            indicators=indicator_results,
            errors=errors,
            calculated_at=datetime.now(timezone.utc),
        )

    # ── pandas-ta Calculation ─────────────────────────────────────────────────────

    def _calculate_indicator_on_df(
        self,
        df: pd.DataFrame,
        indicator: str,
        params: IndicatorParams,
    ) -> pd.DataFrame:
        return self._engine.calculate(df, indicator, params)

    # ── Loading OHLCV data ─────────────────────────────────────────

    async def _load_ohlcv_dataframe(
        self,
        asset_id: int,
        resolution: str,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        if self._market_data is None:
            return pd.DataFrame()
        return await self._market_data.load_ohlcv(
            asset_id,
            resolution,
            from_date,
            to_date,
            limit,
        )

    # ── Redis Cache ──────────────────────────────────────────────────────────

    def _cache_key(
        self,
        ticker: str,
        indicator: str,
        params: IndicatorParams,
        resolution: str,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> str:
        params_value = "default"
        if params:
            params_value = "_".join(str(value) for _, value in sorted(params.items()))
        date_value = f"_{from_date}" if from_date else ""
        if to_date:
            date_value += f"_{to_date}"
        return f"technical:{ticker.upper()}:{resolution}:{indicator}:{params_value}{date_value}"

    async def _get_cache(self, key: str) -> CachePayload | None:
        if self._cache is None:
            return None
        return await self._cache.get(key)

    async def _set_cache(self, key: str, data: CachePayload, ttl: int) -> None:
        if self._cache is None:
            return
        await self._cache.set(key, data, ttl)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _parse_indicator_name(self, name: str) -> tuple[str, IndicatorParams]:
        return self._engine.parse_name(name)

    def _validate_min_periods(
        self, df: pd.DataFrame, indicator: str, params: IndicatorParams
    ) -> int:
        return self._engine.validate_min_periods(df, indicator, params)

    def _df_to_series(
        self,
        df: pd.DataFrame,
        col_names: list[str],
        indicator: str,
        params: IndicatorParams,
    ) -> list[TechnicalSeries]:
        return self._engine.to_series(df, col_names, indicator, params)

    async def _resolve_asset_id(self, ticker: str) -> int | None:
        if self._market_data is None:
            return None
        return await self._market_data.resolve_asset_id(ticker)
