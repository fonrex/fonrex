import json
import logging
import os
import random
import re
from typing import Any, Optional

from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.numbers import find_number, parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

WSJ_TOKEN_ENV_VAR = "WSJ_TOKEN"
# Public entitlementToken embedded in WSJ market-data scripts and used by the browser
# to call api.wsj.net (autocomplete search and Dylan quotes).
DEFAULT_WSJ_TOKEN = "57494d5ed7ad44af85bc59a51dd87c90"

_ENTITLEMENT_TOKEN_RE = re.compile(
    r"(?:dylanEntitlementToken|DYLAN2010_ENTITLEMENT_TOKEN|entitlementTrackingToken|"
    r"dylan2010\.entitlementtoken|dylan-entitlement-token|entitlement_token|entitlementToken)"
    r'["\']?\s*[:=]\s*["\']([0-9a-fA-F]{32})["\']',
    re.IGNORECASE,
)
_WSJ_QUOTE_URL_RE = re.compile(
    r"wsj\.com/market-data/(?:quotes/(?:([A-Za-z]{2})/([A-Za-z0-9]+)/)?|stock/)([^/?#]+)",
    re.IGNORECASE,
)
_WINDOW_STATE_RE = re.compile(r"window\.__STATE__\s*=\s*(\{.*?\});?\s*(?:</script>|$)", re.DOTALL)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# Yahoo suffix -> WSJ country prefix
SUFFIX_TO_WSJ_COUNTRY = {
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

ISIN_PREFIX_TO_WSJ_COUNTRY = {
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

EUROPEAN_WSJ_COUNTRIES = frozenset(
    {"FR", "NL", "XE", "UK", "BE", "PT", "IT", "ES", "SE", "FI", "DK", "NO", "AT", "CH"}
)


class WallStreetJournalProvider(BaseProvider):
    """
    WSJ Provider using httpx and selectolax, with Dylan JSON API fallback.
    """

    SEARCH_API = "https://api.wsj.net/api/autocomplete/search"
    DYLAN_QUOTE_API = "https://api.wsj.net/api/dylan/quotes/v2/comp/quoteByDialect"
    TOKEN_SCRIPT_URL = "https://www.wsj.com/asset/ace/ace.min.js"
    TOKEN = os.getenv("WSJ_TOKEN")
    _dynamic_token: Optional[str] = None
    BASE_URL = "https://www.wsj.com/market-data/quotes/{country}/{exchange}/{ticker}"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    @classmethod
    def extract_entitlement_token(cls, text: str) -> Optional[str]:
        """Extract the 32-hex Dylan entitlement token from WSJ HTML or JS and cache it."""
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
        configured = (self.TOKEN or os.getenv(WSJ_TOKEN_ENV_VAR) or "").strip()
        if configured:
            return configured
        if self._dynamic_token:
            return self._dynamic_token
        return DEFAULT_WSJ_TOKEN

    async def _refresh_entitlement_token(
        self, client: ProviderSession, url: Optional[str] = None
    ) -> Optional[str]:
        """Fetch a public WSJ script or page to dynamically refresh the entitlement token."""
        target = url or self.TOKEN_SCRIPT_URL
        try:
            resp = await client.get(target, headers=self._page_headers)
            if resp.status_code == 200:
                return self.extract_entitlement_token(resp.text)
        except Exception as e:
            logger.debug("WSJ token refresh error: %s", e)
        return None

    @staticmethod
    def _page_headers() -> dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "Referer": "https://www.wsj.com/",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    @staticmethod
    def _people_params(search_result: dict) -> Optional[dict[str, str]]:
        """Build the module query parameters that allow `/company-people` through the edge."""
        url_ticker = (search_result.get("url_ticker") or "").upper()
        country = (search_result.get("country") or "").upper()
        exchange = (search_result.get("exchange") or "").upper()
        if not (url_ticker and country and exchange):
            return None
        inst_type = (search_result.get("instrument_type") or "STOCK").upper()
        quote_id = json.dumps(
            {
                "ticker": url_ticker,
                "countryCode": country,
                "exchange": exchange,
                "type": inst_type,
                "path": f"/{country}/{exchange}/{url_ticker}",
            },
            separators=(",", ":"),
        )
        return {"id": quote_id, "type": "quotes_company_profile"}

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        async with self._session() as client:
            try:
                search_result = await self._search_result(client, ticker)
                if not search_result:
                    return None

                metrics = self._metrics_from_search_result(search_result, ticker)

                # Fetch main quote page
                html = await self._fetch_page(client, search_result["provider_url"])
                if html:
                    page_metrics = self._parse_page(
                        html, search_result.get("url_ticker") or ticker
                    )
                    for field, value in page_metrics.model_dump(exclude_none=True).items():
                        setattr(metrics, field, value)

                # Fall back to the Dylan JSON API when the HTML page was blocked or incomplete
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

                # Fetch insider transactions page
                people_url = search_result["provider_url"] + "/company-people"
                html_people = await self._fetch_page(
                    client, people_url, params=self._people_params(search_result)
                )
                if html_people:
                    insider_data = self._parse_insider_transactions(html_people)
                    metrics.insider_transactions = insider_data

                return metrics
            except Exception as e:
                logger.error(f"WSJ error {ticker}: {e}")
                return None

    @staticmethod
    def _normalize_query(ticker: str) -> tuple[str, Optional[str], Optional[str]]:
        """Normalize a ticker, Yahoo symbol, ISIN or WSJ URL into (search_query, country, exchange)."""
        raw = (ticker or "").strip()
        if not raw:
            return "", None, None

        url_match = _WSJ_QUOTE_URL_RE.search(raw)
        if url_match:
            cc, exch, sym = url_match.group(1), url_match.group(2), url_match.group(3)
            sym_upper = sym.upper()
            exch_upper = exch.upper() if exch else None
            if cc and cc.upper() != "US":
                country = "XE" if cc.upper() == "DE" else cc.upper()
                return f"{country}:{sym_upper}", country, exch_upper
            return sym_upper, "US", exch_upper

        upper = raw.upper()
        if ":" in upper:
            prefix, _, sym = upper.partition(":")
            if prefix and sym:
                country = "XE" if prefix == "DE" else prefix
                return f"{country}:{sym}", country, None

        for suffix, country in SUFFIX_TO_WSJ_COUNTRY.items():
            if upper.endswith(suffix):
                base = upper[: -len(suffix)]
                if base:
                    return f"{country}:{base}", country, None

        return raw, None, None

    async def _search_result(self, client: ProviderSession, ticker: str) -> Optional[dict]:
        search_query, _, _ = self._normalize_query(ticker)
        if not search_query:
            return None

        token = self._entitlement_token()
        params = {
            "q": search_query,
            "it": "fund,exchangetradedfund,stock,Index,Currency,Benchmark,Future,Bond,CryptoCurrency",
            "c": "5",
            "t": "symbol,private-company,person,suggested-search-term,topic,omniture-keyword",
            "xe": "XBAH,XCNQ,XTNX,XCYS,XCAI,XSTU,XBER,XHAN,XTAE,XAMM,XKAZ,XKUW,XCAS,XMUS,XKAR,DSMD,XMIC,RTSX,XSAU,XBRA,XCOL,XADS,XDFM,XCAR,XMSTAR,XOSE",
        }
        headers = {
            "Host": "api.wsj.net",
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json",
            "Accept-Language": "fr,fr-FR;q=0.8,en-US;q=0.5,en;q=0.3",
            "Referer": "https://www.wsj.com/",
            "Origin": "https://www.wsj.com",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "dylan2010.entitlementtoken": token,
        }
        try:
            response = await client.get(self.SEARCH_API, params=params, headers=headers)
            if (
                response.status_code == 400
                and not (self.TOKEN or os.getenv(WSJ_TOKEN_ENV_VAR) or "").strip()
            ):
                refreshed = await self._refresh_entitlement_token(client)
                if refreshed and refreshed != token:
                    headers["dylan2010.entitlementtoken"] = refreshed
                    response = await client.get(self.SEARCH_API, params=params, headers=headers)

            if response.status_code == 200:
                data = response.json()
                symbol = self._select_symbol(data, ticker)
                if symbol:
                    return self._search_result_from_symbol(symbol, ticker)
            else:
                logger.warning(f"WSJ search API status {response.status_code} for {ticker}")
        except Exception as e:
            logger.warning(f"WSJ search API error: {e}")
        return None

    async def _search_symbol(self, client: ProviderSession, ticker: str):
        search_result = await self._search_result(client, ticker)
        if search_result:
            return (
                search_result.get("url_ticker") or search_result.get("ticker") or ticker,
                search_result.get("country", "US"),
                search_result.get("exchange", "XNYS"),
            )
        return ticker, "US", "XNYS"

    async def _fetch_page(
        self,
        client: ProviderSession,
        url: str,
        params: Optional[dict[str, str]] = None,
    ) -> Optional[HTMLParser]:
        try:
            resp = await client.get(url, params=params, headers=self._page_headers)
            if resp.status_code == 200:
                self.extract_entitlement_token(resp.text)
                return HTMLParser(resp.text)
            logger.warning(f"WSJ page fetch status {resp.status_code} for {url}")
        except Exception as e:
            logger.warning(f"WSJ fetch error: {e}")
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
            "Referer": "https://www.wsj.com/",
            "Origin": "https://www.wsj.com",
            "dylan2010.entitlementtoken": token,
        }
        try:
            resp = await client.get(self.DYLAN_QUOTE_API, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, dict) else None
            logger.warning("WSJ Dylan quote status %s for %s", resp.status_code, charting_symbol)
        except Exception as e:
            logger.warning("WSJ Dylan quote error for %s: %s", charting_symbol, e)
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

    @classmethod
    def _select_symbol(cls, data: dict, query: str) -> Optional[dict]:
        symbols = data.get("symbols") or []
        if not symbols:
            return None

        normalized_query, explicit_country, explicit_exchange = cls._normalize_query(query)
        query_upper = (normalized_query or query or "").strip().upper()
        bare_query = query_upper.split(":")[-1]
        query_isin = len(query_upper) == 12 and query_upper.isalnum() and query_upper[:2].isalpha()

        isin_country = ISIN_PREFIX_TO_WSJ_COUNTRY.get(query_upper[:2]) if query_isin else None
        available_countries = {(s.get("country") or "").upper() for s in symbols}

        def country_penalty(country: str) -> int:
            if explicit_country:
                return 0 if country == explicit_country else 1
            if query_isin:
                if isin_country and isin_country in available_countries:
                    return 0 if country == isin_country else 1
                if isin_country in EUROPEAN_WSJ_COUNTRIES:
                    return 0 if country in EUROPEAN_WSJ_COUNTRIES else 1
            # Prefer Europe when searching a bare ticker that has an FR version
            if not query_isin and country == "US" and "FR" in available_countries:
                return 1
            return 0

        def rank(symbol):
            ticker = (symbol.get("ticker") or "").upper()
            url_ticker = ticker.split(":")[-1]
            isin = (symbol.get("isin") or "").upper()
            country = (symbol.get("country") or "").upper()
            exchange = (symbol.get("exchangeIsoCode") or "").upper()
            symbol_type = (symbol.get("type") or "").lower()
            score = symbol.get("score") or 0

            # The lower, the better
            return (
                query_isin and isin != query_upper,
                not query_isin and url_ticker != bare_query and ticker != query_upper,
                bool(explicit_exchange and exchange and exchange != explicit_exchange),
                exchange == "OOTC",
                country_penalty(country),
                # We prefer stocks to funds
                symbol_type != "stock",
                -score,
            )

        return sorted(symbols, key=rank)[0]

    @classmethod
    def _search_result_from_symbol(cls, symbol: dict, query: str) -> dict:
        ticker = symbol.get("ticker") or query
        url_ticker = ticker.split(":")[-1]
        country = symbol.get("country") or "US"
        exchange = symbol.get("exchangeIsoCode") or "XNYS"

        charting_symbol = symbol.get("chartingSymbol", "")
        if charting_symbol and (
            charting_symbol.startswith("STOCK/") or charting_symbol.startswith("FUND/")
        ):
            parts = charting_symbol.split("/")
            if len(parts) >= 4:
                country = parts[1] or country
                exchange = parts[2] or exchange
                url_ticker = parts[3] or url_ticker

        provider_url = cls.BASE_URL.format(
            country=country.upper(),
            exchange=exchange.upper(),
            ticker=url_ticker.upper(),
        )

        return {
            "ticker": ticker,
            "url_ticker": url_ticker,
            "charting_symbol": charting_symbol or None,
            "country": country,
            "exchange": exchange,
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

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        metrics = FinancialMetrics(ticker=ticker_input)
        h1 = parser.css_first("h1")
        if h1:
            full_h1 = h1.text(strip=True)
            ticker_span = h1.css_first("[class*='ticker']")
            if ticker_span:
                suffix = ticker_span.text(strip=True)
                if suffix and full_h1.endswith(suffix):
                    full_h1 = full_h1[: -len(suffix)].strip()
            if full_h1:
                metrics.name = full_h1

        price_node = parser.css_first("#quote_val")
        if price_node:
            metrics.price = parse_number(price_node.text(strip=True), decimal=".")

        curr_node = parser.css_first("[class*='cr_sym']")
        if curr_node:
            curr_text = curr_node.text(strip=True)
            if curr_text:
                metrics.currency = curr_text

        metrics.pe_ratio = self._find_value_by_label(
            parser, ["P/E Ratio (TTM)", "P/E Ratio", "Price/Earnings", "P/E"]
        )
        metrics.dividend_yield = self._find_value_by_label(parser, ["Yield", "Dividend Yield"])
        metrics.eps = self._find_value_by_label(
            parser, ["EPS (TTM)", "EPS", "Earnings Per Share"]
        )
        metrics.dividend_rate = self._find_value_by_label(
            parser, ["Latest Dividend", "Annualized Dividend", "Dividend"]
        )
        metrics.market_cap = self._find_value_by_label(parser, ["Market Cap"])
        metrics.shares_outstanding = self._find_value_by_label(parser, ["Shares Outstanding"])
        metrics.float_shares = self._find_value_by_label(parser, ["Public Float"])
        metrics.beta = self._find_value_by_label(parser, ["Beta"])
        metrics.shares_short = self._find_value_by_label(parser, ["Short Interest"])
        metrics.short_percent_float = self._find_value_by_label(
            parser, ["% of Float Shorted", "Percent of Float Shorted"]
        )
        metrics.average_volume = self._find_value_by_label(
            parser, ["65 Day Avg Vol", "Average Volume", "Avg Vol"]
        )

        low_52, high_52 = self._find_range_by_label(parser, ["52 Week Range", "Week Range"])
        if low_52 is not None:
            metrics.week_52_low = low_52
        if high_52 is not None:
            metrics.week_52_high = high_52

        self._parse_window_state(parser, metrics)
        return metrics

    @classmethod
    def _parse_window_state(cls, parser: HTMLParser, metrics: FinancialMetrics) -> None:
        """Extract embedded Dylan tickerData from window.__STATE__ when present."""
        for script in parser.css("script"):
            raw = script.text()
            if "window.__STATE__" not in raw:
                continue
            match = _WINDOW_STATE_RE.search(raw)
            if not match:
                continue
            try:
                state = json.loads(match.group(1))
            except Exception:
                continue
            ticker_data = (state.get("context") or {}).get("tickerData")
            if isinstance(ticker_data, dict):
                cls.read_dylan_match(metrics, ticker_data)

    @staticmethod
    def _clean_field_value_text(value_node) -> str:
        """Return the text of a WSJ `.data_data` node without nested `.data_meta` dates."""
        raw = value_node.text(strip=True)
        for meta in value_node.css("[class*='data_meta'], .data_meta, small"):
            meta_txt = meta.text(strip=True)
            if meta_txt and raw.endswith(meta_txt):
                raw = raw[: -len(meta_txt)].strip()
        return raw

    def _find_range_by_label(
        self, parser: HTMLParser, labels: list[str]
    ) -> tuple[Optional[float], Optional[float]]:
        wanted = {label.lower() for label in labels}
        for field_node in parser.css("[class*='cr_data_field'], .cr_data_field"):
            label_node = field_node.css_first("[class*='data_lbl'], .data_lbl")
            if not label_node or label_node.text(strip=True).lower() not in wanted:
                continue
            value_node = field_node.css_first("[class*='data_data'], .data_data")
            if not value_node:
                return None, None
            raw = self._clean_field_value_text(value_node)
            if " - " in raw:
                left, _, right = raw.partition(" - ")
                low = parse_number(left, decimal=".")
                high = parse_number(right, decimal=".")
                if low is not None and high is not None:
                    return low, high
            return None, None
        return None, None

    def _parse_insider_transactions(self, parser: HTMLParser) -> dict:
        """Parse Transaction Summary and Most Recent Insider Transactions."""
        data = {"Summary": {}, "Transactions": []}

        # 1. Transaction Summary
        summary_table = parser.css_first("table.cr_mod_insider")
        if summary_table:
            for row in summary_table.css("tbody tr"):
                cols = row.css("td")
                if len(cols) >= 3:
                    timeframe = cols[0].text(strip=True)
                    tx_text = " ".join(
                        [
                            _collapse_ws(node.text(strip=False))
                            for node in cols[1].css(".data_data")
                        ]
                    )
                    shares_nodes = cols[2].css(".data_data")
                    shares_text = " / ".join(
                        [_collapse_ws(node.text(strip=False)) for node in shares_nodes]
                    )

                    data["Summary"][timeframe] = {"transactions": tx_text, "shares": shares_text}

        # 2. Most Recent Insider Transactions
        recent_table = parser.css_first("table.cr_mod_transactions")
        if recent_table:
            for row in recent_table.css("tbody tr"):
                cols = row.css("td")
                if len(cols) >= 5:
                    owner_node = cols[1].css_first(".data_data")
                    owner_name = (
                        owner_node.text(strip=True)
                        if owner_node is not None
                        else cols[1].text(strip=True)
                    )
                    data["Transactions"].append(
                        {
                            "date": cols[0].text(strip=True),
                            "ownerName": owner_name,
                            "shares": cols[2].text(strip=True),
                            "description": _collapse_ws(cols[3].text(strip=False)),
                            "value": cols[4].text(strip=True),
                        }
                    )

        return data

    def _find_value_by_label(self, parser: HTMLParser, labels: list[str]) -> Optional[float]:
        wanted = {label.lower() for label in labels}

        # 1. Structured WSJ Key Stock Data / Overview cells:
        # <div class="WSJTheme--cr_data_field--..."><h5 class="WSJTheme--data_lbl--...">P/E Ratio (TTM)</h5>
        # <span class="WSJTheme--data_data--...">7.17<small class="WSJTheme--data_meta--...">(11/02/26)</small></span></div>
        for field_node in parser.css("[class*='cr_data_field'], .cr_data_field"):
            label_node = field_node.css_first("[class*='data_lbl'], .data_lbl")
            if not label_node or label_node.text(strip=True).lower() not in wanted:
                continue
            value_node = field_node.css_first("[class*='data_data'], .data_data")
            if not value_node:
                return None
            raw = self._clean_field_value_text(value_node)
            parsed = parse_number(raw, decimal=".")
            return parsed if parsed is not None else find_number(raw)

        # 2. Generic fallback for simple label/value markup
        for node in parser.css("div, span, td, li"):
            txt = node.text(strip=True)
            if not txt or len(txt) >= 50:
                continue
            txt_lower = txt.lower()
            for label in labels:
                label_lower = label.lower()
                if label_lower in txt_lower:
                    if ":" in txt:
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


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


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
