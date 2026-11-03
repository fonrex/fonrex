"""InvestingProvider — the figures of a share from its Investing.com equity page.

An equity page is found through ``/search/service/SearchInnerPage`` (ISIN first
when given, otherwise the ticker sent without its Yahoo suffix; the listing is
matched by the country flag and exchange of the quote;
``api.investing.com/api/search/v2/search`` is kept as a fallback) or read
directly when a ``provider_url`` is supplied. Only an equity page of
``www.investing.com`` or ``fr.investing.com`` is ever read. One fixed set of
browser headers is used for the session; the ``User-Agent`` can be overridden
with ``INVESTING_USER_AGENT``.
"""

import logging
import os
import re
from typing import Optional
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser, Node

from financials.models import FinancialMetrics
from financials.numbers import find_number, parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")

# Hosts whose equity pages may be read: the English and French editions.
INVESTING_HOSTS = {"www.investing.com", "fr.investing.com"}
# Path of an equity page: "/equities/<slug>", optionally a sub-page ("/equities/<slug>/ratios").
_EQUITY_PATH = re.compile(r"/equities/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?")
# Slug of an equity page, as some search answers and mappings give it ("apple-computer-inc").
_EQUITY_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

USER_AGENT_ENV_VAR = "INVESTING_USER_AGENT"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Safari/605.1.15"
)

SEARCH_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.investing.com",
    "Referer": "https://www.investing.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Yahoo suffix -> country flags and exchange names returned by SearchInnerPage.
PLACES: dict[str, tuple[set[str], set[str]]] = {
    "PA": ({"france"}, {"paris"}),
    "AS": ({"netherlands"}, {"amsterdam"}),
    "BR": ({"belgium"}, {"brussels"}),
    "LS": ({"portugal"}, {"lisbon"}),
    "IR": ({"ireland"}, {"dublin"}),
    "DE": ({"germany"}, {"xetra"}),
    "F": ({"germany"}, {"frankfurt"}),
    "MI": ({"italy"}, {"milan"}),
    "MC": ({"spain"}, {"madrid"}),
    "HE": ({"finland"}, {"helsinki"}),
    "VI": ({"austria"}, {"vienna"}),
    "L": ({"uk", "united_kingdom"}, {"london"}),
    "SW": ({"switzerland"}, {"switzerland", "six"}),
    "ST": ({"sweden"}, {"stockholm"}),
    "CO": ({"denmark"}, {"copenhagen"}),
    "OL": ({"norway"}, {"oslo"}),
    "TO": ({"canada"}, {"toronto"}),
    "T": ({"japan"}, {"tokyo"}),
    "HK": ({"hong_kong", "hong kong"}, {"hong kong"}),
    "AX": ({"australia"}, {"sydney"}),
}
US_FLAGS = {"usa", "united_states"}
US_EXCHANGES = {"nasdaq", "nyse", "nyse arca", "nyse american"}


def user_agent() -> str:
    """User-Agent sent to Investing.com, configurable with ``INVESTING_USER_AGENT``."""
    return (os.environ.get(USER_AGENT_ENV_VAR) or "").strip() or DEFAULT_USER_AGENT


def session_headers() -> dict[str, str]:
    """Fixed headers shared by the requests of one Investing.com session.

    A navigation typed in the address bar (``Sec-Fetch-Site: none``) carries no
    Referer: sending both would be a combination no browser produces. The search
    requests add their own ``Referer`` (``SEARCH_HEADERS``).
    """
    return {
        "User-Agent": user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }


def _ticker_and_place(ticker: str) -> tuple[str, Optional[tuple[set[str], set[str]]]]:
    """The bare symbol and the place of a Yahoo ticker: ``"AIR.PA"`` -> ``("AIR", Paris)``."""
    symbol, _, suffix = ticker.rpartition(".")
    if symbol and suffix in PLACES:
        return symbol, PLACES[suffix]
    return ticker, None


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "/"))


def equity_page_url(value: Optional[str]) -> Optional[str]:
    """The URL of an Investing.com equity page, or ``None`` when ``value`` is not one.

    ``value`` is a full URL, a path (``/equities/eads``) or a bare slug (``eads``).
    Only ``www.investing.com`` and ``fr.investing.com`` are accepted, on the default
    port and without credentials; the edition is kept (it decides the language of
    the page), the scheme is forced to ``https`` and the query string is dropped.
    Any other host is refused, so that a mapping or a search answer can never make
    the provider read another site.
    """
    value = (value or "").strip()
    if not value:
        return None
    if not _looks_like_url(value):
        return (
            f"https://www.investing.com/equities/{value}" if _EQUITY_SLUG.fullmatch(value) else None
        )
    if value.startswith("//"):
        return None
    if value.startswith("/"):
        host, path = "www.investing.com", value.split("?", 1)[0].split("#", 1)[0]
    else:
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        if host not in INVESTING_HOSTS or port is not None or parts.username or parts.password:
            return None
        path = parts.path
    if not _EQUITY_PATH.fullmatch(path) or {".", ".."} & set(path.split("/")):
        return None
    return f"https://{host}{path}"


def _search_term(identifier: str) -> str:
    """What the search endpoint is asked: an ISIN as it is, a ticker without its Yahoo suffix."""
    wanted = identifier.strip().upper()
    if _ISIN.fullmatch(wanted):
        return wanted
    return _ticker_and_place(wanted)[0]


def _matches_place(quote: dict, flags: set[str], exchanges: set[str]) -> bool:
    """Tell whether a search quote belongs to the requested place."""
    flag = (quote.get("flag") or "").strip().lower()
    exchange = (quote.get("exchange") or "").strip().lower()
    if flag and exchange:
        return flag in flags and exchange in exchanges
    if flag:
        return flag in flags
    if exchange:
        return exchange in exchanges
    return False


class InvestingProvider(BaseProvider):
    """
    Investing.com Provider.
    """

    SEARCH_INNER_URL = "https://www.investing.com/search/service/SearchInnerPage"
    SEARCH_API = "https://api.investing.com/api/search/v2/search"
    ROOT_URL = "https://www.investing.com"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def fetch(
        self,
        ticker: Optional[str] = None,
        isin: Optional[str] = None,
        provider_url: Optional[str] = None,
        **kwargs,
    ) -> Optional[FinancialMetrics]:
        return await self.get_financials(ticker, isin=isin, provider_url=provider_url)

    async def get_financials(
        self,
        identifier: Optional[str] = None,
        *,
        isin: Optional[str] = None,
        provider_url: Optional[str] = None,
    ) -> Optional[FinancialMetrics]:
        """The figures of a listing given by its ticker, its ISIN or its equity page.

        ``identifier`` is a Yahoo ticker (``AIR.PA``), an ISIN or an equity page URL.
        With both a ticker and ``isin``, the ISIN is searched first and, among its
        listings, the one of the place named by the ticker suffix is taken. A page URL
        that is not an equity page of Investing.com is refused, never searched. An
        ISIN read on the page that differs from the one asked means another
        instrument: nothing is returned.
        """
        identifier = (identifier or "").strip() or None
        isin = (isin or "").strip().upper() or None
        if isin and not self._looks_like_isin(isin):
            isin = None
        if identifier and self._looks_like_isin(identifier):
            isin = isin or identifier.upper()
            identifier = None

        page_hint = provider_url or (
            identifier if identifier and _looks_like_url(identifier) else None
        )
        ticker = identifier if identifier and not _looks_like_url(identifier) else None
        page_url = equity_page_url(page_hint) if page_hint else None
        if page_hint and not page_url:
            logger.warning("Investing: %r is not an equity page of investing.com", page_hint)
            return None

        target = ticker or isin or page_url
        if not target:
            return None

        async with self._session(headers=session_headers()) as client:
            if page_url:
                search_result = {
                    "url": page_url,
                    "provider_url": page_url,
                    "ticker": ticker,
                    "name": None,
                    "isin": isin,
                }
            else:
                search_result = await self._resolve_search_result(client, ticker=ticker, isin=isin)
            if not search_result:
                return None

            fallback_metrics = self._metrics_from_search_result(search_result, target)
            full_url = search_result["provider_url"]

            try:
                resp = await client.get(full_url)
                if resp.status_code != 200:
                    logger.warning(
                        f"Investing page fetch returned HTTP {resp.status_code} for {full_url}"
                    )
                    return fallback_metrics
                metrics = self._parse_page(
                    HTMLParser(resp.text), search_result.get("ticker") or target
                )
            except Exception as e:
                logger.error(f"Investing fetch error: {e}")
                return fallback_metrics

            if isin and metrics.isin and metrics.isin != isin:
                logger.warning(
                    "Investing page %s shows ISIN %s, %s was asked", full_url, metrics.isin, isin
                )
                return None
            return self._merge_search_result(metrics, fallback_metrics)

    @staticmethod
    def _looks_like_isin(value: str) -> bool:
        value = (value or "").strip().upper()
        return bool(_ISIN.fullmatch(value))

    @classmethod
    def _select_quote(cls, data: dict, query: str, ticker: Optional[str] = None) -> Optional[dict]:
        """The quote of the listing asked, or ``None``: never a quote of another symbol.

        A ticker query keeps the quotes of its symbol only; a ticker with a Yahoo suffix
        keeps the listing of that place, a bare ticker prefers a United States listing.
        An ISIN query lists the listings of one company: ``ticker``, when given, chooses
        the place among them the same way. Only quotes linking an equity page of
        Investing.com are considered.
        """
        raw_quotes = data.get("quotes") if isinstance(data, dict) else None
        quotes = [
            q
            for q in (raw_quotes or [])
            if isinstance(q, dict) and equity_page_url(q.get("link") or q.get("url"))
        ]
        if not quotes:
            return None

        equities = [
            q for q in quotes if (q.get("pair_type") or "equities").strip().lower() == "equities"
        ]
        candidates = equities or quotes

        wanted = (query or "").strip().upper()
        if cls._looks_like_isin(wanted):
            if not ticker:
                return candidates[0]
            symbol, place = _ticker_and_place(ticker.strip().upper())
            if place is not None:
                matches = [q for q in candidates if _matches_place(q, *place)]
                return matches[0] if matches else None
            us_matches = [q for q in candidates if _matches_place(q, US_FLAGS, US_EXCHANGES)]
            by_symbol = [q for q in candidates if (q.get("symbol") or "").strip().upper() == symbol]
            return (us_matches or by_symbol or candidates)[0]

        symbol, place = _ticker_and_place(wanted)
        by_symbol = [q for q in candidates if (q.get("symbol") or "").strip().upper() == symbol]
        if not by_symbol:
            return None

        if place is not None:
            matches = [q for q in by_symbol if _matches_place(q, *place)]
            return matches[0] if matches else None

        us_matches = [q for q in by_symbol if _matches_place(q, US_FLAGS, US_EXCHANGES)]
        return us_matches[0] if us_matches else by_symbol[0]

    @classmethod
    def _search_result_from_quote(cls, quote: Optional[dict], query: str) -> Optional[dict]:
        url = equity_page_url((quote or {}).get("link") or (quote or {}).get("url"))
        if not quote or not url:
            return None

        return {
            "url": url,
            "provider_url": url,
            "ticker": quote.get("symbol"),
            "name": quote.get("name") or quote.get("description"),
            "exchange": quote.get("exchange"),
            "instrument_type": quote.get("type"),
            "isin": query.strip().upper() if cls._looks_like_isin(query) else None,
        }

    @classmethod
    def _metrics_from_search_result(cls, search_result: dict, identifier: str) -> FinancialMetrics:
        return FinancialMetrics(
            ticker=search_result.get("ticker") or identifier,
            isin=search_result.get("isin"),
            provider_url=search_result.get("provider_url"),
            name=search_result.get("name"),
            exchange=search_result.get("exchange"),
            instrument_type=search_result.get("instrument_type"),
        )

    @staticmethod
    def _merge_search_result(
        metrics: FinancialMetrics, fallback: FinancialMetrics
    ) -> FinancialMetrics:
        fallback_data = fallback.model_dump(exclude_none=True)
        for field, value in fallback_data.items():
            if getattr(metrics, field, None) is None:
                setattr(metrics, field, value)
        return metrics

    async def _resolve_search_result(
        self,
        client: ProviderSession,
        *,
        ticker: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> Optional[dict]:
        """Search by ISIN first (the listing chosen by the ticker), then by ticker.

        A result found by ticker does not carry the ISIN asked: only the page, or
        the runner, may tell that the instrument is the one of that ISIN.
        """
        if isin:
            result = await self._search_result(client, isin, ticker=ticker)
            if result:
                return result
        if ticker:
            return await self._search_result(client, ticker)
        return None

    async def _search_result(
        self, client: ProviderSession, query: str, ticker: Optional[str] = None
    ) -> Optional[dict]:
        term = _search_term(query)
        try:
            resp = await client.post(
                self.SEARCH_INNER_URL,
                data={"search_text": term, "tab": "quotes", "isFilter": "true"},
                headers=SEARCH_HEADERS,
            )
            if resp.status_code == 200:
                quote = self._select_quote(resp.json(), query, ticker=ticker)
                result = self._search_result_from_quote(quote, query)
                if result:
                    return result
        except Exception as e:
            logger.warning(f"Investing inner search error: {e}")

        try:
            resp = await client.get(self.SEARCH_API, params={"q": term})
            if resp.status_code == 200:
                quote = self._select_quote(resp.json(), query, ticker=ticker)
                return self._search_result_from_quote(quote, query)
        except Exception as e:
            logger.warning(f"Investing search error: {e}")
        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        """The figures of an equity page of investing.com, in English or in French.

        The key figures are ``<dt>label</dt><dd>value</dd>`` pairs: the ``<dd>`` is the
        next element after the ``<dt>``, whatever whitespace separates them. The
        language of the page (``<html lang>``) decides the decimal separator.
        """
        metrics = FinancialMetrics(ticker=ticker_input)
        html = parser.css_first("html")
        decimal = "," if (html and (html.attributes.get("lang") or "").startswith("fr")) else "."

        h1 = parser.css_first("h1")
        if h1:
            # "Tesla Inc (TSLA)": the symbol in parentheses is not part of the name
            name = re.sub(r"\s*\([^()]*\)\s*$", "", h1.text(strip=True))
            metrics.name = name or None

        last = parser.css_first('[data-test="instrument-price-last"]')
        if last:
            metrics.price = parse_number(last.text(strip=True), decimal=decimal)
        change = parser.css_first('[data-test="instrument-price-change-percent"]')
        if change:
            metrics.change_percent = find_number(
                change.text(strip=True).strip("()"), decimal=decimal
            )
        currency = parser.css_first('[data-test="currency-in-label"] span')
        if currency and _CURRENCY.fullmatch(currency.text(strip=True)):
            metrics.currency = currency.text(strip=True)

        for dt in parser.css("dt"):
            dd = _next_element(dt)
            if dd is None or dd.tag != "dd":
                continue
            label = _label(dt.text(strip=True))
            try:
                self._assign_key_figure(metrics, label, dd.text(strip=True), decimal)
            except (AttributeError, TypeError, ValueError) as error:
                # One unreadable figure must not lose the others
                logger.warning("Investing figure %r not read: %s", label, error)

        # Company profile: "<div>Industry</div><a>Automobiles…</a>"
        for name_node in parser.css("div.pr-3"):
            value_node = _next_element(name_node)
            if value_node is None:
                continue
            field = PROFILE_FIELDS.get(_label(name_node.text(strip=True)))
            value = value_node.text(strip=True)
            if field and value and getattr(metrics, field, None) is None:
                if field == "employees":
                    count = parse_number(value, decimal=decimal)
                    if count is not None and count == int(count):
                        metrics.employees = int(count)
                else:
                    setattr(metrics, field, value)
        return metrics

    @classmethod
    def _assign_key_figure(
        cls, metrics: FinancialMetrics, label: str, text: str, decimal: str
    ) -> None:
        """Set the field of a key figure; the first value of a field wins."""
        if label == "isin":
            candidate = text.strip().upper()
            if cls._looks_like_isin(candidate) and metrics.isin is None:
                metrics.isin = candidate
            return
        if label in DIVIDEND_LABELS:
            # "0.24 (0.11%)": the dividend per share, then the yield in parentheses
            yield_match = re.search(r"\(([^()]*%)\)", text)
            if yield_match and metrics.dividend_yield is None:
                metrics.dividend_yield = find_number(yield_match.group(1), decimal=decimal)
            rate = parse_number(text.split("(")[0], decimal=decimal)
            if rate is not None and getattr(metrics, "dividend_rate", None) is None:
                metrics.dividend_rate = rate
            return
        if label in RANGE_LABELS:
            low, high = RANGE_LABELS[label]
            parts = re.split(r"\s*-\s*(?=\d)", text, maxsplit=1)
            if len(parts) == 2 and getattr(metrics, low, None) is None:
                setattr(metrics, low, parse_number(parts[0], decimal=decimal))
                setattr(metrics, high, parse_number(parts[1], decimal=decimal))
            return
        field = KEY_FIGURES.get(label)
        if field is None or getattr(metrics, field, None) is not None:
            return
        # The <dd> holds the figure alone: a text ("Unlock", "N/A", "Q3 2026") is no figure
        value = parse_number(text, decimal=decimal)
        if value is not None:
            setattr(metrics, field, value)


def _label(text: str) -> str:
    """A label compared without case or spacing."""
    return " ".join(text.lower().split())


def _next_element(node: Node) -> Optional[Node]:
    """The next sibling that is an element, skipping text and comment nodes."""
    sibling = node.next
    while sibling is not None and sibling.tag in ("-text", "_comment", "-comment"):
        sibling = sibling.next
    return sibling


_CURRENCY = re.compile(r"[A-Z]{3}")

# Key figures of the equity page, in English and in French. Percentages are kept as
# displayed (see monitoring/units.py).
KEY_FIGURES = {
    "p/e ratio": "pe_ratio",
    "per": "pe_ratio",
    "eps": "eps",
    "bpa": "eps",
    "dividend yield": "dividend_yield",
    "market cap": "market_cap",
    "capitalisation": "market_cap",
    "shares outstanding": "shares_outstanding",
    "act. en circulation": "shares_outstanding",
    "revenue": "revenue",
    "chiffre d'affaires": "revenue",
    "net income": "net_income",
    "résultat net": "net_income",
    "book value / share": "book_value_per_share",
    "valeur comptable / action": "book_value_per_share",
    "return on assets": "roa",
    "rendement des actifs": "roa",
    "return on equity": "roe",
    "rendement des fonds propres": "roe",
    "gross profit margin": "gross_margin",
    "marge bénéficiaire brute": "gross_margin",
    "price/book": "pb_ratio",
    "p/b ratio": "pb_ratio",
    "ratio p/b": "pb_ratio",
    "ebitda": "ebitda",
    "ev/ebitda": "ev_ebitda",
    "beta": "beta",
    "bêta": "beta",
    "prev. close": "previous_close",
    "clôture précédente": "previous_close",
    "volume": "volume",
    "average volume (3m)": "average_volume",
    "average vol. (3m)": "average_volume",
    "volume moyen (3m)": "average_volume",
}
DIVIDEND_LABELS = {"dividend (yield)", "dividende"}
RANGE_LABELS = {
    "52 wk range": ("week_52_low", "week_52_high"),
    "52 week range": ("week_52_low", "week_52_high"),
    "ecart 52 sem.": ("week_52_low", "week_52_high"),
    "écart 52 sem.": ("week_52_low", "week_52_high"),
}
PROFILE_FIELDS = {
    "industry": "industry",
    "industrie": "industry",
    "sector": "sector",
    "secteur": "sector",
    "employees": "employees",
    "employés": "employees",
}
