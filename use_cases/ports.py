"""Structural contracts owned by the application layer.

Infrastructure adapters implement these protocols without the use cases
depending on SQLAlchemy, Redis, yfinance, or provider implementations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Protocol, TypeAlias, TypedDict

FundamentalPayload: TypeAlias = dict[str, object]
AssetProfile: TypeAlias = dict[str, object]


class AssetContext(TypedDict):
    details: AssetProfile
    mappings: dict[str, object]


class FundamentalsRepositoryPort(Protocol):
    """Persistence operations required by fundamental-data use cases."""

    def get_asset_details(
        self,
        ticker: str | None = None,
        isin: str | None = None,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> AssetProfile | None: ...

    def get_asset_context(
        self,
        ticker: str | None = None,
        isin: str | None = None,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> AssetContext | None: ...

    def get_deep_fundamentals(self, asset_id: int) -> FundamentalPayload | None: ...

    def get_deep_sections(
        self,
        asset_id: int,
        requested_sections: set[str],
        want_all: bool,
    ) -> FundamentalPayload: ...


class AssetProfileRepositoryPort(FundamentalsRepositoryPort, Protocol):
    """Additional profile operations required by the yfinance adapter."""

    def asset_profile_needs_enrichment(self, details: AssetProfile) -> bool: ...

    def asset_profile_enrichment_tickers(
        self,
        details: AssetProfile,
        request_ticker: str | None = None,
        limit: int = 5,
    ) -> list[str]: ...

    def metadata_has_profile_enrichment(self, metadata: Mapping[str, object]) -> bool: ...

    def update_asset_profile_from_metadata(
        self,
        asset_id: object,
        metadata: Mapping[str, object],
        listing_id: object = None,
    ) -> bool: ...


class AsyncJsonCachePort(Protocol):
    """Minimal async cache contract used by the aggregate endpoint."""

    async def get(self, key: str) -> str | bytes | None: ...

    def setex(self, key: str, ttl: int, value: str) -> Awaitable[object]: ...


class SyncJsonCachePort(Protocol):
    """Minimal synchronous cache contract used by deep fundamentals."""

    enabled: bool

    def get(self, key: str) -> FundamentalPayload | None: ...

    def set(
        self,
        key: str,
        value: FundamentalPayload,
        *,
        cache_type: str,
    ) -> object: ...


class ProviderRunnerPort(Protocol):
    async def run(
        self,
        *,
        ticker: str,
        isin: str | None,
        provider_params: Iterable[str],
        asset_mappings: Mapping[str, object],
        provider_default_tickers: Mapping[str, str] | None = None,
        asset_profile: Mapping[str, object] | None = None,
    ) -> tuple[FundamentalPayload, FundamentalPayload]: ...


class FundamentalsFormatterPort(Protocol):
    def to_eodhd(self, results: FundamentalPayload) -> FundamentalPayload: ...


class AssetProfileEnricherPort(Protocol):
    async def enrich(self, asset_profile: AssetProfile, ticker: str) -> None: ...


class DeepFundamentalsEnricherPort(Protocol):
    async def enrich(self, asset_id: int, ticker: str) -> FundamentalPayload: ...


class SecEdgarProviderPort(Protocol):
    def fetch(self, *, ticker: str, limit: int) -> Awaitable[object]: ...


TickerNormalizer = Callable[[str], str]
