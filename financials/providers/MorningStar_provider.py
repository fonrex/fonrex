"""MorningStarProvider — the figures of a share from the JSON services of Morningstar.

The quote page of global.morningstar.com is filled in the browser by JavaScript
components, and answers a script with an AWS WAF challenge (HTTP 202): it is never
read. Its components call two JSON services, which answer a script directly:

* the screener of ``lt.morningstar.com`` turns an ISIN (or a ticker) into the
  Morningstar identifier of a listing (``0P0000OQN8`` for Tesla);
* ``api-global.morningstar.com/sal-service`` gives the overview of that listing
  (price, valuation, ranges, style), the profile of its company (sector, staff) and
  its trading information (beta, shares, short interest).

The ``ApiKey`` and ``X-API-REALTIME-E`` headers are public constants of those
components, written in their JavaScript; ``MORNINGSTAR_SAL_API_KEY`` and
``MORNINGSTAR_SAL_REALTIME_TOKEN`` replace them if Morningstar changes them. The
services are not documented: a failure of one of them leaves its fields empty.
The dividend yields are ratios (``0.055754`` for 5.58 %); the short percentage of the
float is a percentage (``1.9611``), the shares outstanding and the float millions.
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
REALTIME_TOKEN_ENV_VAR = "MORNINGSTAR_SAL_REALTIME_TOKEN"
# The public "tokenRealtime" of the same components, sent by the trading information.
DEFAULT_SAL_REALTIME_TOKEN = (
    "eyJlbmMiOiJBMTI4R0NNIiwiYWxnIjoiUlNBLU9BRVAifQ."
    "X-h4zn65XpjG8cZnL3e6hj8LMbzupQBglHZce7tzu-c4utCtXQ2IYoLxdik04usYRhNo74AS_2crdjLnBc_J0l"
    "FEdAPzb_OBE7HjwfRaYeNhfXIDw74QCrFGqQ5n7AtllL-vTGnqmI1S9WJhSwnIBe_yRxuXGGbIttizI5FItYY."
    "bB3WkiuoS1xzw78w."
    "iTqTFVbxKo4NQQsNNlbkF4tg4GCfgqdRdQXN8zQU3QYhbHc-XDusH1jFii3-_-AIsqpHaP7ilG9aBxzoK7KPPfK3"
    "apcoMS6fDM3QLRSZzjkBoxWK75FtrQMAN5-LecdJk97xaXEciS0QqqBqNugoSPwoiZMazHX3rr7L5jPM-ecXN2uE"
    "jbSR0wfg-57iHAku8jvThz4mtGpMRAOil9iZaL6iRQ."
    "o6tR6kuOQBhnpcsdTQeZWw"
)
SAL_VERSION = "5.21.0"

# The screener returns funds before shares for a short ticker ("BNP"): enough rows.
SCREENER_PAGE_SIZE = 50

# Yahoo suffix -> exchanges (MIC, as in "EX$$$$XPAR") and currencies of that place.
# A bare ticker is not an identity: "AIR" is Airbus in Paris and AAR Corp in New York.
PLACES = {
    "PA": ({"XPAR"}, {"EUR"}),
    "AS": ({"XAMS"}, {"EUR"}),
    "BR": ({"XBRU"}, {"EUR"}),
    "LS": ({"XLIS"}, {"EUR"}),
    "IR": ({"XDUB"}, {"EUR"}),
    "DE": ({"XETR"}, {"EUR"}),
    "F": ({"XFRA"}, {"EUR"}),
    "MI": ({"XMIL"}, {"EUR"}),
    "MC": ({"XMAD"}, {"EUR"}),
    "HE": ({"XHEL"}, {"EUR"}),
    "VI": ({"XWBO"}, {"EUR"}),
    "L": ({"XLON"}, {"GBP", "GBX"}),
    "SW": ({"XSWX"}, {"CHF"}),
    "ST": ({"XSTO"}, {"SEK"}),
    "CO": ({"XCSE"}, {"DKK"}),
    "OL": ({"XOSL"}, {"NOK"}),
    "TO": ({"XTSE"}, {"CAD"}),
    "T": ({"XTKS"}, {"JPY"}),
    "HK": ({"XHKG"}, {"HKD"}),
    "AX": ({"XASX"}, {"AUD"}),
}
# Exchanges of a ticker written without suffix (United States).
US_EXCHANGES = {"XNAS", "XNYS", "XASE", "ARCX", "BATS"}

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


def sal_realtime_token() -> str:
    return (os.environ.get(REALTIME_TOKEN_ENV_VAR) or "").strip() or DEFAULT_SAL_REALTIME_TOKEN


class MorningStarProvider(BaseProvider):
    """Figures of a share from the screener and the sal-service of Morningstar."""

    SCREENER_URL = "https://lt.morningstar.com/api/rest.svc/9vehuxllxs/security/screener"
    SAL_URL = "https://api-global.morningstar.com/sal-service/v1/stock/{path}"
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

            overview, profile, trading = await asyncio.gather(
                self._sal(
                    client,
                    "data/equityOverview",
                    ms_id,
                    {"shareClassId": ms_id, "showAnalystRating": "true", "hideesg": "true"},
                ),
                self._sal(client, "data/companyProfile", ms_id, {"shareClassId": ms_id}),
                self._sal(
                    client,
                    f"tradingInformation/{ms_id}/data",
                    ms_id,
                    {},
                    {"X-API-REALTIME-E": sal_realtime_token()},
                ),
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
        if trading:
            self.read_trading(metrics, trading)
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
            "pageSize": str(SCREENER_PAGE_SIZE),
            "outputType": "json",
            "version": "1",
            "securityDataPoints": "SecId|Name|Ticker|ISIN|ExchangeId|CurrencyId",
            # The screener knows "AIR", never the Yahoo ticker "AIR.PA"
            "term": _search_term(identifier),
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

        An ISIN must be the ISIN of the row. A ticker must be the ticker of the row,
        without its Yahoo suffix, on the place the suffix names (``AIR.PA``: Paris,
        never AAR Corp in New York); without suffix, a United States listing first.
        Funds (identifiers that do not start with ``0P``) are never taken.
        """
        wanted = identifier.strip().upper()
        shares = [row for row in rows if _MS_ID.fullmatch(str(row.get("SecId") or ""))]
        if _ISIN.fullmatch(wanted):
            matches = [row for row in shares if str(row.get("ISIN") or "").upper() == wanted]
            return matches[0]["SecId"] if matches else None

        symbol, place = _ticker_and_place(wanted)
        matches = [row for row in shares if str(row.get("Ticker") or "").upper() == symbol]
        if place is not None:
            exchanges, currencies = place
            matches = [
                row
                for row in matches
                if _code(row.get("ExchangeId")) in exchanges
                or (not _code(row.get("ExchangeId")) and _code(row.get("CurrencyId")) in currencies)
            ]
        else:
            matches.sort(key=lambda row: _code(row.get("ExchangeId")) not in US_EXCHANGES)
        return matches[0]["SecId"] if matches else None

    # ── Services ──────────────────────────────────────────────────────────────

    async def _sal(
        self,
        client: ProviderSession,
        path: str,
        ms_id: str,
        params: dict[str, str],
        headers: Optional[dict[str, str]] = None,
    ) -> Optional[dict]:
        """One answer of the sal-service, or ``None`` (logged) when it cannot be read."""
        query = {
            **params,
            "locale": "fr",
            "clientId": "INTLCOM",
            "benchmarkId": "null",
            "version": SAL_VERSION,
        }
        try:
            resp = await client.get(
                self.SAL_URL.format(path=path),
                params=query,
                headers={"ApiKey": sal_api_key(), **(headers or {})},
            )
            if resp.status_code != 200:
                logger.warning(
                    "MorningStar %s of %s: HTTP %s%s",
                    path,
                    ms_id,
                    resp.status_code,
                    " (check MORNINGSTAR_SAL_* in .env)" if resp.status_code in (401, 403) else "",
                )
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("MorningStar %s of %s not read: %s", path, ms_id, e)
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

    @staticmethod
    def read_trading(metrics: FinancialMetrics, data: dict) -> None:
        """Beta, shares and short interest of a listing (``tradingInformation``).

        The shares outstanding and the float are given in millions, the shares
        shorted as a count, the short percentage of the float as a percentage.
        """
        _put(metrics, "beta", _number((data.get("returnStatistics") or {}).get("beta")))
        for field, key in (
            ("shares_outstanding", "sharesOutstanding"),
            ("float_shares", "floatShares"),
        ):
            millions = _number(data.get(key))
            _put(metrics, field, millions * 1e6 if millions is not None else None)
        _put(metrics, "shares_short", _number(data.get("sharesShorted")))
        _put(metrics, "short_percent_float", _number(data.get("floatSharesShorted")))
        _put(metrics, "short_ratio", _number(data.get("daysToConver")))
        # Also in the overview: taken here only when the overview did not give them
        for field, key in (
            ("market_cap", "marketCap"),
            ("volume", "volume"),
            ("average_volume", "avgVolume"),
            ("week_52_low", "yearRangeLow"),
            ("week_52_high", "yearRangeHigh"),
        ):
            _put(metrics, field, _number(data.get(key)))
        _put(metrics, "currency", _text(data.get("tradingCurrency")))


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


def _code(value: Any) -> str:
    """The code of a screener identifier: "EX$$$$XPAR" -> "XPAR", "CU$$$$$EUR" -> "EUR"."""
    return str(value or "").rsplit("$", 1)[-1].upper()


def _ticker_and_place(ticker: str) -> tuple[str, Optional[tuple[set[str], set[str]]]]:
    """The bare symbol and the place of a Yahoo ticker: "AIR.PA" -> ("AIR", Paris).

    A suffix that names no known place (``BRK.B`` is a share class) stays in the symbol.
    """
    symbol, _, suffix = ticker.rpartition(".")
    if symbol and suffix in PLACES:
        return symbol, PLACES[suffix]
    return ticker, None


def _search_term(identifier: str) -> str:
    """What the screener is asked: an ISIN as it is, a ticker without its Yahoo suffix."""
    wanted = identifier.strip().upper()
    if _ISIN.fullmatch(wanted):
        return wanted
    return _ticker_and_place(wanted)[0]
