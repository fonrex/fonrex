"""External market-data fetchers used by historical ingestion."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import websocket
import yfinance as yf

from concurrency import run_sync
from database.price_series import session_timestamp

logger = logging.getLogger(__name__)


def _price(value: Any) -> float:
    """A Yahoo price without the noise of its 32-bit origin.

    Yahoo publishes prices as 32-bit numbers: ``31.378`` arrives as
    ``31.378000259399414``. Eight significant digits keep everything such a
    number carries and give the price as it is quoted.
    """
    number = float(value)
    return number if pd.isna(number) else float(f"{number:.8g}")


class HistoricalMarketDataFetcher:
    """Fetch historical bars from yfinance or TradingView."""

    async def fetch(
        self,
        ticker: str,
        resolution: str,
        source: str,
        start: date,
        end: date,
        *,
        yahoo_symbol: str | None = None,
        currency: str | None = None,
        symbol_note: str | None = None,
        pause: float = 0.0,
    ) -> dict[str, Any]:
        """Fetch bars from the chosen source; in ``auto`` mode yfinance, then TradingView.

        yfinance is queried with ``yahoo_symbol`` — the symbol verified for the
        listing — and not at all without one: the ticker as typed may be the
        symbol of another instrument. TradingView is searched by ticker and only
        a line quoted in ``currency`` is accepted.
        """
        no_symbol = {
            "bars": [],
            "source_used": "yfinance",
            "error": symbol_note or f"No verified Yahoo symbol for {ticker}",
        }
        # TradingView names a line by its ticker on an exchange, without suffix.
        tv_ticker = ticker if ":" in ticker else (yahoo_symbol or ticker).split(".")[0]

        if source == "yfinance":
            if not yahoo_symbol:
                return no_symbol
            return await self.fetch_yfinance(yahoo_symbol, resolution, start, end)
        if source == "tradingview":
            return await self.fetch_tradingview(tv_ticker, resolution, start, end, currency)

        if yahoo_symbol:
            result = await self.fetch_yfinance(yahoo_symbol, resolution, start, end)
            if result.get("bars"):
                return result
            why_not_yahoo = f"Yahoo has no bar for {yahoo_symbol}" + (
                f" ({result['error']})" if result.get("error") else ""
            )
            await asyncio.sleep(pause)
        else:
            why_not_yahoo = no_symbol["error"]
        logger.warning("%s: %s; trying TradingView", ticker, why_not_yahoo)

        result = await self.fetch_tradingview(tv_ticker, resolution, start, end, currency)
        if result.get("bars"):
            # The answer comes from the second source: say why, or a symbol that
            # stopped being verified would go unnoticed behind a success.
            return {**result, "note": why_not_yahoo}
        if not yahoo_symbol:
            # Say why Yahoo was not used: it is the reason the listing has no price.
            reason = f"{why_not_yahoo}; {result.get('error') or 'no bar on TradingView'}"
            result = {**result, "error": reason}
        return result

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
            return {"bars": [], "source_used": "yfinance", "symbol": ticker}

        # yfinance dates a bar at midnight in the time zone of the exchange. The
        # date of that local midnight is the trading session: it is kept as is.
        # Converting the instant to UTC would move every exchange east of
        # Greenwich to the previous day (8 January in Paris -> 7 January 23:00Z).
        dataframe.index = pd.to_datetime(dataframe.index)

        bars = [
            {
                "timestamp": session_timestamp(index.date()),
                "open": _price(row["Open"]),
                "high": _price(row["High"]),
                "low": _price(row["Low"]),
                "close": _price(row["Close"]),
                "volume": (
                    int(row["Volume"]) if "Volume" in row and not pd.isna(row["Volume"]) else 0
                ),
                "adjusted": True,
                "source": "yfinance",
            }
            for index, row in dataframe.iterrows()
        ]
        return {"bars": bars, "source_used": "yfinance", "symbol": ticker}

    async def fetch_tradingview(
        self, ticker: str, resolution: str, start: date, end: date, currency: str | None = None
    ) -> dict[str, Any]:
        try:
            return await run_sync(
                self._fetch_tradingview_sync, ticker, resolution, start, end, currency
            )
        except Exception as exc:
            logger.error("TradingView failed for %s: %s", ticker, exc)
            return {"bars": [], "source_used": "tradingview", "error": str(exc)}

    def _fetch_tradingview_sync(
        self, ticker: str, resolution: str, start: date, end: date, currency: str | None = None
    ) -> dict[str, Any]:
        symbol = self._resolve_tradingview_symbol(ticker, currency)
        if not symbol:
            wanted = f" quoted in {currency}" if currency else ""
            return {
                "bars": [],
                "source_used": "tradingview",
                "error": f"No TradingView symbol found for {ticker}{wanted}",
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
        return {"bars": bars, "source_used": "tradingview", "symbol": symbol}

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
                # TradingView dates a bar at the opening of its session, in UTC:
                # its UTC date is the session date (except for the few exchanges
                # that open before midnight UTC, such as Sydney in summer time).
                session = datetime.fromtimestamp(values[0], tz=timezone.utc).date()
                if start <= session <= end:
                    bars.append(
                        {
                            "timestamp": session_timestamp(session),
                            "open": float(values[1]),
                            "high": float(values[2]),
                            "low": float(values[3]),
                            "close": float(values[4]),
                            "volume": int(values[5]) if len(values) > 5 else 0,
                            "adjusted": True,
                            "source": "tradingview",
                        }
                    )

    @staticmethod
    def _resolve_tradingview_symbol(ticker: str, currency: str | None = None) -> str | None:
        """Find the TradingView symbol of a ticker.

        Several instruments share a ticker across markets. When the currency of
        the listing is given, only a line quoted in that currency is accepted;
        a line whose currency is unknown is refused rather than assumed.
        """
        if ":" in ticker:
            return ticker
        try:
            from tradingview_scraper.symbols.screener import Screener

            filters = [{"left": "name", "operation": "equal", "right": ticker}]
            for market in ["global", "america"]:
                result = Screener().screen(
                    market=market,
                    filters=filters,
                    columns=["name", "exchange", "type", "currency"],
                )
                if result.get("status") != "success":
                    continue
                for row in result.get("data") or []:
                    if not currency:
                        return row["symbol"]
                    if str(row.get("currency") or "").upper() == currency.upper():
                        return row["symbol"]
        except Exception as exc:
            logger.warning("TradingView symbol resolution failed for %s: %s", ticker, exc)
        return None
