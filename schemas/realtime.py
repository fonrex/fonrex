"""
Schémas Pydantic pour le système de streaming temps réel Fonrex.
Utilisés par les endpoints WebSocket, REST /quote et /realtime/subscribe.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator


class RealtimeTick(BaseModel):
    """Un tick de prix reçu en temps réel depuis TradingView."""

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
    Snapshot du dernier prix connu pour un ticker.
    Retourné par GET /quote/{ticker}.
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
    change: Decimal | None = None  # variation absolue vs clôture hier
    change_pct: Decimal | None = None  # variation en %
    previous_close: Decimal | None = None
    timestamp: datetime | None = None
    is_realtime: bool = False  # True si données < 2min
    is_market_open: bool | None = None
    source: str = "tradingview"
    delay_seconds: int | None = None  # délai estimé des données


class SubscriptionRequest(BaseModel):
    """Corps de POST /realtime/subscribe."""

    tickers: list[str]  # ex: ["AIR.PA", "BNP.PA", "AAPL"]


class SubscriptionStatus(BaseModel):
    """Statut d'abonnement pour un ticker."""

    model_config = ConfigDict(from_attributes=True)

    ticker: str
    tv_exchange: str
    tv_symbol: str
    is_active: bool
    subscribed_at: datetime
    last_tick_at: datetime | None = None
    tick_count: int = 0
    is_streaming: bool = False  # True si le worker a une connexion active


class WebSocketMessage(BaseModel):
    """
    Format standardisé des messages WebSocket Fonrex → Client.
    type : "tick" | "snapshot" | "error" | "subscribed" | "unsubscribed" | "pong"
    """

    type: str
    ticker: str | None = None
    data: dict[str, object] | None = None
    error: str | None = None
    ts: datetime | None = None
