"""MorningStarProvider — the figures of a share from the JSON services of Morningstar.

The quote page of global.morningstar.com is filled in the browser by JavaScript
components, and answers a script with an AWS WAF challenge (HTTP 202): it is never
read. Its components call two JSON services, which answer a script directly:

* the screener of ``lt.morningstar.com`` turns an ISIN (or a ticker) into the
  Morningstar identifier of a listing (``0P0000OQN8`` for Tesla);
* ``api-global.morningstar.com/sal-service`` gives the overview of that listing
  (price, valuation, ranges, style) and the profile of its company (sector, staff).

The ``ApiKey`` header is the public key of those components, written in their
JavaScript; ``MORNINGSTAR_SAL_API_KEY`` replaces it if Morningstar changes it. The
services are not documented: a failure of one of them leaves its fields empty.
Ratios such as the dividend yield are given as ratios (``0.055754`` for 5.58 %).
"""

import asyncio
import logging
import os
import re
from typing import Any, Optional

from financials.models import FinancialMetrics
from financials.numbers import parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

# A Morningstar identifier: "0P" and eight letters or digits.
_MS_ID = re.compile(r"\b(0P[0-9A-Z]{8})\b")
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")

SAL_API_KEY_ENV_VAR = "MORNINGSTAR_SAL_API_KEY"
# The public key of the components of global.morningstar.com (sal-common-*.js).
DEFAULT_SAL_API_KEY = "lstzFDEOhfFNMLikKa0am9mgEKLBl49T"
SAL_VERSION = "5.21.0"

# Placeholder of the figures reserved to paying users ("premium only").
_PREMIUM_ONLY = "_PO_"

# Morningstar style box: size (large, mid, small) by style (value, blend, growth).
INVESTMENT_STYLES = {
    "1": "Large Value",
    "2": "Large Blend",
    "3": "Large Growth",
    "4": "Mid Value",
    "5": "Mid Blend",
    "6": "Mid Growth",
    "7": "Small Value",
    "8": "Small Blend",
    "9": "Small Growth",
}


def sal_api_key() -> str:
    return (os.environ.get(SAL_API_KEY_ENV_VAR) or "").strip() or DEFAULT_SAL_API_KEY


class MorningStarProvider(BaseProvider):
    """Figures of a share from the screener and the sal-service of Morningstar."""

    SCREENER_URL = "https://lt.morningstar.com/api/rest.svc/9vehuxllxs/security/screener"
    SAL_URL = "https://api-global.morningstar.com/sal-service/v1/stock/data/{endpoint}"
    PAGE_URL = "https://global.morningstar.com/fr/investissements/actions/{ms_id}/cours"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, identifier: str) -> Optional[FinancialMetrics]:
        """The figures of a share given by its ISIN, its ticker, its Morningstar
        identifier or its page.

        ``None`` when no Morningstar identifier is found. When the services cannot be
        read, the identifiers alone are returned.
        """
        identifier = (identifier or "").strip()
        if not identifier:
            logger.warning("No identifier given for the MorningStar search.")
            return None

        async with self._session() as client:
            ms_id = self.direct_id(identifier) or await self._search_id(client, identifier)
            if not ms_id:
                logger.warning("Could not find the MorningStar identifier of %s", identifier)
                return None

            overview, profile = await asyncio.gather(
                self._sal(
                    client, "equityOverview", ms_id, showAnalystRating="true", hideesg="true"
                ),
                self._sal(client, "companyProfile", ms_id),
            )

        metrics = FinancialMetrics(
            ticker=identifier, provider_url=self.PAGE_URL.format(ms_id=ms_id)
        )
        if _ISIN.fullmatch(identifier.upper()):
            metrics.isin = identifier.upper()
        metrics.morningstar_id = ms_id
        if overview:
            self.read_overview(metrics, overview)
        if profile:
            self.read_profile(metrics, profile)
        return metrics

    @staticmethod
    def direct_id(identifier: str) -> Optional[str]:
        """The Morningstar identifier written in the identifier itself, or in its URL."""
        match = _MS_ID.search(identifier)
        return match.group(1) if match else None

    # ── Search ────────────────────────────────────────────────────────────────

    async def _search_id(self, client: ProviderSession, identifier: str) -> Optional[str]:
        """The identifier of the listing found by the screener for an ISIN or a ticker."""
        params = {
            "page": "1",
            "pageSize": "10",
            "outputType": "json",
            "version": "1",
            "securityDataPoints": "SecId|Name|Ticker|ISIN|ExchangeId|CurrencyId",
            "term": identifier,
        }
        try:
            resp = await client.get(self.SCREENER_URL, params=params)
            if resp.status_code != 200:
                logger.warning("MorningStar screener: HTTP %s for %s", resp.status_code, identifier)
                return None
            return self.select_id(resp.json().get("rows") or [], identifier)
        except Exception as e:
            logger.warning("MorningStar search error for %s: %s", identifier, e)
        return None

    @staticmethod
    def select_id(rows: list[dict], identifier: str) -> Optional[str]:
        """The row that is the identifier asked, never only the first one.

        An ISIN must be the ISIN of the row; a ticker (``TSLA``, ``AIR.PA``) its
        ticker, without the Yahoo suffix.
        """
        wanted = identifier.strip().upper()
        if _ISIN.fullmatch(wanted):
            key = "ISIN"
        else:
            key, wanted = "Ticker", wanted.split(".")[0]
        for row in rows:
            if str(row.get(key) or "").upper() == wanted and _MS_ID.fullmatch(
                str(row.get("SecId") or "")
            ):
                return row["SecId"]
        return None

    # ── Services ──────────────────────────────────────────────────────────────

    async def _sal(
        self, client: ProviderSession, endpoint: str, ms_id: str, **extra: str
    ) -> Optional[dict]:
        """One answer of the sal-service, or ``None`` (logged) when it cannot be read."""
        params = {
            "shareClassId": ms_id,
            **extra,
            "locale": "fr",
            "clientId": "INTLCOM",
            "benchmarkId": "null",
            "version": SAL_VERSION,
        }
        try:
            resp = await client.get(
                self.SAL_URL.format(endpoint=endpoint),
                params=params,
                headers={"ApiKey": sal_api_key()},
            )
            if resp.status_code != 200:
                logger.warning(
                    "MorningStar %s of %s: HTTP %s%s",
                    endpoint,
                    ms_id,
                    resp.status_code,
                    f" (check {SAL_API_KEY_ENV_VAR})" if resp.status_code in (401, 403) else "",
                )
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("MorningStar %s of %s not read: %s", endpoint, ms_id, e)
        return None

    @staticmethod
    def read_overview(metrics: FinancialMetrics, data: dict) -> None:
        """Price, valuation, ranges and style of a listing (``equityOverview``)."""
        fundamentals = data.get("morningstarFundamentals") or {}
        _put(metrics, "name", _text(data.get("securityName")))
        _put(metrics, "price", _number(fundamentals.get("lastClose")))
        _put(
            metrics,
            "currency",
            _text(data.get("tradingCurrency")) or _text(fundamentals.get("lastCloseCurrency")),
        )
        for field, key in (
            ("market_cap", "marketCap"),
            ("volume", "volume"),
            ("average_volume", "avgVolume"),
            ("week_52_low", "yearRangeLow"),
            ("week_52_high", "yearRangeHigh"),
            ("ps_ratio", "priceSale"),
            ("pb_ratio", "priceBook"),
            ("pe_ratio", "priceEarnings"),
            ("dividend_yield", "dividendYield"),
            ("dividend_yield_forward", "forwardDivYield"),
        ):
            _put(metrics, field, _number(data.get(key)))
        _put(metrics, "price_to_fair_value", _number(fundamentals.get("priceFairValue")))
        stars = _number(data.get("starRating"))
        if stars is not None and stars == int(stars):
            _put(metrics, "morningstar_rating", str(int(stars)))
        _put(metrics, "investment_style", INVESTMENT_STYLES.get(str(data.get("investmentStyle"))))
        _put(metrics, "sector", _text(data.get("sector")))
        _put(metrics, "industry", _text(data.get("industry")))

    @staticmethod
    def read_profile(metrics: FinancialMetrics, data: dict) -> None:
        """Sector, industry, staff, website and country of a company (``companyProfile``)."""
        sections = data.get("sections") or {}

        def value(section: str, key: str = "value") -> Any:
            return (sections.get(section) or {}).get(key)

        _put(metrics, "sector", _text(value("sector")))
        _put(metrics, "industry", _text(value("industry")))
        staff = _number(value("totalEmployees"))
        if staff is not None and staff == int(staff):
            _put(metrics, "employees", int(staff))
        _put(metrics, "website", _text(value("contact", "url")))
        _put(metrics, "country", _text(value("contact", "country")))


def _put(metrics: FinancialMetrics, field: str, value: Any) -> None:
    """Set a field once: the first source that gives it wins."""
    if value is not None and getattr(metrics, field, None) is None:
        setattr(metrics, field, value)


def _text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text if text and text != _PREMIUM_ONLY else None


def _number(value: Any) -> Optional[float]:
    """A number of the JSON (given as a number or as a text such as ``"407.46"``)."""
    if value == _PREMIUM_ONLY:
        return None
    return parse_number(value, decimal=".")
