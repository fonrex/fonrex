"""MsnProvider — the figures of a share from the JSON services of MSN Finance.

The quote page of msn.com (``/finance/details-de-l-action/fi-<id>``) is a JavaScript
application: its server-rendered HTML holds the name and the price, never the
fundamentals. In the browser, its components fetch them from a JSON service, which
answers a script directly; the page is therefore never read:

* the autosuggest of ``services.bingapis.com`` turns a ticker into the MSN
  identifier of a listing (``ae7bur`` for Airbus in Paris);
* ``assets.msn.com/service/Finance/QuoteSummary`` gives the quote of that listing
  (price, valuation, yield, ranges, market capitalisation) and its key metrics
  (EPS, payout, margins, returns, debt, latest revenue and net income).

The ``apikey`` parameter is a public constant of those components, written in their
JavaScript; ``MSN_FINANCE_API_KEY`` replaces it if MSN changes it (the service then
answers HTTP 401). The yield, the payout ratio, the net margin and the returns are
percentages (``1.599`` for 1.60 %); they are declared in ``monitoring/units.py``.
"""

import json
import logging
import os
from typing import Any, Optional

from financials.models import FinancialMetrics
from financials.numbers import parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

API_KEY_ENV_VAR = "MSN_FINANCE_API_KEY"
# The public key of the finance components of msn.com (bundles/v1/finance/latest).
DEFAULT_API_KEY = "0QfOX3Vn51YCzitbLaRkTTBadtWpgTN8NZLW0C1SEM"

# FinancialMetrics field -> key of the "quote" object of QuoteSummary.
QUOTE_FIELDS = (
    ("price", "price"),
    ("pe_ratio", "peRatio"),
    ("dividend_yield", "yieldPercent"),
    ("market_cap", "marketCap"),
    ("week_52_high", "price52wHigh"),
    ("week_52_low", "price52wLow"),
    ("volume", "accumulatedVolume"),
    ("average_volume", "averageVolume"),
)
# FinancialMetrics field -> key of "equity.analysis.keyMetrics".
KEY_METRIC_FIELDS = (
    ("eps", "eps"),
    ("payout_ratio", "payoutRatio"),
    ("debt_to_equity", "debtToEquityRatio"),
    ("pb_ratio", "priceToBookRatio"),
    ("revenue", "latestRevenue"),
    ("net_income", "latestIncome"),
    ("profit_margin", "latestNetProfitMargin"),
    ("roe", "returnOnEquity"),
    ("roa", "returnOnAssets"),
    ("roic", "returnOnCapital"),
)
# Ratios that cannot be zero: MSN writes 0 when it has no value.
_ZERO_MEANS_MISSING = {"pe_ratio", "pb_ratio"}


def api_key() -> str:
    return (os.environ.get(API_KEY_ENV_VAR) or "").strip() or DEFAULT_API_KEY


class MsnProvider(BaseProvider):
    """Figures of a share from the autosuggest of Bing and the QuoteSummary of MSN."""

    SEARCH_API = "https://services.bingapis.com/contentservices-finance.csautosuggest/api/v1/Query"
    QUOTE_SUMMARY_API = "https://assets.msn.com/service/Finance/QuoteSummary"
    BASE_URL = "https://www.msn.com/fr-ca/finances/details-de-l-action/{slug}/fi-{id}?id={id}"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """The figures of a share, or ``None`` when MSN does not know it or does not answer."""
        async with self._session() as client:
            search_data = await self._search_id(client, ticker)
            if not search_data:
                return None
            summary = await self._quote_summary(client, search_data["id"])

        if not summary:
            return None
        metrics = FinancialMetrics(
            ticker=ticker,
            provider_url=self.BASE_URL.format(slug=search_data["slug"], id=search_data["id"]),
        )
        self.read_quote_summary(metrics, summary)
        return metrics

    async def _search_id(self, client: ProviderSession, query: str) -> Optional[dict]:
        # Query params: query=..., count=5
        try:
            resp = await client.get(
                self.SEARCH_API, params={"query": query, "count": 5, "market": "fr-ca"}
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("data") and data["data"].get("stocks"):
                    item = data["data"]["stocks"][0]

                    if isinstance(item, str):
                        try:
                            item = json.loads(item)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    if isinstance(item, dict):
                        sec_id = item.get("SecId") or item.get("SecID")
                        symbol = item.get("OS001Index", "").lower()
                        country = item.get("RT0EC", "").lower()
                        slug = (
                            f"{symbol}-{country}-stock"
                            if symbol and country
                            else f"{query.lower()}-stock"
                        )
                        if sec_id:
                            return {"id": sec_id, "slug": slug}
        except Exception as e:
            logger.warning(f"Msn search error: {e}")
        return None

    async def _quote_summary(self, client: ProviderSession, sec_id: str) -> Optional[dict]:
        """The QuoteSummary of a listing, or ``None`` when it holds no quote."""
        params = {
            "apikey": api_key(),
            "ids": sec_id,
            "intents": "Quotes,QuoteDetails",
            "wrapodata": "false",
        }
        try:
            resp = await client.get(self.QUOTE_SUMMARY_API, params=params)
            if resp.status_code != 200:
                logger.warning("Msn QuoteSummary: HTTP %s for %s", resp.status_code, sec_id)
                return None
            data = resp.json()
        except Exception as e:
            logger.warning("Msn QuoteSummary of %s not read: %s", sec_id, e)
            return None
        # A list with one entry per identifier; an unknown identifier has no "quote".
        entry = data[0] if isinstance(data, list) and data else None
        if not isinstance(entry, dict) or not isinstance(entry.get("quote"), dict):
            logger.warning("Msn QuoteSummary: no quote for %s", sec_id)
            return None
        return entry

    @staticmethod
    def read_quote_summary(metrics: FinancialMetrics, data: dict) -> None:
        """Name, quote and key metrics of a listing (one entry of ``QuoteSummary``)."""
        quote = _object(data.get("quote"))
        equity = _object(data.get("equity"))
        key_metrics = _object(_object(equity.get("analysis")).get("keyMetrics"))
        company = _object(equity.get("company"))

        _put(metrics, "name", _text(quote.get("displayName")) or _text(equity.get("displayName")))
        _put(metrics, "currency", _text(quote.get("currency")))
        for field, key in QUOTE_FIELDS:
            _put(metrics, field, _number(field, quote.get(key)))
        for field, key in KEY_METRIC_FIELDS:
            _put(metrics, field, _number(field, key_metrics.get(key)))
        _put(metrics, "beta", _number("beta", equity.get("beta")))
        _put(metrics, "sector", _text(company.get("sector")))
        _put(metrics, "industry", _text(company.get("industry")))


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _put(metrics: FinancialMetrics, field: str, value: Any) -> None:
    if value is not None:
        setattr(metrics, field, value)


def _text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _number(field: str, value: Any) -> Optional[float]:
    """A number of the JSON; a zero that stands for a missing ratio is dropped."""
    number = parse_number(value, decimal=".")
    if number == 0 and field in _ZERO_MEANS_MISSING:
        return None
    return number
