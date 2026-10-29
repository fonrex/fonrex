"""GurufocusProvider — the scores and ratios of the summary page of a share on GuruFocus.

One page is read: ``/stock/{symbol}/summary``. GuruFocus renders it on its server
(Nuxt), so the figures are in the HTML. Each figure of its tables sits in a row
whose label links to the definition of the term (``/term/fscore/TSLA``): the row is
found by that code, never by the first number of the page, and the value is the
cell next to the label.

Read: the GF Score (``/100``), the Piotroski F-Score (``/9``), the Beneish M-Score,
the Altman Z-Score, the ROIC, the PE ratio, the net and operating margins, the debt to equity, the EPS
and the revenue of the last twelve months. Percentages are returned as displayed
(``3.11`` for 3.11 %), as declared in ``monitoring/units.py``.
"""

import json
import logging
import re
from typing import Optional

from selectolax.parser import HTMLParser, Node

from financials.exchange import GURUFOCUS_TO_YAHOO
from financials.models import FinancialMetrics
from financials.numbers import parse_number
from financials.providers.base import BaseProvider, ProviderSession

logger = logging.getLogger(__name__)

# Code of the term in the link of a row → field of FinancialMetrics.
TERM_FIELDS = {
    "fscore": "piotroski_score",
    "mscore": "beneish_m_score",
    "zscore": "altman_z_score",
    "roic": "roic",
    "pettm": "pe_ratio",
    "net-margin": "profit_margin",
    "operating-margin": "operating_margin",
    "debt-to-equity": "debt_to_equity",
    "eps-diluated": "eps",  # sic: the code GuruFocus uses
    "revenue": "revenue",
}

# The scale an amount is displayed in, written in its label: "Revenue (TTM) (Mil $)".
_SCALE = re.compile(r"\((Thou|Mil|Bil)\b", re.IGNORECASE)
_SCALES = {"thou": 1e3, "mil": 1e6, "bil": 1e9}

# A US listing has no exchange in the address of its page (``/stock/TSLA/``).
_US_EXCHANGES = frozenset(code for code, suffix in GURUFOCUS_TO_YAHOO.items() if suffix == "")

# Server-rendered Nuxt state carrying ``gf_score`` when its badge is client-rendered.
# The score of the page is a property of its ``stock:{...}`` object.
_NUXT_STOCK = re.compile(r"\bstock:\{")
_NUXT_GF_SCORE = re.compile(r"gf_score:([A-Za-z0-9_$.+-]+)")
_NUXT_PARAMS = re.compile(r"window\.__NUXT__=\(function\(([^)]*)\)\{")
# A number literal of JavaScript as Nuxt writes it: ``95``, ``-2.4``, ``.93``.
_JS_NUMBER = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)")
_OPENING = {"(": ")", "[": "]", "{": "}"}


class GurufocusProvider(BaseProvider):
    """Scores and ratios of a share, from its GuruFocus summary page."""

    BASE_URL = "https://www.gurufocus.com"

    def __init__(self, max_retries: int = 3, timeout: int = 20):
        self.max_retries = max_retries
        self.timeout = timeout
        # ``Accept: text/html...`` placed after ``Referer`` lets both ``/_api/_search``
        # and ``/stock/{symbol}/summary`` pass Cloudflare without a JS challenge:
        # the JSON ``Accept`` of a browser XHR is refused on the search.
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,fr-FR;q=0.8,fr;q=0.7",
            "Origin": "https://www.gurufocus.com",
            "Referer": "https://www.gurufocus.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """Financial metrics, after resolving the ticker with the search API."""
        async with self._session(headers=self.headers) as client:
            # 1. Resolve the GuruFocus ticker (e.g. TFI.PA -> XPAR:TFI)
            gf_ticker = await self._resolve_gf_ticker(client, ticker)
            if not gf_ticker:
                logger.warning("Could not resolve GuruFocus ticker for %s", ticker)
                return None
            logger.info("Resolved GuruFocus ticker: %s -> %s", ticker, gf_ticker)

            # 2. Read the summary page
            url = self.summary_url(gf_ticker)
            page = None
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    page = HTMLParser(resp.text)
                else:
                    logger.warning("GuruFocus page of %s: HTTP %s", gf_ticker, resp.status_code)
            except Exception as e:
                logger.error("Error fetching the GuruFocus page of %s: %s", gf_ticker, e)

            metrics = (
                self._parse_page(page, gf_ticker)
                if page is not None
                else FinancialMetrics(ticker=gf_ticker)
            )
            metrics.ticker = gf_ticker
            metrics.provider_url = url
            return metrics

    @classmethod
    def summary_url(cls, gf_ticker: str) -> str:
        """The summary page: ``/stock/TSLA/summary``, ``/stock/XPAR:AIR/summary``."""
        exchange, _, symbol = gf_ticker.rpartition(":")
        path = symbol if exchange.upper() in _US_EXCHANGES else gf_ticker
        return f"{cls.BASE_URL}/stock/{path}/summary"

    # ── Page ──────────────────────────────────────────────────────────────────

    def _parse_page(self, page: HTMLParser, ticker: str) -> FinancialMetrics:
        """The figures of a summary page; a figure not displayed stays ``None``."""
        metrics = FinancialMetrics(ticker=ticker)

        title = page.css_first("h1")
        if title is not None:
            name = " ".join(title.text(deep=True).split())
            metrics.name = name or None

        gf_score = page.css_first('a[href*="/term/gf-score/"] .t-primary')
        if gf_score is not None:
            metrics.gf_score = parse_number(gf_score.text(deep=True), decimal=".")
        if metrics.gf_score is None:
            metrics.gf_score = _nuxt_gf_score(page)

        for code, field in TERM_FIELDS.items():
            row = self._row(page, code)
            if row is None:
                continue
            label, value = row
            number = parse_number(value, decimal=".")
            if number is None:
                continue
            if field == "piotroski_score":
                if number != int(number) or not 0 <= number <= 9:
                    continue
                number = int(number)
            elif field == "revenue":
                scale = _SCALE.search(label)
                if scale:
                    number *= _SCALES[scale.group(1).lower()]
            setattr(metrics, field, number)

        return metrics

    @staticmethod
    def _row(page: HTMLParser, code: str) -> Optional[tuple[str, str]]:
        """The label and the value of the first row of a table whose label links to
        the term ``code``. The value drops what follows it (``4/9`` gives ``4``)."""
        for link in page.css(f'tr a[href*="/term/{code}/"]'):
            cell = _ancestor(link, "td")
            if cell is None:
                continue
            value_cell = cell.next
            while value_cell is not None and value_cell.tag != "td":
                value_cell = value_cell.next
            if value_cell is None:
                continue
            for extra in value_cell.css(".t-label, svg"):
                extra.decompose()
            label = " ".join(cell.text(deep=True).split())
            value = " ".join(value_cell.text(deep=True).split())
            return label, value
        return None

    # ── Ticker ────────────────────────────────────────────────────────────────

    async def _resolve_gf_ticker(self, client: ProviderSession, ticker: str) -> Optional[str]:
        """
        Resolve the GuruFocus ticker with the search API and the exchange mapping.
        E.g. TFI.PA -> XPAR:TFI
        """
        # The ticker already holds a GuruFocus exchange (e.g. NAS:AAPL)
        if ":" in ticker:
            return ticker

        # Split the symbol and the exchange suffix, if any
        parts = ticker.split(".")
        symbol = parts[0].upper()
        target_suffix = f".{parts[1].upper()}" if len(parts) > 1 else ""

        resolved = await self._search_symbol(client, symbol, target_suffix)
        if resolved:
            return resolved

        # Fallback: when the search fails, rebuild the ticker from the exchange mapping
        for gf_exch, suffix in GURUFOCUS_TO_YAHOO.items():
            if suffix.upper() == target_suffix.upper():
                # No suffix (US market): prefer NAS or NYSE
                if target_suffix == "" and gf_exch not in ["NAS", "NYSE"]:
                    continue
                return f"{gf_exch}:{symbol}"

        return None

    async def _search_symbol(
        self, client: ProviderSession, query: str, target_suffix: Optional[str]
    ) -> Optional[str]:
        """
        Query the GuruFocus search API and pick the best result.
        """
        search_url = f"{self.BASE_URL}/reader/_api/_search?v=1.8.50&text={query}&type="
        try:
            resp = await client.get(search_url)
            if resp.status_code == 200:
                results = resp.json()
                wanted = query.upper()
                matches = [
                    item.get("data") or {}
                    for item in results
                    if isinstance(item, dict)
                    and item.get("type") == "stock"
                    and (item.get("data") or {}).get("symbol", "").upper() == wanted
                ]

                if target_suffix:
                    for data in matches:
                        gf_exchange = data.get("exchange", "").upper()
                        mapped_suffix = GURUFOCUS_TO_YAHOO.get(gf_exchange)
                        if mapped_suffix and mapped_suffix.upper() == target_suffix.upper():
                            return f"{gf_exchange}:{wanted}"
                    return None

                matches.sort(key=lambda data: data.get("exchange", "").upper() not in _US_EXCHANGES)
                if matches:
                    gf_exchange = matches[0].get("exchange", "").upper()
                    return f"{gf_exchange}:{wanted}" if gf_exchange else wanted

        except Exception as e:
            logger.error("Error during GuruFocus search for %s: %s", query, e)
        return None


def _nuxt_gf_score(page: HTMLParser) -> Optional[float]:
    """The GF Score written in the server-rendered ``window.__NUXT__`` script."""
    for script in page.css("script"):
        text = script.text()
        token = _stock_gf_score_token(text)
        if token is None:
            continue
        # A parameter name of the IIFE (``bZ``, ``usd1``) is never read as a number.
        score = (
            _resolve_nuxt_arg(text, token)
            if token[0].isalpha() or token[0] in "_$"
            else parse_number(token, decimal=".")
        )
        if score is not None and 0 < score <= 100:
            return score
    return None


def _stock_gf_score_token(script_text: str) -> Optional[str]:
    """The value written for ``gf_score`` directly in a ``stock:{...}`` object.

    A ``gf_score`` nested deeper (a peer, a comparison) belongs to another share.
    """
    for start in _NUXT_STOCK.finditer(script_text):
        depth = 0
        in_string = False
        i = start.end() - 1
        while i < len(script_text):
            char = script_text[i]
            if in_string:
                if char == "\\":
                    i += 1
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    break
            if depth == 1 and not in_string and char in "{,":
                match = _NUXT_GF_SCORE.match(script_text, i + 1)
                if match:
                    return match.group(1)
            i += 1
    return None


def _resolve_nuxt_arg(script_text: str, token: str) -> Optional[float]:
    """The numeric argument of the ``window.__NUXT__`` IIFE bound to ``token``.

    Only that argument is decoded: the others may be JavaScript that is not JSON
    (``void 0``, ``Array(2)``, ``new Date(...)``, ``{a:1}``) and are skipped.
    """
    params_match = _NUXT_PARAMS.search(script_text)
    if not params_match:
        return None
    params = [p.strip() for p in params_match.group(1).split(",")]
    if token not in params:
        return None
    args = _iife_args(script_text, params_match.end() - 1)
    idx = params.index(token)
    if args is None or idx >= len(args):
        return None
    return _js_number(args[idx])


def _iife_args(text: str, body_start: int) -> Optional[list[str]]:
    """The source of each argument passed to the function whose body opens at
    ``body_start``: ``function(a,b){...}(1,"x")`` gives ``["1", '"x"']``."""
    body_end = _closing(text, body_start)
    if body_end is None:
        return None
    call_start = body_end + 1
    while call_start < len(text) and text[call_start].isspace():
        call_start += 1
    if call_start >= len(text) or text[call_start] != "(":
        return None
    call_end = _closing(text, call_start)
    if call_end is None:
        return None
    args: list[str] = []
    depth = 0
    piece = call_start + 1
    i = piece
    while i < call_end:
        char = text[i]
        if char in "\"'":
            i = _string_end(text, i)
            continue
        if char in _OPENING:
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            args.append(text[piece:i].strip())
            piece = i + 1
        i += 1
    args.append(text[piece:call_end].strip())
    return args


def _closing(text: str, start: int) -> Optional[int]:
    """The index of the bracket closing the one at ``start``; strings are skipped."""
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char in "\"'":
            i = _string_end(text, i)
            continue
        if char in _OPENING:
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _string_end(text: str, start: int) -> int:
    """The index just after the JavaScript string opening at ``start``."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return len(text)


def _js_number(source: str) -> Optional[float]:
    """A number written as a JavaScript literal (``95``, ``.93``, ``"81"``)."""
    if _JS_NUMBER.fullmatch(source):
        return parse_number(source, decimal=".")
    if source[:1] == '"':
        try:
            value = json.loads(source)
        except ValueError:
            return None
        return parse_number(value, decimal=".") if isinstance(value, str) else None
    return None


def _ancestor(node: Node, tag: str) -> Optional[Node]:
    while node is not None and node.tag != tag:
        node = node.parent
    return node
