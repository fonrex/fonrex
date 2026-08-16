import asyncio
import logging
import random
import re
from typing import Optional

import httpx
from selectolax.parser import HTMLParser, Node

from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# List of modern User-Agents for rotation
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]


class GoogleFinanceProvider(BaseProvider):
    """
    Modern, asynchronous provider for Google Finance using httpx and selectolax.

    Features:
    - Async fetching with httpx
    - Fast parsing with selectolax
    - User-Agent rotation
    - Retries with exponential backoff
    - Automatic Ticker Conversion (Yahoo -> Google)
    """

    BASE_URL = "https://www.google.com/finance"
    QUOTE_URL = "https://www.google.com/finance/quote/{ticker}"

    # Yahoo suffix to Google Exchange mapping
    # Sourced from legacy ToolsBox.py and GoogleFinance.py
    GOOGLE_EXCHANGES = {
        "NYSE": "New York Stock Exchange",
        "NASDAQ": "The NASDAQ Stock Market, Inc. – NASDAQ Last Sale",
        "NYSE_CHANGE_IT": "NYSE AMEX",
        "NYSEARCA": "NYSE ARCA",
        "OTC": "FINRA OTC Bulletin Board",
        "PINK": "FINRA OTC Bulletin Board",
        "TSE": "Toronto Stock Exchange",
        "CVE": "Toronto TSX Ventures Exchange",
        "OPRA": "Option Chains",
        "LON": "London Stock Exchange",
        "FRA": "Deutsche Börse Frankfurt Stock Exchange",
        "ETR": "Deutsche Börse XETRA",
        "BIT": "Borsa Italiana Milan Stock Exchange",
        "EPA": "NYSE Euronext Paris",
        "EBR": "NYSE Euronext Brussels",
        "ELI": "NYSE Euronext Lisbon",
        "AMS": "NYSE Euronext Amsterdam",
        "BOM": "Bombay Stock Exchange Limited",
        "NSE": "National Stock Exchange of India",
        "SHA": "Shanghai Stock Exchange",
        "SHE": "Shenzhen Stock Exchange",
        "TPE": "Taiwan Stock Exchange",
        "HKG": "Hong Kong Stock Exchange",
        "TYO": "Tokyo Stock Exchange",
        "ASX": "Australian Securities Exchange",
        "NZE": "New Zealand Stock Exchange",
        "WSE": "Warsaw Stock Exchange",
        "OTCMKTS": "OTCMKTS",
        "STO": "Swiss",  # Mapped from SW in legacy
        "HEL": "Helsinki",  # Mapped from HE
        "CPH": "Copenhagen",  # Mapped from CO
        "OSL": "Oslo",  # Mapped from OL
        "VIE": "Vienna",  # Mapped from VI
        "BME": "Madrid",  # Mapped from MC
    }

    # Reverse lookup map (Legacy suffix/code -> Google Exchange Code)
    # Used for direct mapping when we have "PA", "DE", etc.
    YAHOO_TO_GOOGLE_CODE = {
        "PA": "EPA",  # Paris
        "DE": "ETR",  # XETRA (Germany) - often defaults to ETR
        "AS": "AMS",  # Amsterdam
        "BR": "EBR",  # Brussels
        "MC": "BME",  # Madrid
        "L": "LON",  # London
        "VI": "VIE",  # Vienna
        "SW": "STO",  # Swiss
        "HE": "HEL",  # Helsinki
        "CO": "CPH",  # Copenhagen
        "OL": "OSL",  # Oslo
        "TO": "TSE",  # Toronto
        "HK": "HKG",  # Hong Kong
        "T": "TYO",  # Tokyo
        "MI": "BIT",  # Milan
    }

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """
        Fetches financial data for a given ticker.
        Handles Yahoo-style tickers (e.g., AIR.PA) by converting them.
        """
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                # 1. Handle URL input (extract ticker)
                is_url = False
                if ticker.startswith("http"):
                    is_url = True
                    # Extract ticker from URL: .../quote/AAPL:NASDAQ -> AAPL:NASDAQ
                    # Or .../quote/AAPL:NASDAQ?window=...
                    try:
                        if "/quote/" in ticker:
                            ticker = ticker.split("/quote/")[1].split("?")[0]
                    except Exception:
                        logger.warning(f"Could not extract ticker from URL {ticker}, using as is.")

                # 2. Normalize ticker (AIR.PA -> AIR:EPA)
                # If it was a URL, we assume the ticker is already in Google format (Symbol:Exchange)
                if is_url and ":" in ticker:
                    google_ticker = ticker
                elif ":" in ticker:
                    # If we already have a colon, we assume it is EXCHANGE:SYMBOL or SYMBOL:EXCHANGE
                    # We let _normalize_ticker handle the swap if necessary for the URL
                    google_ticker = ticker
                else:
                    # Otherwise we use the new centralized logic
                    # We pass the raw ticker, the exchange will be deduced or default
                    google_ticker = ticker

                # Normalisation finale pour l'URL (SYMBOL:EXCHANGE)
                url_ticker = self._normalize_ticker(google_ticker)

                # 3. Build URL
                url = self.QUOTE_URL.format(ticker=url_ticker)

                # 3. Fetch page
                html = await self._fetch_page(client, url)

                # 4. Fallback for US Stocks (Discovery Mode)
                # If failed (None) and it's a plain ticker (no dot/colon), it might be NYSE/AMEX etc.
                if not html and "." not in ticker and ":" not in ticker:
                    # We defaulted to NASDAQ in get_google_ticker. Now try others.
                    # Common US prefixes/suffixes for Google Finance
                    fallbacks = ["NYSE", "NYSEAMERICAN", "NYSEARCA", "OTCMKTS"]

                    for exchange in fallbacks:
                        # logger.info(f"Retrying {ticker} with {exchange} on Google Finance...")
                        # URL format: SYMBOL:EXCHANGE (e.g. ACU:NYSEAMERICAN)
                        adhoc_token = f"{ticker}:{exchange}"
                        adhoc_url = self.QUOTE_URL.format(ticker=adhoc_token)

                        html = await self._fetch_page(client, adhoc_url)
                        if html:
                            # Found it! Update URL for the record
                            url = adhoc_url
                            logger.info(f"✅ Found {ticker} on {exchange}")
                            break

                if not html:
                    logger.error(f"Failed to fetch Google Finance page for {ticker}")
                    return None

                # 5. Parse data
                metrics = self._parse_page(html, ticker)
                if metrics:
                    metrics.provider_url = url

                    # Fix Ticker format: User wants "HKG:0700" (Exchange:Symbol)
                    # The URL contains "SYMBOL:EXCHANGE" (e.g. 0700:HKG)
                    # We need to extract and swap.
                    try:
                        # Extract the part after /quote/
                        if "/quote/" in url:
                            token_part = url.split("/quote/")[1].split("?")[0]  # "0700:HKG"
                            if ":" in token_part:
                                # The format in the URL is SYMBOL:EXCHANGE
                                sym, exch = token_part.split(":")
                                # We return EXCHANGE:SYMBOL for consistency
                                metrics.ticker = f"{exch}:{sym}"
                            else:
                                metrics.ticker = token_part
                    except Exception as e:
                        logger.warning(f"Could not format Google ticker from URL {url}: {e}")

                return metrics

            except Exception as e:
                logger.error(f"Global error fetching {ticker} on Google Finance: {e}")
                import traceback

                logger.debug(traceback.format_exc())
                return None

    def get_google_ticker(self, symbol: str) -> str:
        """
        Resolves to Google Finance Internal Ticker format (Exchange:Symbol).
        Example: 'AIR.PA' -> 'EPA:AIR'
        """
        # 1. Check if already contains : (assuming it is formatted, but we want Exchange:Symbol)
        # If user passed "EPA:AIR", it is Exchange:Symbol.
        # If user passed "AIR:EPA", it is Symbol:Exchange.
        # Legacy google_ticker variable stored it as EXCHANGE:SYMBOL.
        if ":" in symbol:
            return symbol

        # 2. Handle Dots (Yahoo Style)
        if "." in symbol:
            parts = symbol.split(".")
            ticker_clean = parts[0]
            extension = parts[1]

            # Use mapping
            exchange_code = self.YAHOO_TO_GOOGLE_CODE.get(extension)

            if exchange_code:
                return f"{exchange_code}:{ticker_clean}"

            # Fallback for unknown extensions
            return f"{extension}:{ticker_clean}"

        # 3. Default US
        # Legacy logic calls for Exchange:Symbol
        return f"NASDAQ:{symbol}"

    def _normalize_ticker(self, ticker: str) -> str:
        """
        Converts inputs for URL usage: 'SYMBOL:EXCHANGE'.
        Uses get_google_ticker to get 'EXCHANGE:SYMBOL' first, then swaps.
        """
        # Get Exchange:Symbol (e.g. EPA:AIR)
        google_ticker_fmt = self.get_google_ticker(ticker)

        if ":" in google_ticker_fmt:
            parts = google_ticker_fmt.split(":")
            # Swap to Symbol:Exchange (AIR:EPA) for URL
            return f"{parts[1]}:{parts[0]}"

        return google_ticker_fmt

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> Optional[HTMLParser]:
        """Fetches the page content and returns a parser."""
        response = await self._fetch_with_retry(client, url)
        if response:
            # Check for Soft 404
            # Google Finance usually says "Your search - ... - did not match any finance results."
            # or in French "Votre recherche ... ne correspond à aucun résultat financier."
            # We should check for English and maybe generic structure?
            # "did not match any finance results" seems robust for en-US requests.
            # "match any finance results" covers it.
            # Also "couldn't find any match"
            if (
                "match any finance results" in response.text
                or "couldn't find any match" in response.text
            ):
                return None
            return HTMLParser(response.text)
        return None

    async def _fetch_with_retry(
        self, client: httpx.AsyncClient, url: str
    ) -> Optional[httpx.Response]:
        """Executes request with retry logic, backoff, and UA rotation."""
        for attempt in range(self.max_retries):
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }

            try:
                # Add random slight delay to behave more human-like
                await asyncio.sleep(random.uniform(0.1, 0.5))

                response = await client.get(url, headers=headers)

                if response.status_code == 200:
                    return response

                if response.status_code in [429, 503, 502]:
                    wait_time = (2**attempt) + random.uniform(1, 3)
                    logger.warning(
                        f"Google Finance Status {response.status_code}. Retrying in {wait_time:.2f}s..."
                    )
                    await asyncio.sleep(wait_time)
                    continue

                logger.warning(f"Error {response.status_code} fetching {url}")
                # Don't retry 404
                if response.status_code == 404:
                    return None

            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.warning(f"Network error: {e}. Retrying...")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep((2**attempt) + 1)
                else:
                    return None

        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str) -> FinancialMetrics:
        """Parses Google Finance HTML."""
        metrics = FinancialMetrics(ticker=ticker_input)

        try:
            # 1. Basic Info
            # Name is usually available in a specific H1 class or just the first H1
            h1 = parser.css_first("h1")  # Google often calls it class="zzDege" but let's be generic
            if h1:
                metrics.name = h1.text(strip=True)

            # Price (usually huge font, heuristics)
            # We don't strictly need price in FinancialMetrics base (it's in StockSummary?)
            # But let's verify if we need it. FinancialMetrics inherits StandardFinancials.
            # StockSummary has price. FinancialMetrics doesn't strictly require it but good to have.
            # Let's skip price as it wasn't explicitly requested in 'Structure des données' bullet points.

            # 2. Key Stats (P/E, Market Cap, Dividend Yield)
            # These are usually in a list like "P/E ratio 30.5"
            # We look for the labels.

            # P/E Ratio
            pe_node = self._find_stat_by_label(parser, "P/E ratio")
            if pe_node:
                metrics.pe_ratio = self._clean_number(pe_node)

            # Dividend Yield
            div_node = self._find_stat_by_label(parser, "Dividend yield")
            if div_node:
                metrics.dividend_yield = self._clean_number(div_node)  # Needs strict % handling?

            # Market Cap (not in StandardFinancials but commonly used)

            # 3. Financials Table (Revenue, Net Income, etc.)
            # Google Finance has a simplified financial table.
            # We search for "Financials" section headers.

            # Logic: Find text "Revenue" in a div/td, get the corresponding value.
            # Google tables: Row label is in one div, values in following columns.
            # Often structured as:
            # tr -> td (label) -> td (value 1) -> td (value 2)
            # OR div structures.

            # We'll stick to a heuristic:
            # Find a node containing "Quarter", "Annual", "Financials".

            # Let's look for "Revenue" specifically.
            self._extract_financial_row(parser, metrics)

            # 4. About / Description
            # Usually in a section "About"
            about_header = self._find_text_node(parser, "About")
            if about_header:
                # The description is usually in the sibling or parent's sibling
                pass

        except Exception as e:
            logger.error(f"Error parsing Google Finance page: {e}")

        return metrics

    def _find_stat_by_label(self, parser: HTMLParser, label: str) -> Optional[str]:
        """
        Finds a statistic value by its label.
        Google Finance often puts label in one div/span and value in the next one.
        """
        for node in parser.css("div, span"):
            if node.text(strip=True) == label:
                # Use heuristics to find the value
                # 1. Helper text?
                # 2. Next sibling?
                # 3. Parent's next sibling?

                # Try next sibling
                sibling = node.next
                if sibling and sibling.text(strip=True):
                    return sibling.text(strip=True)

                # Try parent's next sibling (common in grid layouts)
                if node.parent:
                    parent_sibling = node.parent.next
                    if parent_sibling:
                        # Often the value is deep inside
                        return parent_sibling.text(strip=True)

        return None

    def _find_text_node(self, parser: HTMLParser, text: str) -> Optional[Node]:
        for node in parser.css("*"):
            if text in node.text(strip=True):
                return node
        return None

    def _extract_financial_row(self, parser: HTMLParser, metrics: FinancialMetrics):
        """
        Scans for rows like 'Revenue', 'Net Income', 'Operating Margin'.
        """
        # We iterate over all text nodes or table rows.
        # Google Finance uses a lot of divs.

        # Keywords map
        keywords = {
            "revenue": ["Revenue", "Chiffre d'affaires"],
            "net_income": ["Net income", "Résultat net"],
            "operating_income": ["Operating income", "Résultat d'exploitation"],
            "operating_margin": ["Operating margin", "Marge d'exploitation"],
            "profit_margin": ["Net profit margin", "Marge nette"],
            "eps": ["Earnings per share", "BPA"],
            "ebitda": ["EBITDA"],
        }

        # Scan all text elements that look like labels (short strings)
        for node in parser.css("div, span, td"):
            txt = node.text(strip=True)
            if not txt or len(txt) > 30:
                continue

            txt_lower = txt.lower()

            for field, triggers in keywords.items():
                for trigger in triggers:
                    if trigger.lower() == txt_lower:
                        # Found a label. Try to find the value.
                        # Usually the value is to the right in the DOM (next sibling divs)

                        # In Google Finance tables (modern), it's often:
                        # <tr><td>Label</td><td>Value</td>...</tr>
                        # or flexbox.

                        val = self._find_value_nearby(node)
                        if val is not None:
                            # Assign to metrics
                            if field == "revenue":
                                metrics.revenue = val
                            elif field == "net_income":
                                metrics.net_income = val
                            elif field == "operating_margin":
                                metrics.operating_margin = val
                            elif field == "profit_margin":
                                metrics.profit_margin = val
                            elif field == "eps":
                                metrics.eps = val
                            elif field == "ebitda":
                                metrics.ebitda = val

    def _find_value_nearby(self, label_node: Node) -> Optional[float]:
        """
        Looks for a numeric value in siblings or next elements.
        """
        # 1. Check siblings
        curr = label_node.next
        for _ in range(5):  # Check next 5 siblings
            if not curr:
                break
            txt = curr.text(strip=True)
            val = self._clean_number(txt)
            if val is not None:
                return val
            curr = curr.next

        # 2. Check Parent Siblings (if label is wrapped in a div)
        parent = label_node.parent
        if parent:
            curr = parent.next
            for _ in range(5):
                if not curr:
                    break
                # Only check text content, deeper search might be needed
                txt = curr.text(strip=True)
                val = self._clean_number(txt)
                if val is not None:
                    return val
                curr = curr.next

        return None

    def _clean_number(self, text: str) -> Optional[float]:
        """Parses "1.23B", "5.67%", "1,234.56"."""
        if not text:
            return None

        # Remove currency symbols text like "USD", "EUR"
        clean = re.sub(r"[^\d.,\-MBTK%]", "", text.upper())  # Keep M, B, T, K for scale

        if not clean:
            return None

        # Multipliers
        multiplier = 1.0
        if "B" in clean:
            multiplier = 1_000_000_000
            clean = clean.replace("B", "")
        elif "M" in clean:
            multiplier = 1_000_000
            clean = clean.replace("M", "")
        elif "T" in clean:
            multiplier = 1_000_000_000_000
            clean = clean.replace("T", "")
        elif "K" in clean:
            multiplier = 1_000
            clean = clean.replace("K", "")
        elif "%" in clean:
            multiplier = 1.0  # Standard is already unit? Or usually 0.01?
            # In data models, margins are often whole numbers or ratios.
            # If 5.56%, we might return 5.56
            clean = clean.replace("%", "")

        # Format handling
        # US/European mix. Google Finance usually adapts to locale, but scraping requests usually get US English (en-US header).
        # "1,234.56" -> 1234.56
        clean = clean.replace(",", "")

        try:
            return float(clean) * multiplier
        except ValueError:
            return None
