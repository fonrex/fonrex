import json
import logging
import os
import random
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.numbers import find_number, parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

BARRONS_TOKEN_ENV_VAR = "BARRONS_TOKEN"
# Public entitlementToken embedded in Barron's market-data scripts/pages and used
# by the browser to call api.barrons.com (autocomplete search and Dylan quotes).
DEFAULT_BARRONS_TOKEN = "57494d5ed7ad44af85bc59a51dd87c90"

_ENTITLEMENT_TOKEN_RE = re.compile(
    r"(?:dylanEntitlementToken|DYLAN2010_ENTITLEMENT_TOKEN|entitlementTrackingToken|"
    r"dylan2010\.entitlementtoken|dylan-entitlement-token|entitlement_token|entitlementToken|"
    r"MICHELANGELO_ENTITLEMENT)"
    r'["\']?\s*[:=]\s*["\']([0-9a-fA-F]{32})["\']',
    re.IGNORECASE,
)
_BARRONS_URL_RE = re.compile(
    r"barrons\.com/market-data/(?:stocks|funds|etfs)/([^/?#]+)", re.IGNORECASE
)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# Yahoo suffix -> Barron's / Dow Jones country prefix
SUFFIX_TO_BARRONS_COUNTRY = {
    ".PA": "FR",
    ".AS": "NL",
    ".BR": "BE",
    ".LS": "PT",
    ".DE": "XE",
    ".F": "XE",
    ".L": "UK",
    ".MI": "IT",
    ".MC": "ES",
    ".ST": "SE",
    ".HE": "FI",
    ".CO": "DK",
    ".OL": "NO",
    ".VI": "AT",
    ".SW": "CH",
    ".TO": "CA",
}

ISIN_PREFIX_TO_BARRONS_COUNTRY = {
    "US": "US",
    "FR": "FR",
    "DE": "XE",
    "GB": "UK",
    "NL": "NL",
    "BE": "BE",
    "PT": "PT",
    "IT": "IT",
    "ES": "ES",
    "SE": "SE",
    "FI": "FI",
    "DK": "DK",
    "NO": "NO",
    "AT": "AT",
    "CH": "CH",
    "CA": "CA",
}

EUROPEAN_BARRONS_COUNTRIES = frozenset(
    {"FR", "NL", "XE", "UK", "BE", "PT", "IT", "ES", "SE", "FI", "DK", "NO", "AT", "CH"}
)


class BarronsProvider(BaseProvider):
    """
    Barrons Provider using httpx and selectolax, with Dylan JSON API fallback.
    """

    SEARCH_API = "https://api.barrons.com/api/autocomplete/search"
    DYLAN_QUOTE_API = "https://api.barrons.com/api/dylan/quotes/v2/comp/quoteByDialect"
    TOKEN_SCRIPT_URL = "https://www.barrons.com/asset/dj-watchlist/v1.27.0/p-88b00271.entry.js"
    TOKEN = os.getenv(BARRONS_TOKEN_ENV_VAR)
    _dynamic_token: Optional[str] = None
    BASE_URL = "https://www.barrons.com/market-data/stocks/{ticker}"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    @classmethod
    def extract_entitlement_token(cls, text: str) -> Optional[str]:
        """Extract the 32-hex Dylan entitlement token from Barron's HTML or JS and cache it."""
        if not text:
            return None
        match = _ENTITLEMENT_TOKEN_RE.search(text)
        if match:
            token = match.group(1).lower()
            cls._dynamic_token = token
            return token
        return None

    def _entitlement_token(self) -> str:
        """Return the entitlement token: env override -> dynamically extracted -> default."""
        configured = (self.TOKEN or os.getenv(BARRONS_TOKEN_ENV_VAR) or "").strip()
        if configured:
            return configured
        if self._dynamic_token:
            return self._dynamic_token
        return DEFAULT_BARRONS_TOKEN

    async def _refresh_entitlement_token(
        self, client: ProviderSession, url: Optional[str] = None
    ) -> Optional[str]:
        """Fetch a public Barron's script or page to dynamically refresh the entitlement token."""
        target = url or self.TOKEN_SCRIPT_URL
        try:
            resp = await client.get(target, headers=self._page_headers)
            if resp.status_code == 200:
                return self.extract_entitlement_token(resp.text)
        except Exception as e:
            logger.debug("Barrons token refresh error: %s", e)
        return None

    @staticmethod
    def _page_headers() -> dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.barrons.com/",
            "DNT": "1",
            "Connection": "keep-alive",
        }

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        async with self._session() as client:
            try:
                search_result = await self._search_result(client, ticker)
                if not search_result:
                    return None

                metrics = self._metrics_from_search_result(search_result, ticker)

                html = await self._fetch_page(client, search_result["provider_url"])
                if html:
                    page_metrics = self._parse_page(html, search_result.get("url_ticker") or ticker)
                    for field, value in page_metrics.model_dump(exclude_none=True).items():
                        setattr(metrics, field, value)

                charting_symbol = search_result.get("charting_symbol")
                if (
                    charting_symbol
                    and metrics.pe_ratio is None
                    and metrics.eps is None
                    and getattr(metrics, "price", None) is None
                ):
                    dylan = await self._fetch_dylan_quote(client, charting_symbol)
                    if dylan:
                        self.read_dylan_quote(metrics, dylan)

                return metrics
            except Exception as e:
                logger.error(f"Barrons error {ticker}: {e}")
                return None

    @staticmethod
    def _normalize_query(ticker: str) -> tuple[str, Optional[str]]:
        """Normalize a ticker, Yahoo symbol, ISIN or Barron's URL into (search_query, country)."""
        raw = (ticker or "").strip()
        if not raw:
            return "", None

        url_match = _BARRONS_URL_RE.search(raw)
        if url_match:
            sym = url_match.group(1).upper()
            qs = parse_qs(urlparse(raw).query)
            cc = (qs.get("countrycode") or [None])[0]
            if cc and cc.upper() != "US":
                country = "XE" if cc.upper() == "DE" else cc.upper()
                return f"{country}:{sym}", country
            return sym, "US"

        upper = raw.upper()
        if ":" in upper:
            prefix, _, sym = upper.partition(":")
            if prefix and sym:
                country = "XE" if prefix == "DE" else prefix
                return f"{country}:{sym}", country

        for suffix, country in SUFFIX_TO_BARRONS_COUNTRY.items():
            if upper.endswith(suffix):
                base = upper[: -len(suffix)]
                if base:
                    return f"{country}:{base}", country

        return raw, None

    async def _search_result(self, client: ProviderSession, ticker: str) -> Optional[dict]:
        search_query, _ = self._normalize_query(ticker)
        if not search_query:
            return None

        token = self._entitlement_token()
        params = {
            "q": search_query,
            "it": "stock,etf,fund",
            "c": 10,
            "entitlementToken": token,
        }
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json",
            "Referer": "https://www.barrons.com/",
            "Origin": "https://www.barrons.com",
            "dylan2010.entitlementtoken": token,
        }
        try:
            response = await client.get(self.SEARCH_API, params=params, headers=headers)
            if (
                response.status_code == 400
                and not (self.TOKEN or os.getenv(BARRONS_TOKEN_ENV_VAR) or "").strip()
            ):
                refreshed = await self._refresh_entitlement_token(client)
                if refreshed and refreshed != token:
                    params["entitlementToken"] = refreshed
                    headers["dylan2010.entitlementtoken"] = refreshed
                    response = await client.get(self.SEARCH_API, params=params, headers=headers)

            if response.status_code == 200:
                data = response.json()
                symbol = self._select_symbol(data, ticker)
                if symbol:
                    return self._search_result_from_symbol(symbol, ticker)
            else:
                logger.warning(f"Barrons search API status {response.status_code} for {ticker}")
        except Exception as e:
            logger.warning(f"Barrons search API error: {e}")
        return None

    async def _search_symbol(self, client: ProviderSession, ticker: str):
        search_result = await self._search_result(client, ticker)
        if search_result:
            return search_result.get("url_ticker") or search_result.get(
                "ticker"
            ) or ticker, search_result.get("country", "US")
        return ticker, "US"

    @classmethod
    def _select_symbol(cls, data: dict, query: str) -> Optional[dict]:
        symbols = data.get("symbols") or []
        if not symbols:
            return None

        normalized_query, explicit_country = cls._normalize_query(query)
        query_upper = (normalized_query or query or "").strip().upper()
        bare_query = query_upper.split(":")[-1]
        query_isin = len(query_upper) == 12 and query_upper.isalnum() and query_upper[:2].isalpha()

        isin_country = ISIN_PREFIX_TO_BARRONS_COUNTRY.get(query_upper[:2]) if query_isin else None
        available_countries = {(s.get("country") or "").upper() for s in symbols}

        def country_penalty(country: str) -> int:
            if explicit_country:
                return 0 if country == explicit_country else 1
            if query_isin:
                if isin_country and isin_country in available_countries:
                    return 0 if country == isin_country else 1
                if isin_country in EUROPEAN_BARRONS_COUNTRIES:
                    return 0 if country in EUROPEAN_BARRONS_COUNTRIES else 1
            return 0 if country == "US" else 1

        def rank(symbol):
            ticker = (symbol.get("ticker") or "").upper()
            url_ticker = ticker.split(":")[-1]
            isin = (symbol.get("isin") or "").upper()
            country = (symbol.get("country") or "").upper()
            exchange = (symbol.get("exchangeIsoCode") or "").upper()
            symbol_type = (symbol.get("type") or "").lower()
            score = symbol.get("score") or 0
            return (
                query_isin and isin != query_upper,
                not query_isin and url_ticker != bare_query and ticker != query_upper,
                exchange == "OOTC",
                country_penalty(country),
                symbol_type != "stock",
                -score,
            )

        return sorted(symbols, key=rank)[0]

    @classmethod
    def _search_result_from_symbol(cls, symbol: dict, query: str) -> dict:
        ticker = symbol.get("ticker") or query
        url_ticker = ticker.split(":")[-1]
        country = symbol.get("country") or "US"
        provider_url = cls.BASE_URL.format(ticker=url_ticker.lower())
        if country and country.lower() != "us":
            provider_url += f"?countrycode={country.lower()}"

        return {
            "ticker": ticker,
            "url_ticker": url_ticker,
            "charting_symbol": symbol.get("chartingSymbol") or None,
            "country": country,
            "exchange": symbol.get("exchangeIsoCode"),
            "exchange_name": symbol.get("exchange"),
            "isin": symbol.get("isin"),
            "name": symbol.get("company"),
            "instrument_type": symbol.get("type"),
            "provider_url": provider_url,
        }

    @staticmethod
    def _metrics_from_search_result(search_result: dict, ticker_input: str) -> FinancialMetrics:
        return FinancialMetrics(
            name=search_result.get("name"),
            ticker=search_result.get("url_ticker") or search_result.get("ticker") or ticker_input,
            isin=search_result.get("isin"),
            provider_url=search_result.get("provider_url"),
            exchange=search_result.get("exchange"),
            exchange_name=search_result.get("exchange_name"),
            instrument_type=search_result.get("instrument_type"),
        )

    async def _fetch_page(self, client: ProviderSession, url: str) -> Optional[HTMLParser]:
        try:
            # A new User-Agent is drawn at each attempt of the shared retry policy.
            resp = await client.get(url, headers=self._page_headers)
            if resp.status_code == 200:
                self.extract_entitlement_token(resp.text)
                return HTMLParser(resp.text)
            logger.warning(f"Barrons page fetch status {resp.status_code} for {url}")
        except Exception as e:
            logger.warning(f"Barrons page fetch error: {e}")
        return None

    async def _fetch_dylan_quote(
        self, client: ProviderSession, charting_symbol: str
    ) -> Optional[dict]:
        token = self._entitlement_token()
        params = {
            "id": charting_symbol,
            "dialect": "charting",
            "needed": (
                "CompositeTrading|BluegrassChannels|CurrencySpecific|Meta|"
                "TradingStatistics|Financials|Trading|IndustryClassification|CompanyInfo"
            ),
            "MaxInstrumentMatches": "1",
            "ckey": token[:10],
            "EntitlementToken": token,
        }
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json",
            "Referer": "https://www.barrons.com/",
            "Origin": "https://www.barrons.com",
            "dylan2010.entitlementtoken": token,
        }
        try:
            resp = await client.get(self.DYLAN_QUOTE_API, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, dict) else None
            logger.warning(
                "Barrons Dylan quote status %s for %s", resp.status_code, charting_symbol
            )
        except Exception as e:
            logger.warning("Barrons Dylan quote error for %s: %s", charting_symbol, e)
        return None

    @classmethod
    def read_dylan_quote(cls, metrics: FinancialMetrics, data: dict) -> None:
        """Populate missing fields on metrics from a Dylan quoteByDialect JSON payload."""
        responses = data.get("InstrumentResponses") or []
        if not responses:
            return
        matches = (responses[0] or {}).get("Matches") or []
        if not matches:
            return
        cls.read_dylan_match(metrics, matches[0] or {})

    @staticmethod
    def read_dylan_match(metrics: FinancialMetrics, match: dict) -> None:
        """Populate missing fields on metrics from a single Dylan Instrument match object."""
        if not isinstance(match, dict) or not match:
            return
        instrument = match.get("Instrument") or {}
        exchange = instrument.get("Exchange") or {}
        financials = match.get("Financials") or {}
        trading = match.get("Trading") or {}
        composite = match.get("CompositeTrading") or {}
        stats = match.get("TradingStatistics") or {}
        industry_cls = match.get("IndustryClassification") or {}
        company_info = match.get("CompanyInfo") or {}

        _put(metrics, "name", _text(instrument.get("CommonName")))
        _put(metrics, "isin", _text(instrument.get("Isin")))
        _put(metrics, "exchange", _text(exchange.get("IsoCode")))
        _put(metrics, "exchange_name", _text(exchange.get("CommonName")))

        price = (
            _nested_number(composite, "Last", "Price", "Value")
            or _nested_number(trading, "Last", "Price", "Value")
            or _nested_number(financials, "Previous", "Price", "Value")
        )
        _put(metrics, "price", price)

        currency = (
            _nested_text(composite, "Last", "Price", "Iso")
            or _nested_text(trading, "Last", "Price", "Iso")
            or _nested_text(financials, "MarketCapitalization", "Iso")
        )
        _put(metrics, "currency", currency)

        _put(metrics, "pe_ratio", _number(financials.get("PriceToEarningsRatio")))
        _put(metrics, "eps", _nested_number(financials, "LastEarningsPerShare", "Value"))
        _put(metrics, "dividend_yield", _number(financials.get("Yield")))
        _put(
            metrics,
            "dividend_rate",
            _number(financials.get("AnnualizedDividend"))
            or _nested_number(financials, "LastDividendPerShare", "Value"),
        )
        _put(metrics, "market_cap", _nested_number(financials, "MarketCapitalization", "Value"))
        _put(metrics, "shares_outstanding", _number(financials.get("SharesOutstanding")))
        _put(metrics, "float_shares", _number(financials.get("PublicFloat")))
        _put(metrics, "beta", _number(financials.get("Beta")))
        _put(metrics, "shares_short", _number(financials.get("ShortInterest")))
        _put(metrics, "short_percent_float", _number(financials.get("PercentOfFloatShorted")))

        _put(metrics, "sector", _nested_text(industry_cls, "Sector", "DisplayName"))
        _put(metrics, "industry", _nested_text(industry_cls, "Industry", "DisplayName"))
        _put(metrics, "description", _text(company_info.get("BusinessDescriptionShort")))
        _put(metrics, "website", _text(company_info.get("Url")))

        for stat in stats.get("PriceStatistics") or []:
            duration = ((stat.get("Range") or {}).get("Duration") or {}).get("ValueString")
            if duration == "P364D":
                _put(metrics, "week_52_low", _nested_number(stat, "Minimum", "Value"))
                _put(metrics, "week_52_high", _nested_number(stat, "Maximum", "Value"))
                break

        for stat in stats.get("VolumeStatistics") or []:
            duration = ((stat.get("Range") or {}).get("Duration") or {}).get("ValueString")
            if duration == "P3M":
                _put(metrics, "average_volume", _number(stat.get("Average")))
                break

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)

        # Name
        h1 = parser.css_first("h1")
        if h1:
            metrics.name = h1.text(strip=True)

        # Meta price and currency from Barron's head tags when present
        price_meta = parser.css_first("meta[name='price']")
        if price_meta:
            metrics.price = parse_number(price_meta.attributes.get("content"), decimal=".")
        currency_meta = parser.css_first("meta[name='priceCurrency']")
        if currency_meta:
            curr = (currency_meta.attributes.get("content") or "").strip()
            if curr:
                metrics.currency = curr

        # P/E
        metrics.pe_ratio = self._find_value_by_label(
            parser, ["P/E Ratio (TTM)", "P/E Ratio", "Price/Earnings", "P/E"]
        )

        # Yield
        metrics.dividend_yield = self._find_value_by_label(parser, ["Yield", "Dividend Yield"])

        # EPS
        metrics.eps = self._find_value_by_label(parser, ["EPS (TTM)", "EPS", "Earnings Per Share"])

        # Additional Key Data fields
        metrics.dividend_rate = self._find_value_by_label(
            parser, ["Latest Dividend", "Annualized Dividend", "Dividend"]
        )
        metrics.market_cap = self._find_value_by_label(parser, ["Market Value", "Market Cap"])
        metrics.shares_outstanding = self._find_value_by_label(parser, ["Shares Outstanding"])
        metrics.float_shares = self._find_value_by_label(parser, ["Public Float"])
        metrics.beta = self._find_value_by_label(parser, ["Beta"])
        metrics.shares_short = self._find_value_by_label(parser, ["Short Interest"])
        metrics.short_percent_float = self._find_value_by_label(
            parser, ["Percent of Float", "% of Float Shorted", "Percent of Float Shorted"]
        )
        metrics.average_volume = self._find_value_by_label(parser, ["Average Volume", "65 Day Avg"])

        low_52, high_52 = self._find_range_by_label(
            parser, ["52 Wk Range", "52 Week Range", "Week Range"]
        )
        if low_52 is not None:
            metrics.week_52_low = low_52
        if high_52 is not None:
            metrics.week_52_high = high_52

        self._parse_next_data(parser, metrics)
        return metrics

    @classmethod
    def _parse_next_data(cls, parser: HTMLParser, metrics: FinancialMetrics) -> None:
        """Extract missing fields from Next.js `<script id="__NEXT_DATA__">` when present."""
        script = parser.css_first("script#__NEXT_DATA__")
        if not script:
            return
        try:
            payload = json.loads(script.text())
        except Exception:
            return
        props = ((payload if isinstance(payload, dict) else {}).get("props") or {}).get(
            "pageProps"
        ) or {}

        title_card = props.get("titleCard") or {}
        _put(metrics, "name", _text(title_card.get("companyName")))
        _put(metrics, "exchange_name", _text(title_card.get("exchange")))

        key_data_card = props.get("keyDataCard") or {}
        points = {
            str(item["key"]).strip().lower(): (item.get("value") or {}).get("primaryValue")
            for item in (key_data_card.get("dataPoints") or [])
            if isinstance(item, dict) and item.get("key")
        }
        if points:
            _put(metrics, "pe_ratio", _number(points.get("p/e ratio")))
            _put(metrics, "eps", _number(points.get("eps")))
            _put(metrics, "dividend_yield", _number(points.get("yield")))
            _put(metrics, "dividend_rate", _number(points.get("dividend")))
            _put(metrics, "market_cap", _number(points.get("market cap")))
            _put(metrics, "shares_outstanding", _number(points.get("shares outstanding")))
            _put(metrics, "float_shares", _number(points.get("public float")))
            _put(metrics, "beta", _number(points.get("beta")))
            _put(metrics, "shares_short", _number(points.get("short interest")))
            _put(
                metrics,
                "short_percent_float",
                _number(points.get("% of float shorted") or points.get("percent of float")),
            )
            _put(metrics, "average_volume", _number(points.get("average volume")))

            range_raw = points.get("52 week range")
            if isinstance(range_raw, str) and " - " in range_raw:
                left, _, right = range_raw.partition(" - ")
                _put(metrics, "week_52_low", _number(left))
                _put(metrics, "week_52_high", _number(right))

        year_range = props.get("yearRangeCard") or {}
        _put(metrics, "week_52_low", _nested_number(year_range, "low", "value"))
        _put(metrics, "week_52_high", _nested_number(year_range, "high", "value"))
        _put(metrics, "price", _nested_number(year_range, "dayCurrent", "value"))

        vol_card = props.get("volumeCard") or {}
        _put(
            metrics,
            "average_volume",
            _nested_number(vol_card, "averageVolumeForPeriod", "value"),
        )

    def _find_range_by_label(
        self, parser: HTMLParser, labels: list[str]
    ) -> tuple[Optional[float], Optional[float]]:
        wanted = {label.lower() for label in labels}
        for div in parser.css("div"):
            ps = div.css("p")
            if len(ps) >= 2:
                label_text = ps[0].text(strip=True).lower()
                if label_text in wanted or any(label in label_text for label in wanted):
                    raw = ps[1].text(strip=True)
                    if " - " in raw:
                        left, _, right = raw.partition(" - ")
                        low = parse_number(left, decimal=".")
                        high = parse_number(right, decimal=".")
                        if low is not None and high is not None:
                            return low, high
        return None, None

    def _find_value_by_label(self, parser: HTMLParser, labels: list) -> Optional[float]:
        wanted = {label.lower() for label in labels}
        # 1. New DOM structure: div > p (label), p (value)
        for div in parser.css("div"):
            ps = div.css("p")
            if len(ps) >= 2:
                label_text = ps[0].text(strip=True).lower()
                if label_text in wanted:
                    raw = ps[1].text(strip=True)
                    parsed = parse_number(raw, decimal=".")
                    value = parsed if parsed is not None else find_number(raw)
                    if value is not None:
                        return value

        # 2. Generic finder fallback
        for node in parser.css("div, span, td, li"):
            txt = node.text(strip=True)
            if not txt or len(txt) >= 50:
                continue
            txt_lower = txt.lower()
            for label in labels:
                label_lower = label.lower()
                if label_lower in txt_lower:
                    # The text is "P/E Ratio: 12.5", or the value is in the next node.
                    if ":" in txt and txt_lower.split(":", 1)[0].strip() == label_lower:
                        value = find_number(txt, last=True)
                        if value is not None:
                            return value
                    elif txt_lower == label_lower and node.next:
                        sibling_txt = node.next.text(strip=True)
                        parsed = parse_number(sibling_txt, decimal=".")
                        value = parsed if parsed is not None else find_number(sibling_txt)
                        if value is not None:
                            return value
        return None


def _put(metrics: FinancialMetrics, field: str, value: Any) -> None:
    if value is not None and getattr(metrics, field, None) is None:
        setattr(metrics, field, value)


def _text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _number(value: Any) -> Optional[float]:
    return parse_number(value, decimal=".")


def _nested_number(data: dict, *keys: str) -> Optional[float]:
    curr: Any = data
    for key in keys:
        if not isinstance(curr, dict):
            return None
        curr = curr.get(key)
    return _number(curr)


def _nested_text(data: dict, *keys: str) -> Optional[str]:
    curr: Any = data
    for key in keys:
        if not isinstance(curr, dict):
            return None
        curr = curr.get(key)
    return _text(curr)
