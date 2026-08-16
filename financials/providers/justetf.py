"""Asynchronous justETF provider.

The legacy :mod:`JustETF_provider` scraper is kept for compatibility with old
scripts.  This module is the application-facing adapter used by FastAPI.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from financials.providers.base import BaseFinancialProvider


class JustETFResult(BaseModel):
    """Normalized subset of justETF data returned by the API."""

    model_config = ConfigDict(extra="allow")

    isin: str
    name: Optional[str] = None
    ticker: Optional[str] = None
    net_expense_ratio: Optional[Decimal] = None
    total_net_assets: Optional[Decimal] = None
    domicile: Optional[str] = None
    replication_method: Optional[str] = None
    distribution_policy: Optional[str] = None
    index_tracked: Optional[str] = None
    inception_date: Optional[date] = None
    nb_holdings: Optional[int] = None
    performance: dict[str, Any] = Field(default_factory=dict)
    top_holdings: list[dict[str, Any]] = Field(default_factory=list)
    allocation: dict[str, Any] = Field(default_factory=dict)
    provider_url: Optional[str] = None


class JustETFProvider(BaseFinancialProvider):
    """Fetch and normalize UCITS ETF metadata from justETF."""

    name = "justetf"
    timeout = 12.0
    API_URL = "https://www.justetf.com/api/etfs/{isin}"
    PROFILE_URL = "https://www.justetf.com/en/etf-profile.html?isin={isin}"

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        **kwargs,
    ) -> Optional[JustETFResult]:
        target_isin = isin or ticker
        if not target_isin:
            return None
        result = await self._fetch_via_api(target_isin)
        if result is not None:
            return result
        return await self._fetch_via_scraping(target_isin, provider_url=provider_url)

    async def _fetch_via_api(self, isin: str) -> Optional[JustETFResult]:
        data = await self._get_json(self.API_URL.format(isin=isin))
        if not isinstance(data, dict):
            return None

        return JustETFResult(
            isin=isin,
            name=data.get("name"),
            ticker=data.get("ticker"),
            net_expense_ratio=self._percentage_from_api(data.get("ter")),
            total_net_assets=self._to_decimal(data.get("totalNetAssets")),
            domicile=data.get("domicile"),
            replication_method=data.get("replicationMethod"),
            distribution_policy=data.get("distributionPolicy"),
            index_tracked=data.get("index"),
            inception_date=self._parse_date(data.get("inceptionDate")),
            nb_holdings=self._to_int(data.get("numberOfHoldings")),
            performance=data.get("performance") or {},
            top_holdings=data.get("topHoldings") or [],
            allocation=data.get("allocation") or {},
            provider_url=self.PROFILE_URL.format(isin=isin),
        )

    async def _fetch_via_scraping(
        self, isin: str, provider_url: Optional[str] = None
    ) -> Optional[JustETFResult]:
        url = provider_url or self.PROFILE_URL.format(isin=isin)
        html = await self._get(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        values: dict[str, str] = {}
        for row in soup.select("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                values[cells[0].get_text(" ", strip=True).lower()] = cells[1].get_text(
                    " ", strip=True
                )

        holdings = []
        for table in soup.select("table.holding"):
            for row in table.select("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    holdings.append(
                        {
                            "name": cells[0].get_text(" ", strip=True),
                            "weight": self._parse_percentage(cells[1].get_text(" ", strip=True)),
                        }
                    )

        title = soup.find("h1")
        return JustETFResult(
            isin=isin,
            name=title.get_text(" ", strip=True) if title else None,
            net_expense_ratio=self._parse_percentage(values.get("ter")),
            total_net_assets=self._parse_currency_amount(values.get("taille du fonds")),
            domicile=values.get("domicile"),
            replication_method=values.get("réplication") or values.get("replication"),
            distribution_policy=values.get("distribution"),
            index_tracked=values.get("indice") or values.get("index"),
            inception_date=self._parse_date(values.get("date de lancement")),
            nb_holdings=len(holdings) or None,
            top_holdings=holdings,
            provider_url=url,
        )

    @staticmethod
    def _parse_percentage(value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        match = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value).replace("\u00a0", ""))
        if not match:
            return None
        try:
            return Decimal(match.group(0).replace(",", ".")) / Decimal("100")
        except InvalidOperation:
            return None

    @classmethod
    def _parse_currency_amount(cls, value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        text = str(value).strip().replace("\u00a0", " ")
        match = re.search(r"[-+]?\d+(?:[.,]\d+)?", text)
        if not match:
            return None
        try:
            amount = Decimal(match.group(0).replace(",", "."))
        except InvalidOperation:
            return None
        lowered = text.lower()
        compact = re.sub(r"\s+", "", lowered)
        if "mrd" in lowered or "billion" in lowered or compact.endswith("b"):
            amount *= Decimal("1000000000")
        elif "mio" in lowered or "million" in lowered or compact.endswith("m"):
            amount *= Decimal("1000000")
        return amount

    @staticmethod
    def _percentage_from_api(value: Any) -> Optional[Decimal]:
        parsed = JustETFProvider._to_decimal(value)
        return parsed / Decimal("100") if parsed is not None else None

    @staticmethod
    def _to_decimal(value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
