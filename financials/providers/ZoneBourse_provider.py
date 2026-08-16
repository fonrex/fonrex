import asyncio
import logging
import random
import re
from typing import List, Optional

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


class ZoneBourseProvider(BaseProvider):
    """
    Modern, asynchronous provider for ZoneBourse using httpx and selectolax.
    Features:
    - Async fetching with httpx
    - Fast parsing with selectolax
    - User-Agent rotation
    - Retries with exponential backoff
    - Data mapping to FinancialMetrics
    """

    BASE_URL = "https://www.zonebourse.com"
    SEARCH_URL = "https://www.zonebourse.com/recherche/?q={symbol}"

    def __init__(self, max_retries: int = 3, timeout: int = 10, concurrent_requests: int = 5):
        self.max_retries = max_retries
        self.timeout = timeout
        # Semaphore for concurrency control if needed internally, though typically handled by caller
        self.semaphore = asyncio.Semaphore(concurrent_requests)

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """
        Fetches financial data for a given ticker or ISIN.
        """
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                # 1. Search for the security URL
                url = await self._search_symbol(client, ticker)
                if not url:
                    logger.warning(f"Ticker/ISIN {ticker} not found on ZoneBourse")
                    return None

                # 2. Fetch the profile page
                html = await self._fetch_page(client, url)
                if not html:
                    logger.error(f"Failed to fetch profile page for {ticker}")
                    return None

                # 3. Parse data
                return self._parse_page(html, ticker, url)

            except Exception as e:
                logger.error(f"Global error fetching {ticker}: {e}")
                import traceback

                logger.debug(traceback.format_exc())
                return None

    async def _search_symbol(self, client: httpx.AsyncClient, symbol: str) -> Optional[str]:
        """
        Searches for the symbol/ISIN using the internal async search API and returns the profile URL.
        """
        url = f"{self.BASE_URL}/async/search/quick"

        # Generate random tokens/IDs as requested
        import string

        def random_string(length=10):
            return "".join(random.choices(string.ascii_letters + string.digits, k=length))

        def random_hex(length=32):
            return "".join(random.choices("0123456789abcdef", k=length))

        # Randomize payload values
        payload = {
            "search": symbol,
            "type": "company",
            "search-type": random_string(20),  # Random value
            "token": random_string(50) + "." + random_string(40),  # Mock token structure
        }

        # Randomize headers
        headers = {
            "Host": "www.zonebourse.com",
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "*/*",
            "Accept-Language": "fr,fr-FR;q=0.8,en-US;q=0.5,en;q=0.3",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            # Mock cookies
            "Cookie": f'PHPSESSID={random_hex(26)}; pv_r0_rand=16; pv_r0_date=2025-12-31; pv_r0=1; g_state={{"i_l":1}}; _lr_geo_location=CA',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        try:
            logger.debug(f"Searching for {symbol} via POST {url}")
            response = await client.post(url, data=payload, headers=headers)

            if response.status_code != 200:
                logger.warning(f"Search failed with status {response.status_code}")
                return None

            data = response.json()
            if not data or not isinstance(data, dict):
                logger.warning("Invalid JSON response from search")
                return None

            html_content = data.get("data", "")
            if not html_content:
                logger.warning("No HTML content in search response")
                return None

            # Parse the returned HTML fragment
            parser = HTMLParser(html_content)

            # Find the first row with data-href
            first_row = parser.css_first("tr[data-href]")
            if first_row:
                href = first_row.attributes.get("data-href")
                if href:
                    full_url = f"{self.BASE_URL}{href}" if href.startswith("/") else href
                    logger.info(f"Found URL for {symbol}: {full_url}")
                    return full_url

            logger.warning(f"No matching symbol found in search results for {symbol}")
            return None

        except Exception as e:
            logger.error(f"Error searching symbol {symbol}: {e}")
            return None

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> Optional[HTMLParser]:
        """Fetches the page content and returns a parser."""
        response = await self._fetch_with_retry(client, url)
        if response:
            return HTMLParser(response.text)
        return None

    async def _fetch_with_retry(
        self, client: httpx.AsyncClient, url: str
    ) -> Optional[httpx.Response]:
        """Executes request with retry logic, backoff, and UA rotation."""
        for attempt in range(self.max_retries):
            # Rotate User-Agent
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }

            try:
                # Use semaphore if we were calling this rapidly, but here we just do one request sequence per get_financials.
                logger.debug(f"Fetching {url} (Attempt {attempt + 1}/{self.max_retries})")
                response = await client.get(url, headers=headers)

                if response.status_code == 200:
                    return response

                # Handle Rate Limits and Server Errors
                if response.status_code in [429, 503, 502, 504]:
                    wait_time = (2**attempt) + random.uniform(0.5, 1.5)
                    logger.warning(
                        f"Status {response.status_code}. Retrying in {wait_time:.2f}s..."
                    )
                    await asyncio.sleep(wait_time)
                    continue

                # Handle other client errors (404, etc) - No retry
                if 400 <= response.status_code < 500:
                    logger.warning(f"Client error {response.status_code} for {url}. No retry.")
                    return None

            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.warning(f"Network error: {e}. Retrying...")
                if attempt < self.max_retries - 1:
                    wait_time = (2**attempt) + random.uniform(0.5, 1.5)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Max retries reached for {url}")
                    return None

        return None

    def _parse_page(self, parser: HTMLParser, ticker_input: str, url: str) -> FinancialMetrics:
        """Parses the HTML to extract financial metrics."""
        metrics = FinancialMetrics(ticker=ticker_input)
        metrics.provider_url = url

        try:
            # 1. Basic Info (ISIN, Name, Sector)
            # Using generic strategies as specific classes change.
            # Looking for h1/h2 relevant text.

            # Header often contains the name
            h1 = parser.css_first("h1")
            if h1:
                metrics.name = h1.text(strip=True)

            # Extract ISIN - usually labeled
            # Try to find text "ISIN:" nearby
            metrics.ticker = self._extract_labeled_value(parser, "ISIN") or ticker_input

            # Sector
            metrics.sector = self._extract_labeled_value(
                parser, "Secteur"
            ) or self._extract_labeled_value(parser, "Sector")

            # 2. Ratings & Consensus
            # Trading Rating / Investment Rating often in a 'ratings' section.
            # We look for container with 'rating' class or id.
            # Old code used: div#ratings

            # Consensus Mean
            # Old code: div#consensusDetail...
            consensus_node = parser.css_first("div#consensusDetail")
            if consensus_node:
                # Consensus markup is detected here; extraction remains provider-specific.
                logger.debug("Consensus section detected for %s", ticker_input)

            # ESG Score
            # Old code: div.esg-rank
            esg_node = parser.css_first(".esg-rank")
            if esg_node:
                metrics.esg_score = esg_node.text(strip=True)

            # 3. Financial Data (Revenue, PE, etc.)
            # Extract from financial tables (often present on the summary page or a tab)
            # We assume the summary page has some key figures table.
            self._extract_financial_table_data(parser, metrics)

            # 4. Analyst Count / Recommendation
            # Often in the consensus section

        except Exception as e:
            logger.error(f"Error parsing page: {e}")

        return metrics

    def _extract_labeled_value(self, parser: HTMLParser, label: str) -> Optional[str]:
        """
        Helper to find a value associated with a label using text search.
        E.g. find node with text "ISIN", then get next sibling or child.
        """
        # Strategy: Find any element containing the label
        # This is expensive for the whole doc, maybe limit scope?
        # For now, searching common metadata containers if possible, else global.

        # Selectolax doesn't implement :contains easily. We iterate promising candidates.
        # Check standard metadata lists
        candidates = parser.css("li, td, div, span")
        for node in candidates:
            # We want 'leaf' nodes or small nodes roughly
            txt = node.text(strip=True)
            if label in txt and len(txt) < 100:
                # If label is "ISIN: FR0000...", we split
                if ":" in txt:
                    parts = txt.split(":")
                    if label in parts[0]:
                        return parts[1].strip()
                # If label is its own node, maybe the value is next sibling
                # Not easily accessible in selectolax without parent iter.
        return None

    def _extract_financial_table_data(self, parser: HTMLParser, metrics: FinancialMetrics):
        """
        Scans all tables for financial keywords and extracts data.
        """
        for table in parser.css("table"):
            rows = table.css("tr")
            if not rows:
                continue

            # Check header to see if it's a financial table (Years?)
            # Heuristic: headers look like years (2023, 2024...)

            for row in rows:
                cells = row.css("td")
                header = row.css_first("th")

                # Determine label
                label = ""
                if header:
                    label = header.text(strip=True)
                elif cells:
                    label = cells[0].text(strip=True)  # First cell effectively header
                else:
                    continue

                if not label:
                    continue

                # Get the first numeric value
                # We iterate values to find the first valid number (often current year/estimate)
                idx_offset = 0 if header else 1  # If first cell was label, skip it

                relevant_cells = row.css("td")[idx_offset:] if header else cells[1:]

                # Helper to set if not already set
                current_val = self._find_first_number(relevant_cells)

                if current_val is not None:
                    label_lower = label.lower()

                    if "chiffre d'affaires" in label_lower or "net sales" in label_lower:
                        metrics.revenue = current_val
                    elif "résultat net" in label_lower or "net income" in label_lower:
                        metrics.net_income = current_val
                    elif "bna" in label_lower or "bpa" in label_lower or "eps" in label_lower:
                        metrics.eps = current_val
                    elif "per" in label_lower or "p/e" in label_lower:
                        metrics.pe_ratio = current_val
                    elif "rendement" in label_lower or "yield" in label_lower:
                        # Sometimes yield is %, handle in helper
                        metrics.dividend_yield = current_val  # Assuming handled in cleaning
                    elif "ebitda" in label_lower:
                        metrics.ebitda = current_val
                    elif "dette nette" in label_lower or "net debt" in label_lower:
                        # Mapping to debt_to_equity if strictly ratio, but here it's likely absolute.
                        # FinancialMetrics has debt_to_equity.
                        # We might need to compute it or find the ratio line.
                        pass
                    elif "marge d'exploitation" in label_lower or "operating margin" in label_lower:
                        metrics.operating_margin = current_val
                    elif "marge nette" in label_lower or "net margin" in label_lower:
                        metrics.profit_margin = current_val

    def _find_first_number(self, cells: List[Node]) -> Optional[float]:
        """Iterates cells to find the first valid float."""
        for cell in cells:
            txt = cell.text(strip=True)
            val = self._clean_number(txt)
            if val is not None:
                return val
        return None

    def _clean_number(self, text: str) -> Optional[float]:
        """Converts French/US number formats to float."""
        if not text or text == "-":
            return None
        try:
            # Remove currency symbols and percentages
            clean = re.sub(r"[€$%\sA-Za-z]", "", text)
            # Replace comma with dot for decimals (French format handle)
            # But be careful with thousands separators.
            # If text was "1 234,56", after remove space: "1234,56" -> replace , with . -> 1234.56
            # If text was "1,234.56" (US), after remove space: "1,234.56" -> remove , -> 1234.56

            # Simple heuristic for ZoneBourse (usually French format with spaces and commas)
            # Replaces spaces first.
            clean = clean.replace(",", ".")

            # Handle multiple dots?
            if clean.count(".") > 1:
                # Possible thousands separator used as dot?
                # or just remove all but last.
                pass

            if clean:
                return float(clean)
        except ValueError:
            pass
        return None

    def _extract_number_from_text(self, text: str) -> Optional[float]:
        """Extracts the first number found in a text string."""
        if not text:
            return None
        matches = re.findall(r"(\d+[.,]\d+)", text)
        if matches:
            return self._clean_number(matches[0])
        return None
