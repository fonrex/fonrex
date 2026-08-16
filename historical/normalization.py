"""Normalization rules for external OHLCV bars."""

from __future__ import annotations

from typing import Any

import pandas as pd


def normalize_bars(
    bars: list[dict[str, Any]],
    asset_id: int,
    listing_id: int | None,
    resolution: str,
) -> list[dict[str, Any]]:
    """Normalize, validate and deduplicate external OHLCV bars."""
    normalized = []
    seen_timestamps = set()
    for bar in bars:
        timestamp = bar["timestamp"]
        if timestamp in seen_timestamps:
            continue
        seen_timestamps.add(timestamp)

        open_price = bar["open"]
        high = bar["high"]
        low = bar["low"]
        close = bar["close"]
        if any(pd.isna(value) for value in (open_price, high, low, close)):
            continue
        if high < low:
            high, low = low, high
        high = max(open_price, high, low, close)
        low = min(open_price, high, low, close)
        volume = max(0, bar["volume"]) if bar["volume"] is not None else 0

        normalized.append(
            {
                "time": timestamp,
                "asset_id": asset_id,
                "asset_listing_id": listing_id,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "adj_close": close,
                "volume": volume,
                "resolution": resolution,
                "adjusted": bar.get("adjusted", True),
                "source": bar.get("source", "unknown"),
            }
        )
    return normalized
