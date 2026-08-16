"""Typed contracts shared by the technical-analysis components."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias, TypedDict

if TYPE_CHECKING:
    import pandas as pd

IndicatorParameter: TypeAlias = int | float
IndicatorParams: TypeAlias = dict[str, IndicatorParameter]
IndicatorCategoryName: TypeAlias = Literal["trend", "momentum", "volatility", "volume"]

JsonScalar: TypeAlias = str | int | float | bool | None
CacheValue: TypeAlias = JsonScalar | Decimal | date | datetime | list[object] | dict[str, object]
CachePayload: TypeAlias = dict[str, CacheValue]


class IndicatorDefinition(TypedDict):
    """Declarative metadata required to calculate and expose an indicator."""

    func: str
    params: list[str]
    cols: list[str]
    category: IndicatorCategoryName


class AsyncRedisPort(Protocol):
    """Subset of the async Redis API required by technical indicators."""

    def get(self, key: str) -> Awaitable[str | bytes | None]: ...

    def setex(self, key: str, ttl: int, value: str) -> Awaitable[object]: ...


class TechnicalMarketDataPort(Protocol):
    """Market-data operations required by technical-analysis use cases."""

    async def load_ohlcv(
        self,
        asset_id: int,
        resolution: str,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
    ) -> pd.DataFrame: ...

    async def resolve_asset_id(self, ticker: str) -> int | None: ...


class TechnicalCachePort(Protocol):
    """Failure-neutral cache operations required by indicator use cases."""

    async def get(self, key: str) -> CachePayload | None: ...

    async def set(self, key: str, data: CachePayload, ttl: int) -> None: ...


class WebSocketPort(Protocol):
    """Framework-neutral WebSocket operations used by the connection manager."""

    def accept(self) -> Awaitable[None]: ...

    def send_json(self, data: Mapping[str, object]) -> Awaitable[None]: ...
