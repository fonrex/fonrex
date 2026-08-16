"""External market-data fetchers used by historical ingestion."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import websocket
import yfinance as yf

from concurrency import run_sync

logger = logging.getLogger(__name__)


class HistoricalMarketDataFetcher:
    """Fetch historical bars from yfinance or TradingView."""

    async def fetch_yfinance(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> dict[str, Any]:
        try:
            return await run_sync(self._fetch_yfinance_sync, ticker, resolution, start, end)
        except Exception as exc:
            logger.error("yfinance failed for %s: %s", ticker, exc)
            return {"bars": [], "source_used": "yfinance", "error": str(exc)}

    def _fetch_yfinance_sync(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> dict[str, Any]:
        interval = {"1D": "1d", "1W": "1wk", "1M": "1mo"}.get(resolution, "1d")
        dataframe = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval=interval,
            keepna=False,
        )
        if dataframe.empty:
            return {"bars": [], "source_used": "yfinance"}

        dataframe.index = pd.to_datetime(dataframe.index)
        if dataframe.index.tz is not None:
            dataframe.index = dataframe.index.tz_convert("UTC")
        else:
            dataframe.index = dataframe.index.tz_localize("UTC")

        bars = [
            {
                "timestamp": index.to_pydatetime(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": (
                    int(row["Volume"]) if "Volume" in row and not pd.isna(row["Volume"]) else 0
                ),
                "adjusted": True,
            }
            for index, row in dataframe.iterrows()
        ]
        return {"bars": bars, "source_used": "yfinance"}

    async def fetch_tradingview(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> dict[str, Any]:
        try:
            return await run_sync(self._fetch_tradingview_sync, ticker, resolution, start, end)
        except Exception as exc:
            logger.error("TradingView failed for %s: %s", ticker, exc)
            return {"bars": [], "source_used": "tradingview", "error": str(exc)}

    def _fetch_tradingview_sync(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> dict[str, Any]:
        symbol = self._resolve_tradingview_symbol(ticker)
        if not symbol:
            return {
                "bars": [],
                "source_used": "tradingview",
                "error": "TradingView symbol resolution failed",
            }

        resolution_code = {"1D": "D", "1W": "W", "1M": "M"}.get(resolution, "D")
        socket = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=screener%2F",
            headers={"Origin": "https://www.tradingview.com", "User-Agent": "Mozilla/5.0"},
            timeout=10,
        )

        def send_message(function: str, arguments: list[object]) -> None:
            message = json.dumps({"m": function, "p": arguments}, separators=(",", ":"))
            socket.send(f"~m~{len(message)}~m~{message}")

        delta_days = (end - start).days
        bar_count = max(10, delta_days)
        if resolution == "1W":
            bar_count = max(5, delta_days // 7 + 5)
        elif resolution == "1M":
            bar_count = max(5, delta_days // 30 + 5)

        send_message("set_auth_token", ["unauthorized_user_token"])
        send_message("chart_create_session", ["cs_ingest", ""])
        payload = json.dumps({"adjustment": "splits", "symbol": symbol})
        send_message("resolve_symbol", ["cs_ingest", "sds_sym_1", f"={payload}"])
        send_message(
            "create_series",
            ["cs_ingest", "sds_1", "s1", "sds_sym_1", resolution_code, bar_count, ""],
        )

        bars: list[dict[str, object]] = []
        try:
            for _ in range(30):
                response = socket.recv()
                if re.match(r"~m~\d+~m~~h~\d+$", response):
                    socket.send(response)
                    continue
                if "timescale_update" not in response:
                    continue
                self._append_tradingview_bars(response, start, end, bars)
                if bars:
                    break
        except Exception as exc:
            logger.error("TradingView stream failed: %s", exc)
        finally:
            try:
                socket.close()
            except Exception as exc:
                logger.debug("TradingView socket close failed: %s", exc)

        bars.sort(key=lambda bar: bar["timestamp"])
        return {"bars": bars, "source_used": "tradingview"}

    @staticmethod
    def _append_tradingview_bars(
        response: str,
        start: date,
        end: date,
        bars: list[dict[str, object]],
    ) -> None:
        for part in re.split(r"~m~\d+~m~", response):
            if not part:
                continue
            try:
                data = json.loads(part)
            except json.JSONDecodeError:
                continue
            if data.get("m") != "timescale_update":
                continue
            series = data["p"][1]["sds_1"]
            for item in series.get("s", []):
                values = item["v"]
                timestamp = datetime.fromtimestamp(values[0], tz=timezone.utc)
                if start <= timestamp.date() <= end:
                    bars.append(
                        {
                            "timestamp": timestamp,
                            "open": float(values[1]),
                            "high": float(values[2]),
                            "low": float(values[3]),
                            "close": float(values[4]),
                            "volume": int(values[5]) if len(values) > 5 else 0,
                            "adjusted": True,
                        }
                    )

    @staticmethod
    def _resolve_tradingview_symbol(ticker: str) -> str | None:
        if ":" in ticker:
            return ticker
        try:
            from tradingview_scraper.symbols.screener import Screener

            filters = [{"left": "name", "operation": "equal", "right": ticker}]
            for market in ["global", "america"]:
                result = Screener().screen(
                    market=market,
                    filters=filters,
                    columns=["name", "exchange", "type"],
                )
                if result.get("status") == "success" and result.get("data"):
                    return result["data"][0]["symbol"]
        except Exception as exc:
            logger.warning("TradingView symbol resolution failed for %s: %s", ticker, exc)
        return None
