"""Concrete enrichment adapters composed at the HTTP boundary."""

from __future__ import annotations

import asyncio
import logging

from concurrency import run_sync
from financials.enrichment.yfinance_enricher import YFinanceEnricher
from fundamental.tools.ToolsBox import ToolsBox
from use_cases.ports import (
    AssetProfile,
    AssetProfileRepositoryPort,
    FundamentalPayload,
    FundamentalsRepositoryPort,
)

logger = logging.getLogger(__name__)


class YFinanceAssetProfileEnricher:
    """Enrich a persisted asset profile using yfinance metadata."""

    def __init__(
        self,
        database: AssetProfileRepositoryPort,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.database = database
        self.timeout_seconds = timeout_seconds

    async def enrich(self, asset_profile: AssetProfile, ticker: str) -> None:
        if not self.database.asset_profile_needs_enrichment(asset_profile):
            return

        # Kept lazy because import_assets is also a standalone CLI module.
        from import_assets import fetch_yfinance_data

        enrichment_tickers = self.database.asset_profile_enrichment_tickers(asset_profile, ticker)
        for enrichment_ticker in enrichment_tickers:
            metadata = await asyncio.wait_for(
                run_sync(fetch_yfinance_data, enrichment_ticker),
                timeout=self.timeout_seconds,
            )
            if not self.database.metadata_has_profile_enrichment(metadata):
                continue
            await run_sync(
                self.database.update_asset_profile_from_metadata,
                asset_profile.get("asset_id"),
                metadata,
                asset_profile.get("listing_id"),
            )
            return


class YFinanceDeepFundamentalsEnricher:
    """Adapter exposing the concrete yfinance deep enricher as an app port."""

    def __init__(self, database: FundamentalsRepositoryPort) -> None:
        self._delegate = YFinanceEnricher(database)

    async def enrich(self, asset_id: int, ticker: str) -> FundamentalPayload:
        return await self._delegate.enrich(asset_id, ticker)


def normalize_google_finance_ticker(ticker: str) -> str:
    """Translate a Google Finance identity into the canonical Yahoo form."""
    return ToolsBox().googleFinanceToYahooFinance(ticker)
