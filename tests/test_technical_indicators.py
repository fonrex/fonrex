"""
Tests for TechnicalIndicatorService.

Strategy:
- Synthetic OHLCV data with known patterns (constant prices,
  growing trend, sinusoidal) to validate calculations
- Mock Redis with fake redis
- Mock DB with pre-generated data (no need for a real TimescaleDB)
- Validation of calculated values against known reference values
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from cache.technical import RedisTechnicalCache
from schemas.technical import IndicatorResult
from technical.errors import (
    InsufficientHistoricalData,
    InvalidIndicator,
    TechnicalDataNotFound,
    UnsupportedIndicatorResolution,
)
from technical.indicator_service import TechnicalIndicatorService

# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_ohlcv_df(n: int = 100, trend: str = "flat", base_price: float = 100.0) -> pd.DataFrame:
    """
    Generates a synthetic OHLCV DataFrame for testing.

    trend="flat"    → constant price (RSI ≈ 50, SMA ≈ close)
    trend="up"      → regularly increasing price
    trend="down"    → regularly decreasing price
    trend="sine"    → sinusoidal oscillation (RSI oscillates between 30 and 70)
    trend="overbought" → RSI > 70 guaranteed after warmup
    trend="oversold"   → RSI < 30 guaranteed after warmup
    """
    timestamps = pd.date_range(start="2024-01-01", periods=n, freq="D", tz="UTC")
    if trend == "flat":
        closes = np.full(n, base_price)
    elif trend == "up":
        closes = np.linspace(base_price, base_price * 1.5, n)
    elif trend == "down":
        closes = np.linspace(base_price, base_price * 0.5, n)
    elif trend == "sine":
        closes = base_price + 10 * np.sin(np.linspace(0, 4 * np.pi, n))
    elif trend == "overbought":
        closes = np.concatenate(
            [np.full(20, base_price), np.linspace(base_price, base_price * 2.5, n - 20)]
        )
    elif trend == "oversold":
        closes = np.concatenate(
            [np.full(20, base_price), np.linspace(base_price, base_price * 0.1, n - 20)]
        )
    else:
        closes = np.full(n, base_price)

    # Use small noise
    noise = np.random.uniform(-0.1, 0.1, n)
    opens = closes - noise
    highs = np.maximum(opens, closes) + abs(noise) + 0.1
    lows = np.minimum(opens, closes) - abs(noise) - 0.1
    volumes = np.random.randint(500000, 2000000, n).astype(np.int64)

    return pd.DataFrame(
        {
            "open": opens.astype(np.float64),
            "high": highs.astype(np.float64),
            "low": lows.astype(np.float64),
            "close": closes.astype(np.float64),
            "volume": volumes,
        },
        index=timestamps,
    )


class AsyncFakeRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.store:
                deleted += 1
                del self.store[key]
        return deleted


# ── RSI Tests ─────────────────────────────────────────────────────────────────


class TestRSI:
    def test_rsi_range_always_0_100(self):
        """RSI must always be between 0 and 100."""
        df = make_ohlcv_df(120, trend="sine")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "rsi", {"length": 14})

        rsi_col = [c for c in calc_df.columns if "RSI" in c][0]
        rsi_vals = calc_df[rsi_col].dropna()
        assert len(rsi_vals) > 0
        assert rsi_vals.min() >= 0.0
        assert rsi_vals.max() <= 100.0

    def test_rsi_overbought_above_70(self):
        """Strong uptrend → RSI > 70 after warmup."""
        df = make_ohlcv_df(120, trend="overbought")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "rsi", {"length": 14})

        rsi_col = [c for c in calc_df.columns if "RSI" in c][0]
        rsi_vals = calc_df[rsi_col].dropna()
        assert len(rsi_vals) > 0
        assert rsi_vals.max() > 70.0

    def test_rsi_oversold_below_30(self):
        """Strong downtrend → RSI < 30 after warmup."""
        df = make_ohlcv_df(120, trend="oversold")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "rsi", {"length": 14})

        rsi_col = [c for c in calc_df.columns if "RSI" in c][0]
        rsi_vals = calc_df[rsi_col].dropna()
        assert len(rsi_vals) > 0
        assert rsi_vals.min() < 30.0

    def test_rsi_flat_market_near_50(self):
        """Constant prices → RSI near 50."""
        df = make_ohlcv_df(120, trend="flat")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "rsi", {"length": 14})

        rsi_col = [c for c in calc_df.columns if "RSI" in c][0]
        rsi_vals = calc_df[rsi_col].dropna()
        # With zero price variations, pandas-ta handles division by zero or constant price.
        # It's usually NaN or near 50 depending on initialization. If it has values, check they are around 50.
        if len(rsi_vals) > 0:
            for val in rsi_vals:
                assert 45.0 <= val <= 55.0 or np.isnan(val)

    def test_rsi_warmup_nan_count(self):
        """The first value must be NaN (pandas-ta v0.4.71b0 warmup)."""
        df = make_ohlcv_df(50, trend="up")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "rsi", {"length": 14})

        rsi_col = [c for c in calc_df.columns if "RSI" in c][0]
        # pandas-ta v0.4.71b0: only the 1st value is NaN,
        # the following are calculated via Wilder smoothing starting from the 2nd point
        assert calc_df[rsi_col].iloc[0] != calc_df[rsi_col].iloc[0]  # NaN != NaN
        # All values after the 1st must be defined
        assert not calc_df[rsi_col].iloc[1:].isna().any()

    def test_rsi_custom_period(self):
        """RSI with period=7 vs period=21 — different values."""
        df = make_ohlcv_df(100, trend="sine")
        service = TechnicalIndicatorService(None)

        df_7 = service._calculate_indicator_on_df(df.copy(), "rsi", {"length": 7})
        df_21 = service._calculate_indicator_on_df(df.copy(), "rsi", {"length": 21})

        rsi_7_col = [c for c in df_7.columns if "RSI" in c][0]
        rsi_21_col = [c for c in df_21.columns if "RSI" in c][0]

        # Compare values after index 25 (warmup for both)
        diff = df_7[rsi_7_col].iloc[25:] - df_21[rsi_21_col].iloc[25:]
        assert (diff.abs() > 0.001).any()


# ── SMA / EMA Tests ───────────────────────────────────────────────────────────


class TestMovingAverages:
    def test_sma_equals_manual_calculation(self):
        """SMA(20) must match the manual average of the last 20 values."""
        df = make_ohlcv_df(50, trend="up")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "sma", {"length": 20})

        sma_col = [c for c in calc_df.columns if "SMA" in c][0]

        # Manual average at index 30
        manual_avg = df["close"].iloc[11:31].mean()
        sma_val = calc_df[sma_col].iloc[30]
        assert abs(sma_val - manual_avg) < 0.0001

    def test_ema_reacts_faster_than_sma(self):
        """After a price change, EMA must react faster than SMA."""
        # Flat for 20 days then a jump
        closes = np.concatenate([np.full(20, 100.0), np.full(20, 150.0)])
        timestamps = pd.date_range("2026-01-01", periods=40, tz="UTC")
        df = pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000},
            index=timestamps,
        )

        service = TechnicalIndicatorService(None)
        df_sma = service._calculate_indicator_on_df(df.copy(), "sma", {"length": 10})
        df_ema = service._calculate_indicator_on_df(df.copy(), "ema", {"length": 10})

        sma_col = [c for c in df_sma.columns if "SMA" in c][0]
        ema_col = [c for c in df_ema.columns if "EMA" in c][0]

        # Check first days after jump (index 20 is the first jump day)
        # EMA should increase faster towards 150
        assert df_ema[ema_col].iloc[20] > df_sma[sma_col].iloc[20]
        assert df_ema[ema_col].iloc[21] > df_sma[sma_col].iloc[21]

    def test_sma_flat_equals_price(self):
        """On constant price, SMA must equal the price."""
        df = make_ohlcv_df(50, trend="flat", base_price=120.0)
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "sma", {"length": 10})

        sma_col = [c for c in calc_df.columns if "SMA" in c][0]
        sma_vals = calc_df[sma_col].dropna()
        assert (sma_vals == 120.0).all()

    def test_warmup_nan_count_sma(self):
        """SMA(20): the first 19 values are NaN."""
        df = make_ohlcv_df(40, trend="up")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "sma", {"length": 20})

        sma_col = [c for c in calc_df.columns if "SMA" in c][0]
        assert calc_df[sma_col].iloc[:19].isna().all()
        assert not calc_df[sma_col].iloc[19:].isna().any()


# ── MACD Tests ────────────────────────────────────────────────────────────────


class TestMACD:
    def test_macd_returns_three_series(self):
        """MACD must return 3 series: MACD line, Signal, Histogram."""
        df = make_ohlcv_df(60, trend="sine")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(
            df, "macd", {"fast": 12, "slow": 26, "signal": 9}
        )

        macd_cols = [c for c in calc_df.columns if "MACD" in c]
        # Expecting MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
        assert len(macd_cols) == 3
        assert any("MACDh" in c for c in macd_cols)  # histogram
        assert any("MACDs" in c for c in macd_cols)  # signal
        assert any(c.startswith("MACD_") for c in macd_cols)  # macd line

    def test_macd_histogram_equals_diff(self):
        """Histogram = MACD line - Signal line."""
        df = make_ohlcv_df(60, trend="sine")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(
            df, "macd", {"fast": 12, "slow": 26, "signal": 9}
        )

        macd_line = calc_df["MACD_12_26_9"]
        macd_hist = calc_df["MACDh_12_26_9"]
        macd_sign = calc_df["MACDs_12_26_9"]

        diff = macd_line - macd_sign
        pd.testing.assert_series_equal(
            macd_hist.dropna(), diff.dropna(), check_names=False, atol=1e-5
        )

    def test_macd_crossover_detected(self):
        """MACD/Signal crossover visible on changing trend."""
        # Sine trend guarantees crossings
        df = make_ohlcv_df(100, trend="sine")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(
            df, "macd", {"fast": 12, "slow": 26, "signal": 9}
        )

        macd_hist = calc_df["MACDh_12_26_9"].dropna()
        # Find where hist sign changes (crossovers)
        signs = np.sign(macd_hist)
        diff_signs = signs.diff().dropna()
        crossovers = diff_signs[diff_signs != 0]
        assert len(crossovers) > 0


# ── Bollinger Bands Tests ─────────────────────────────────────────────────────


class TestBollingerBands:
    def test_bbands_returns_five_series(self):
        """Bollinger must return 5 series (Lower, Mid, Upper, Bandwidth, %B)."""
        df = make_ohlcv_df(50, trend="up")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(df, "bbands", {"length": 20, "std": 2.0})

        bb_cols = [c for c in calc_df.columns if c.startswith("BB")]
        assert len(bb_cols) == 5  # BBL, BBM, BBU, BBB, BBP

    def test_bbands_upper_always_above_lower(self):
        """Upper band always >= Lower band."""
        df = make_ohlcv_df(50, trend="sine")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(
            df, "bbands", {"length": 20, "std": 2.0, "ddof": 2.0}
        )

        bbu = calc_df["BBU_20_2.0_2.0"].dropna()
        bbl = calc_df["BBL_20_2.0_2.0"].dropna()
        assert (bbu >= bbl).all()

    def test_bbands_middle_equals_sma(self):
        """Middle band = SMA of the same period."""
        df = make_ohlcv_df(50, trend="sine")
        service = TechnicalIndicatorService(None)
        calc_df = service._calculate_indicator_on_df(
            df, "bbands", {"length": 20, "std": 2.0, "ddof": 2.0}
        )
        df_sma = service._calculate_indicator_on_df(df.copy(), "sma", {"length": 20})

        bbm = calc_df["BBM_20_2.0_2.0"].dropna()
        sma = df_sma["SMA_20"].dropna()
        pd.testing.assert_series_equal(bbm, sma, check_names=False, atol=1e-5)

    def test_bbands_narrow_on_flat_market(self):
        """Narrow bands on flat market (low volatility)."""
        df_flat = make_ohlcv_df(50, trend="flat", base_price=100.0)
        df_volatile = make_ohlcv_df(50, trend="sine", base_price=100.0)

        service = TechnicalIndicatorService(None)
        calc_flat = service._calculate_indicator_on_df(
            df_flat, "bbands", {"length": 20, "std": 2.0, "ddof": 2.0}
        )
        calc_vol = service._calculate_indicator_on_df(
            df_volatile, "bbands", {"length": 20, "std": 2.0, "ddof": 2.0}
        )

        width_flat = (calc_flat["BBU_20_2.0_2.0"] - calc_flat["BBL_20_2.0_2.0"]).mean()
        width_vol = (calc_vol["BBU_20_2.0_2.0"] - calc_vol["BBL_20_2.0_2.0"]).mean()

        assert width_flat < width_vol
        assert width_flat < 1.0  # very narrow, close to 0


# ── Service Tests ─────────────────────────────────────────────────────────────


class TestTechnicalIndicatorService:
    @pytest.mark.asyncio
    async def test_market_data_is_accessed_through_injected_port(self):
        frame = make_ohlcv_df(20)
        market_data = MagicMock()
        market_data.resolve_asset_id = AsyncMock(return_value=42)
        market_data.load_ohlcv = AsyncMock(return_value=frame)
        service = TechnicalIndicatorService(market_data)

        assert await service._resolve_asset_id("AAPL") == 42
        assert await service._load_ohlcv_dataframe(42, "1D", limit=20) is frame
        market_data.resolve_asset_id.assert_awaited_once_with("AAPL")
        market_data.load_ohlcv.assert_awaited_once_with(42, "1D", None, None, 20)

    @pytest.mark.asyncio
    async def test_unknown_indicator_raises_400(self):
        """Unknown indicators are reported without a transport dependency."""
        service = TechnicalIndicatorService(None)
        with pytest.raises(InvalidIndicator) as exc:
            await service.calculate("AIR.PA", "unknown_indicator")
        assert "Unknown indicator" in exc.value.detail

    @pytest.mark.asyncio
    async def test_insufficient_data_raises_422(self):
        """Insufficient history is a transport-independent domain failure."""
        # Set up mock DatabaseService returning small DataFrame
        mock_db = MagicMock()
        service = TechnicalIndicatorService(mock_db)

        df_small = make_ohlcv_df(5, trend="flat")

        with (
            patch.object(service, "_resolve_asset_id", return_value=1),
            patch.object(service, "_load_ohlcv_dataframe", return_value=df_small),
        ):
            with pytest.raises(InsufficientHistoricalData) as exc:
                await service.calculate("AIR.PA", "rsi", period=14)
            assert "Not enough data" in exc.value.detail

    @pytest.mark.asyncio
    async def test_redis_cache_hit(self):
        """Second identical call → returned from Redis (cached=True)."""
        fake_redis = AsyncFakeRedis()
        mock_db = MagicMock()
        service = TechnicalIndicatorService(mock_db, cache=RedisTechnicalCache(fake_redis))

        df = make_ohlcv_df(50, trend="sine")

        original_env = os.environ.get("TECHNICAL_CACHE_ENABLED")
        os.environ["TECHNICAL_CACHE_ENABLED"] = "true"
        try:
            with (
                patch.object(service, "_resolve_asset_id", return_value=1),
                patch.object(service, "_load_ohlcv_dataframe", return_value=df),
            ):
                # First call: calculates
                res1 = await service.calculate("AIR.PA", "rsi")
                assert res1.cached is False

                # Let the fire-and-forget cache write task complete
                await asyncio.sleep(0)

                # Second call: reads cache
                res2 = await service.calculate("AIR.PA", "rsi")
                assert res2.cached is True
                assert len(res2.series[0].values) == len(res1.series[0].values)
        finally:
            if original_env is None:
                os.environ.pop("TECHNICAL_CACHE_ENABLED", None)
            else:
                os.environ["TECHNICAL_CACHE_ENABLED"] = original_env

    @pytest.mark.asyncio
    async def test_multi_loads_df_once(self):
        """calculate_multi with 5 indicators → _load_ohlcv_dataframe called once."""
        mock_db = MagicMock()
        service = TechnicalIndicatorService(mock_db)

        df = make_ohlcv_df(100, trend="up")

        with (
            patch.object(service, "_resolve_asset_id", return_value=1),
            patch.object(service, "_load_ohlcv_dataframe", return_value=df) as mock_load,
        ):
            res = await service.calculate_multi(
                ticker="AIR.PA",
                indicators=["sma_20", "ema_50", "rsi_14", "macd", "bbands_20"],
                resolution="1D",
            )

            mock_load.assert_called_once()
            assert len(res.indicators) == 5
            assert "sma_20" in res.indicators
            assert "macd" in res.indicators

    @pytest.mark.asyncio
    async def test_parse_indicator_name(self):
        """
        "rsi_14"    → ("rsi", {"length": 14})
        "sma_50"    → ("sma", {"length": 50})
        "macd"      → ("macd", {"fast": 12, "slow": 26, "signal": 9})
        "bbands_20" → ("bbands", {"length": 20, "std": 2.0})
        """
        service = TechnicalIndicatorService(None)

        assert service._parse_indicator_name("rsi_14") == ("rsi", {"length": 14})
        assert service._parse_indicator_name("sma_50") == ("sma", {"length": 50})
        assert service._parse_indicator_name("macd") == (
            "macd",
            {"fast": 12, "slow": 26, "signal": 9},
        )
        assert service._parse_indicator_name("bbands_20") == (
            "bbands",
            {"length": 20, "std": 2.0, "ddof": 2.0},
        )

    @pytest.mark.asyncio
    async def test_vwap_requires_volume(self):
        """VWAP rejects daily data through a domain-specific error."""
        service = TechnicalIndicatorService(None)
        with pytest.raises(UnsupportedIndicatorResolution) as exc:
            await service.calculate("AIR.PA", "vwap", resolution="1D")
        assert "VWAP only available on intraday data" in exc.value.detail

    @pytest.mark.asyncio
    async def test_missing_ticker_raises_domain_error(self):
        service = TechnicalIndicatorService(None)
        with patch.object(service, "_resolve_asset_id", return_value=None):
            with pytest.raises(TechnicalDataNotFound) as exc:
                await service.calculate("UNKNOWN", "rsi")
        assert exc.value.detail == "Ticker not found: UNKNOWN"

    @pytest.mark.parametrize(
        ("error", "expected_status"),
        [
            (InvalidIndicator("invalid"), 400),
            (UnsupportedIndicatorResolution("unsupported"), 400),
            (TechnicalDataNotFound("missing"), 404),
            (InsufficientHistoricalData("insufficient"), 422),
        ],
    )
    def test_http_adapter_translates_technical_errors(self, error, expected_status):
        from routers.technical import _raise_technical_http_error

        with pytest.raises(HTTPException) as exc:
            _raise_technical_http_error(error)
        assert exc.value.status_code == expected_status
        assert exc.value.detail == error.detail

    @pytest.mark.asyncio
    async def test_screen_rsi_oversold(self):
        """RSI < 30 screener → returns assets with RSI < 30."""
        from routers.technical import run_indicator_screen
        from schemas.technical import DataPoint, IndicatorCategory, TechnicalSeries

        # We need mock listings
        mock_listing1 = MagicMock()
        mock_listing1.ticker = "OVERSOLD.PA"
        mock_listing1.asset = MagicMock()
        mock_listing1.asset.name = "Oversold Asset"
        mock_listing1.asset.isin = "FR1111111111"

        mock_listing2 = MagicMock()
        mock_listing2.ticker = "NORMAL.PA"
        mock_listing2.asset = MagicMock()
        mock_listing2.asset.name = "Normal Asset"
        mock_listing2.asset.isin = "FR2222222222"

        mock_db_session = MagicMock()
        mock_db_session.query.return_value.join.return_value.filter.return_value.limit.return_value.all.return_value = [
            mock_listing1,
            mock_listing2,
        ]

        now = datetime.now(timezone.utc)
        res_oversold = IndicatorResult(
            ticker="OVERSOLD.PA",
            indicator="rsi",
            params={"length": 14},
            resolution="1D",
            category=IndicatorCategory.momentum,
            count=50,
            series=[
                TechnicalSeries(
                    name="RSI_14", label="RSI", values=[DataPoint(t=now, v=Decimal("25.0"))]
                )
            ],
            calculated_at=now,
        )

        res_normal = IndicatorResult(
            ticker="NORMAL.PA",
            indicator="rsi",
            params={"length": 14},
            resolution="1D",
            category=IndicatorCategory.momentum,
            count=50,
            series=[
                TechnicalSeries(
                    name="RSI_14", label="RSI", values=[DataPoint(t=now, v=Decimal("55.0"))]
                )
            ],
            calculated_at=now,
        )

        async def mock_calculate(ticker, **kwargs):
            if ticker == "OVERSOLD.PA":
                return res_oversold
            return res_normal

        mock_db_service = MagicMock()
        mock_db_service.get_session.return_value = mock_db_session
        mock_tech_service = MagicMock()
        mock_tech_service.calculate = AsyncMock(side_effect=mock_calculate)

        original_env = os.environ.get("TECHNICAL_CACHE_ENABLED")
        try:
            os.environ["TECHNICAL_CACHE_ENABLED"] = "false"
            result = await run_indicator_screen(
                db_service=mock_db_service,
                technical_service=mock_tech_service,
                indicator="rsi",
                operator="lt",
                value=30.0,
                resolution="1D",
                period=14,
                limit=50,
            )

            assert result["total"] == 1
            assert result["matches"][0]["ticker"] == "OVERSOLD.PA"
            assert result["matches"][0]["value"] == "25.0"
        finally:
            if original_env is None:
                os.environ.pop("TECHNICAL_CACHE_ENABLED", None)
            else:
                os.environ["TECHNICAL_CACHE_ENABLED"] = original_env
