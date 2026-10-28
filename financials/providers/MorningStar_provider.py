"""MorningStarProvider — the quote page of a share on global.morningstar.com.

The page of a share is ``/fr/investissements/actions/{id}/cours``, where ``id`` is the
Morningstar identifier of the share (``0P0000OQN8`` for Tesla). The identifier comes
from a mapping (the page URL or the identifier itself), or from the legacy search of
morningstar.fr with the ISIN; that search now redirects to the global site and finds
nothing, so a share without a mapping is not found.

The page is rendered on the server (Nuxt): its figures are ``name`` / ``value`` pairs
(``.sal-dp-name`` and ``.sal-dp-value``, ``.dp-name`` and ``.dp-value``). A French page
writes ``1,61 Bil`` for 1.61 thousand billion (a French *billion*), an English one
``1.61 Bil`` for 1.61 billion. The site may answer a script with an AWS WAF challenge
(HTTP 202): that answer holds no figure and is reported, never parsed.
"""

import logging
import random
import re
from typing import Optional

from selectolax.parser import HTMLParser, Node

from financials.models import FinancialMetrics
from financials.numbers import find_number, parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# A Morningstar identifier: "0P" and eight letters or digits.
_MS_ID = re.compile(r"\b(0P[0-9A-Z]{8})\b")
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")
_CURRENCY = re.compile(r"Devise en ([A-Z]{3})|Currency in ([A-Z]{3})")
# "1,61 Bil": the scale of an amount, which depends on the language of the page
_SCALED = re.compile(r"^(.*?)\s*(Bil|Tril)\.?$", re.IGNORECASE)
_SCALES = {"fr": {"bil": 1e12}, "en": {"bil": 1e9, "tril": 1e12}}


class MorningStarProvider(BaseProvider):
    """Figures of a share from its quote page on global.morningstar.com."""

    SEARCH_API = "https://www.morningstar.fr/fr/util/SecuritySearch.ashx"
    PAGE_URL = "https://global.morningstar.com/fr/investissements/actions/{ms_id}/cours"
    PDF_URL = (
        "https://tools.morningstar.fr/fr/util/documentproxy.aspx?key=EquityQuant&secId={ms_id}"
    )

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, identifier: str) -> Optional[FinancialMetrics]:
        """The figures of a share given by its ISIN, its Morningstar identifier or its page.

        ``None`` when no Morningstar identifier is found. When the page cannot be read,
        the identifiers alone are returned.
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

            url = self.PAGE_URL.format(ms_id=ms_id)
            metrics = FinancialMetrics(ticker=identifier, provider_url=url)
            try:
                resp = await client.get(url, headers={"User-Agent": random.choice(USER_AGENTS)})
                if resp.status_code == 200 and resp.text:
                    metrics = self._parse_page(HTMLParser(resp.text), identifier, url)
                elif resp.status_code == 202 or "x-amzn-waf-action" in resp.headers:
                    logger.warning("MorningStar answered %s with a bot challenge (AWS WAF)", url)
                else:
                    logger.warning("MorningStar page %s: HTTP %s", url, resp.status_code)
            except Exception as e:
                logger.error("MorningStar fetch error for %s: %s", identifier, e)

            if _ISIN.fullmatch(identifier.upper()):
                metrics.isin = identifier.upper()
            metrics.morningstar_id = ms_id
            metrics.morningstar_pdf_url = self.PDF_URL.format(ms_id=ms_id)
            return metrics

    @staticmethod
    def direct_id(identifier: str) -> Optional[str]:
        """The Morningstar identifier written in the identifier itself, or in its URL."""
        match = _MS_ID.search(identifier)
        return match.group(1) if match else None

    async def _search_id(self, client: ProviderSession, isin: str) -> Optional[str]:
        """The legacy search of morningstar.fr; it now redirects to the global site."""
        url = self.SEARCH_API + "?source=nav&moduleId=6&ifIncludeAds=True&usrtType=v"
        try:
            resp = await client.post(url, data={"q": isin})
            if resp.status_code == 200:
                match = re.search(r'{"i":"([^"]+)"', resp.text)
                if match:
                    return match.group(1)
                match = _MS_ID.search(resp.text)
                if match:
                    return match.group(1)
        except Exception as e:
            logger.warning("MorningStar search error for %s: %s", isin, e)
        return None

    # ── Page ──────────────────────────────────────────────────────────────────

    def _parse_page(self, parser: HTMLParser, ticker: str, url: str) -> FinancialMetrics:
        """The figures of a quote page; a figure not displayed stays ``None``."""
        metrics = FinancialMetrics(ticker=ticker, provider_url=url)
        html = parser.css_first("html")
        language = (
            "fr" if (html and (html.attributes.get("lang") or "fr").startswith("fr")) else "en"
        )
        decimal = "," if language == "fr" else "."

        name = parser.css_first('h1 [itemprop="name"]') or parser.css_first("h1 span")
        if name:
            metrics.name = name.text(strip=True) or None

        price = parser.css_first("#message-box-price")
        if price:
            metrics.price = parse_number(price.text(strip=True), decimal=decimal)
        change = parser.css_first("#message-box-percentage")
        if change:
            metrics.change_percent = find_number(
                change.text(separator=" ", strip=True), decimal=decimal, last=True
            )
        currency = _CURRENCY.search(parser.body.text(separator=" ") if parser.body else "")
        if currency:
            metrics.currency = currency.group(1) or currency.group(2)

        for name_node in parser.css(".sal-dp-name, .dp-name"):
            value_node = _value_of(name_node)
            if value_node is None:
                continue
            label = _label(name_node.text(strip=True))
            text = " ".join(value_node.text(separator=" ").split())
            try:
                self._assign(metrics, label, text, decimal, language)
            except (AttributeError, TypeError, ValueError) as error:
                # One unreadable figure must not lose the others
                logger.warning("MorningStar figure %r not read: %s", label, error)
        return metrics

    @staticmethod
    def _assign(metrics: FinancialMetrics, label: str, text: str, decimal: str, language: str):
        """Set the field of a figure; the first value of a field wins."""

        def number(value: str) -> Optional[float]:
            scaled = _SCALED.match(value.strip())
            if scaled:
                scale = _SCALES[language].get(scaled.group(2).lower())
                base = parse_number(scaled.group(1), decimal=decimal)
                return base * scale if base is not None and scale else None
            return parse_number(value, decimal=decimal)

        def put(field: str, value) -> None:
            if value is not None and getattr(metrics, field, None) is None:
                setattr(metrics, field, value)

        if label in TEXT_FIELDS:
            put(TEXT_FIELDS[label], text or None)
        elif label in PAIR_FIELDS:
            # "44,6 M / 36,9 M" or "297,38 – 498,83"
            parts = re.split(r"\s+/\s+|\s*[–—]\s*|\s+-\s+", text, maxsplit=1)
            if len(parts) == 2:
                first, second = PAIR_FIELDS[label]
                low, high = number(parts[0]), number(parts[1])
                if low is not None and high is not None:
                    put(first, low)
                    put(second, high)
        elif label in NUMBER_FIELDS:
            field = NUMBER_FIELDS[label]
            value = number(text)
            if field in INTEGER_FIELDS and value is not None:
                value = int(value) if value == int(value) else None
            put(field, value)


def _label(text: str) -> str:
    """A label compared in lower case, without what follows a parenthesis: "Bêta(5-Année)"."""
    return " ".join(text.split("(")[0].lower().split())


def _value_of(name_node: Node) -> Optional[Node]:
    """The value next to a name: its sibling of class ``*-value``, else the next element."""
    parent = name_node.parent
    if parent is not None:
        for child in parent.iter():
            classes = child.attributes.get("class") or ""
            if child is not name_node and re.search(r"(^|\s)(sal-)?dp-value", classes):
                return child
    sibling = name_node.next
    while sibling is not None and sibling.tag in ("-text", "_comment", "-comment"):
        sibling = sibling.next
    return sibling


# The labels of the quote page, in French and in English. Percentages are kept as
# displayed (see monitoring/units.py).
TEXT_FIELDS = {
    "secteur": "sector",
    "sector": "sector",
    "industrie": "industry",
    "industry": "industry",
    "style du placement": "investment_style",
    "investment style": "investment_style",
}
NUMBER_FIELDS = {
    "cap. boursière": "market_cap",
    "market cap": "market_cap",
    "capitalisation": "market_cap",
    "cours/ventes": "ps_ratio",
    "price/sales": "ps_ratio",
    "cours/valeur comptable": "pb_ratio",
    "price/book": "pb_ratio",
    "cours/bénéfices": "pe_ratio",
    "price/earnings": "pe_ratio",
    "per": "pe_ratio",
    "rend div glissant": "dividend_yield",
    "trailing div yield": "dividend_yield",
    "rendement div.": "dividend_yield",
    "dividend yield": "dividend_yield",
    "rendement div prévisionnel": "dividend_yield_forward",
    "forward div yield": "dividend_yield_forward",
    "bêta": "beta",
    "beta": "beta",
    "flottant": "float_shares",
    "float": "float_shares",
    "actions en circulation": "shares_outstanding",
    "shares outstanding": "shares_outstanding",
    "% à découvert du flottant": "short_percent_float",
    "short interest % of float": "short_percent_float",
    "actions à découvert": "shares_short",
    "shares short": "shares_short",
    "ratio ventes à découvert": "short_ratio",
    "short ratio": "short_ratio",
    "employés": "employees",
    "employees": "employees",
}
INTEGER_FIELDS = {"employees"}
PAIR_FIELDS = {
    "volume / moy": ("volume", "average_volume"),
    "volume/moyenne": ("volume", "average_volume"),
    "volume / avg": ("volume", "average_volume"),
    "fourchette annuelle": ("week_52_low", "week_52_high"),
    "year range": ("week_52_low", "week_52_high"),
}
