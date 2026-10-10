"""
Pydantic schemas of the Fonrex realtime streaming.
Used by the WebSocket route, REST /quote and /realtime/subscribe.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator


class RealtimeTick(BaseModel):
    """A price tick received from TradingView."""

    ticker: str
    timestamp: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal
    volume: int | None = None
    source: str = "tradingview"
    exchange: str | None = None
    currency: str | None = None

    @field_validator("close")
    @classmethod
    def close_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("close price must be positive")
        return v


class QuoteSnapshot(BaseModel):
    """
    Snapshot of the last known price of a ticker.
    Returned by GET /quote/{ticker}.
    """

    ticker: str
    isin: str | None = None
    name: str | None = None
    exchange: str | None = None
    currency: str | None = None
    price: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    change: Decimal | None = None  # absolute change vs. yesterday's close
    change_pct: Decimal | None = None  # change in %
    previous_close: Decimal | None = None
    timestamp: datetime | None = None
    is_realtime: bool = False  # True when the data is < 2min old
    is_market_open: bool | None = None
    source: str = "tradingview"
    delay_seconds: int | None = None  # estimated delay of the data


class SubscriptionRequest(BaseModel):
    """Body of POST /realtime/subscribe."""

    tickers: list[str]  # e.g. ["AIR.PA", "BNP.PA", "AAPL"]


class SubscriptionStatus(BaseModel):
    """Subscription status of a ticker."""

    model_config = ConfigDict(from_attributes=True)

    ticker: str
    tv_exchange: str
    tv_symbol: str
    is_active: bool
    subscribed_at: datetime
    last_tick_at: datetime | None = None
    tick_count: int = 0
    is_streaming: bool = False  # True when the worker has an active connection


class WebSocketMessage(BaseModel):
    """
    Standard format of the Fonrex WebSocket messages → client.
    type : "tick" | "snapshot" | "error" | "subscribed" | "unsubscribed" | "pong"
           | "not_streaming" (read-only key on a ticker that is not streamed)
    """

    type: str
    ticker: str | None = None
    data: dict[str, object] | None = None
    error: str | None = None
    ts: datetime | None = None
