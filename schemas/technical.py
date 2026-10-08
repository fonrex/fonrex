from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from settings import env_int

IndicatorParams: TypeAlias = dict[str, int | float]

# Number of bars loaded when a request gives no limit (TECHNICAL_DEFAULT_LIMIT).
TECHNICAL_MIN_LIMIT = 10
TECHNICAL_MAX_LIMIT = 5000
TECHNICAL_DEFAULT_LIMIT = env_int(
    "TECHNICAL_DEFAULT_LIMIT", 500, minimum=TECHNICAL_MIN_LIMIT, maximum=TECHNICAL_MAX_LIMIT
)


class IndicatorCategory(str, Enum):
    trend = "trend"
    momentum = "momentum"
    volatility = "volatility"
    volume = "volume"


class DataPoint(BaseModel):
    """Un point de données (timestamp + valeur) pour une série."""

    t: datetime  # timestamp
    v: Decimal | None  # valeur (None = NaN / warmup)


class TechnicalSeries(BaseModel):
    """
    Une série de valeurs pour un indicateur (ex: RSI_14, SMA_20...).
    Un indicateur peut avoir plusieurs séries (ex: MACD → 3 séries).
    """

    name: str  # ex: "RSI_14", "MACD_12_26_9", "BBU_20_2.0"
    label: str  # ex: "RSI", "MACD Line", "BB Upper"
    values: list[DataPoint]
    unit: str | None = None  # "%", "price", "volume"


class IndicatorResult(BaseModel):
    """Résultat d'un calcul d'indicateur unique."""

    model_config = ConfigDict(use_enum_values=True)

    ticker: str
    indicator: str
    params: IndicatorParams
    resolution: str
    category: IndicatorCategory
    from_date: datetime | None = None
    to_date: datetime | None = None
    count: int
    series: list[TechnicalSeries]
    cached: bool = False
    calculated_at: datetime


class OHLCVBar(BaseModel):
    """Barre OHLCV pour inclusion dans MultiIndicatorResult."""

    t: datetime
    o: Decimal | None = None
    h: Decimal | None = None
    l: Decimal | None = None
    c: Decimal | None = None
    v: int | None = None


class MultiIndicatorResult(BaseModel):
    """Résultat d'un calcul multi-indicateurs."""

    ticker: str
    resolution: str
    from_date: datetime | None = None
    to_date: datetime | None = None
    count: int
    ohlcv: list[OHLCVBar] | None = None  # si include_ohlcv=True
    indicators: dict[str, IndicatorResult]  # clé = "rsi_14", "macd"...
    errors: dict[str, str] = Field(default_factory=dict)
    calculated_at: datetime


class IndicatorInfo(BaseModel):
    """Description d'un indicateur disponible (pour GET /technical/list)."""

    model_config = ConfigDict(use_enum_values=True)

    name: str
    description: str
    category: IndicatorCategory
    params: IndicatorParams  # paramètres et leurs valeurs par défaut
    min_periods: int  # nombre minimum de bougies requises
    outputs: list[str]  # noms des séries produites
    example: str  # ex: "/technical/AIR.PA?indicator=rsi&period=14"


class TechnicalRequest(BaseModel):
    """Corps de POST /technical/batch."""

    tickers: list[str]
    indicators: list[str]  # ex: ["rsi_14", "macd", "bbands_20"]
    resolution: str = "1D"
    from_date: str | None = None
    to_date: str | None = None
    limit: int = Field(
        default=TECHNICAL_DEFAULT_LIMIT, ge=TECHNICAL_MIN_LIMIT, le=TECHNICAL_MAX_LIMIT
    )
    include_ohlcv: bool = False


class OHLCVWithIndicators(BaseModel):
    """Barre OHLCV avec indicateurs fusionnés."""

    t: datetime
    o: Decimal | None = None
    h: Decimal | None = None
    l: Decimal | None = None
    c: Decimal | None = None
    v: int | None = None
    indicators: dict[str, Decimal | None] = Field(default_factory=dict)
