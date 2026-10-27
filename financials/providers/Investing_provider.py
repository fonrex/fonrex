import logging
import re
from typing import Optional

from selectolax.parser import HTMLParser, Node

from financials.models import FinancialMetrics
from financials.numbers import find_number, parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)


class InvestingProvider(BaseProvider):
    """
    Investing.com Provider.
    """

    SEARCH_API = "https://api.investing.com/api/search/v2/search"
    ROOT_URL = "https://www.investing.com"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, identifier: str) -> Optional[FinancialMetrics]:
        async with self._session() as client:
            search_result = self._direct_search_result(identifier)
            if not search_result:
                search_result = await self._search_result(client, identifier)
            if not search_result:
                return None

            fallback_metrics = self._metrics_from_search_result(search_result, identifier)
            full_url = search_result["provider_url"]

            # Fetch
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://www.investing.com/",
                }
                resp = await client.get(full_url, headers=headers)
                if resp.status_code == 200:
                    metrics = self._parse_page(
                        HTMLParser(resp.text), search_result.get("ticker") or identifier
                    )
                    return self._merge_search_result(metrics, fallback_metrics)
                logger.warning(
                    f"Investing page fetch returned HTTP {resp.status_code} for {full_url}"
                )
                return fallback_metrics
            except Exception as e:
                logger.error(f"Investing fetch error: {e}")
                return fallback_metrics

        return None

    @classmethod
    def _direct_search_result(cls, identifier: str) -> Optional[dict]:
        if not identifier:
            return None
        identifier = identifier.strip()
        if identifier.startswith("http") or identifier.startswith("/equities/"):
            return {
                "url": identifier,
                "provider_url": cls._full_url(identifier),
                "ticker": None,
                "name": None,
                "isin": None,
            }
        return None

    @classmethod
    def _full_url(cls, url_path: str) -> str:
        if url_path.startswith("http"):
            return url_path
        if url_path.startswith("/"):
            return cls.ROOT_URL + url_path
        return cls.ROOT_URL + "/equities/" + url_path

    @staticmethod
    def _looks_like_isin(value: str) -> bool:
        value = (value or "").strip().upper()
        return len(value) == 12 and value.isalnum() and value[:2].isalpha()

    @classmethod
    def _select_quote(cls, data: dict, query: str) -> Optional[dict]:
        quotes = data.get("quotes") or []
        if not quotes:
            return None
        return quotes[0]

    @classmethod
    def _search_result_from_quote(cls, quote: dict, query: str) -> Optional[dict]:
        if not quote or not quote.get("url"):
            return None

        result = {
            "url": quote.get("url"),
            "provider_url": cls._full_url(quote.get("url")),
            "ticker": quote.get("symbol"),
            "name": quote.get("description"),
            "exchange": quote.get("exchange"),
            "instrument_type": quote.get("type"),
            "isin": query.strip().upper() if cls._looks_like_isin(query) else None,
        }
        return result

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

    async def _search_result(self, client: ProviderSession, query: str) -> Optional[dict]:
        try:
            resp = await client.get(self.SEARCH_API, params={"q": query})
            if resp.status_code == 200:
                data = resp.json()
                quote = self._select_quote(data, query)
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

    @staticmethod
    def _assign_key_figure(metrics: FinancialMetrics, label: str, text: str, decimal: str):
        """Set the field of a key figure; the first value of a field wins."""
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
