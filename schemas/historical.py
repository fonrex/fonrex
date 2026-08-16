from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Resolution(str, Enum):
    daily = "1D"
    weekly = "1W"
    monthly = "1M"


class DataSource(str, Enum):
    auto = "auto"
    yfinance = "yfinance"
    tradingview = "tradingview"


class OHLCVBar(BaseModel):
    """Une bougie OHLCV normalisée."""

    model_config = ConfigDict(from_attributes=True)
    timestamp: datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[int] = None
    resolution: str = "1D"
    adjusted: bool = True
    source: Optional[str] = None


class IngestResult(BaseModel):
    """Résultat d'une opération d'ingestion."""

    ticker: str
    resolution: str
    status: str  # "success" | "partial" | "up_to_date" | "failed"
    source_used: Optional[str] = None
    records_added: int = 0
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None


class HistoryResponse(BaseModel):
    """Réponse de GET /ticker/{symbol}/history."""

    ticker: str
    isin: Optional[str] = None
    exchange: Optional[str] = None
    currency: Optional[str] = None
    resolution: str
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    count: int
    source: Optional[str] = None
    bars: List[OHLCVBar] = []


class IngestRequest(BaseModel):
    """Corps de POST /ingest/{ticker}."""

    resolution: Resolution = Resolution.daily
    source: DataSource = DataSource.auto
    force_refresh: bool = False
    from_date: Optional[date] = None  # None = depuis le début disponible
    to_date: Optional[date] = None  # None = aujourd'hui


class BulkIngestRequest(BaseModel):
    """Corps de POST /ingest/bulk."""

    tickers: List[str]
    resolution: Resolution = Resolution.daily
    source: DataSource = DataSource.auto
    force_refresh: bool = False
    concurrency: int = Field(default=5, ge=1, le=20)
