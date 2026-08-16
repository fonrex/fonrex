"""Pure pandas-based calculation and presentation helpers."""

from __future__ import annotations

import logging
import warnings
from decimal import Decimal

import numpy as np
import pandas as pd

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=r"The 'mode\.copy_on_write' option is deprecated.*")
    import pandas_ta as ta  # noqa: F401 - registers the DataFrame accessor

from schemas.technical import DataPoint, TechnicalSeries
from technical.catalog import INDICATOR_DEFAULTS, INDICATOR_REGISTRY
from technical.contracts import IndicatorParams
from technical.errors import IndicatorCalculationFailed

logger = logging.getLogger(__name__)


class TechnicalCalculationEngine:
    """Stateless calculations that operate only on in-memory data frames."""

    def calculate(self, df: pd.DataFrame, indicator: str, params: IndicatorParams) -> pd.DataFrame:
        try:
            function_name = INDICATOR_REGISTRY[indicator]["func"]
            if indicator == "vwap":
                self._calculate_vwap(df)
            else:
                function = getattr(df.ta, function_name)
                function(**params, append=True)
        except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise IndicatorCalculationFailed(f"Unable to calculate {indicator}: {exc}") from exc
        return df

    @staticmethod
    def _calculate_vwap(df: pd.DataFrame) -> None:
        if not isinstance(df.index, pd.DatetimeIndex):
            df.ta.vwap(append=True)
            return

        daily_values = []
        for _, group in df.groupby(df.index.date):
            if group.empty:
                continue
            try:
                daily_values.append(group.ta.vwap(append=False))
            except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
                raise IndicatorCalculationFailed(
                    f"Unable to calculate VWAP segment: {exc}"
                ) from exc
        df["VWAP_D"] = pd.concat(daily_values) if daily_values else np.nan

    @staticmethod
    def parse_name(name: str) -> tuple[str, IndicatorParams]:
        name_clean = name.strip().lower()
        if "_" in name_clean:
            indicator, raw_value = name_clean.split("_", 1)
            if indicator in INDICATOR_REGISTRY:
                params = INDICATOR_DEFAULTS[indicator].copy()
                try:
                    value = int(raw_value)
                except ValueError:
                    try:
                        value = float(raw_value)
                    except ValueError:
                        value = None
                parameter_names = INDICATOR_REGISTRY[indicator]["params"]
                if value is not None and parameter_names:
                    params[parameter_names[0]] = value
                return indicator, params

        return name_clean, INDICATOR_DEFAULTS.get(name_clean, {}).copy()

    @staticmethod
    def validate_min_periods(df: pd.DataFrame, indicator: str, params: IndicatorParams) -> int:
        length_indicators = {
            "sma",
            "ema",
            "wma",
            "dema",
            "tema",
            "cci",
            "roc",
            "mom",
            "atr",
            "kc",
            "mfi",
            "bbands",
        }
        if indicator in length_indicators:
            minimum = params.get("length", 20)
        elif indicator == "rsi":
            minimum = params.get("length", 14) + 1
        elif indicator == "macd":
            minimum = params.get("slow", 26) + params.get("signal", 9)
        elif indicator == "stoch":
            minimum = params.get("k", 14) + params.get("d", 3)
        else:
            minimum = 2

        if len(df) < minimum:
            raise ValueError(
                f"Not enough data to calculate {indicator}. "
                f"Required: {minimum}, Available: {len(df)}"
            )
        return len(df)

    @staticmethod
    def to_series(
        df: pd.DataFrame,
        column_names: list[str],
        indicator: str,
        _params: IndicatorParams,
    ) -> list[TechnicalSeries]:
        return [
            TechnicalCalculationEngine._build_series(df, column, indicator)
            for column in column_names
            if column in df.columns
        ]

    @staticmethod
    def _build_series(df: pd.DataFrame, column: str, indicator: str) -> TechnicalSeries:
        values = []
        for timestamp, raw_value in df[column].items():
            value = None
            if pd.notna(raw_value) and not np.isnan(raw_value) and not np.isinf(raw_value):
                try:
                    value = Decimal(str(round(raw_value, 4)))
                except (TypeError, ValueError, ArithmeticError):
                    value = None
            timestamp_value = (
                timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp
            )
            values.append(DataPoint(t=timestamp_value, v=value))

        return TechnicalSeries(
            name=column,
            label=TechnicalCalculationEngine._series_label(column),
            values=values,
            unit=TechnicalCalculationEngine._series_unit(indicator),
        )

    @staticmethod
    def _series_label(column: str) -> str:
        if column.startswith("MACD"):
            if "h" in column:
                return "Histogram"
            if "s" in column:
                return "Signal Line"
            return "MACD Line"
        if column.startswith("STOCH"):
            return "Stochastic %K" if "k" in column.lower() else "Stochastic %D"
        prefixes = {
            "BBL": "BB Lower",
            "BBM": "BB Middle",
            "BBU": "BB Upper",
            "BBB": "BB Bandwidth",
            "BBP": "BB %B",
            "KCL": "KC Lower",
            "KCB": "KC Basis",
            "KCU": "KC Upper",
        }
        for prefix, label in prefixes.items():
            if column.startswith(prefix):
                return label
        return column.split("_")[0].upper() if "_" in column else column.upper()

    @staticmethod
    def _series_unit(indicator: str) -> str | None:
        if indicator in {"rsi", "stoch", "mfi"}:
            return "%"
        if indicator in {"obv", "volume", "ad"}:
            return "volume"
        if indicator in {
            "sma",
            "ema",
            "wma",
            "dema",
            "tema",
            "vwap",
            "bbands",
            "kc",
            "atr",
        }:
            return "price"
        return None
