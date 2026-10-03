"""
JustETF Provider - Professional ETF data extraction from JustETF.com
Provides comprehensive ETF analysis including fees, holdings, and fund size data.

The scraper reads an ETF profile page either from local HTML files (pages saved
beforehand, one per ISIN) or from the website. It is synchronous and independent
from the FastAPI application, which uses :mod:`financials.providers.justetf`.
"""

import logging
import os
import re
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, quote, urlparse

import httpx
from bs4 import BeautifulSoup
from lxml import etree

from fundamental.tools.ToolsBox import (
    REGEX_JUSTETF_LOGO,
    REGEX_JUSTETF_LOGO_LOCAL,
    ToolsBox,
)

# Configure logging
logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://www.justetf.com"
# The language is explicit: without it the site redirects according to the visitor's
# country, and labels and number formats then change from one run to the next.
ETF_PROFILE_URL = f"{BASE_URL}/en/etf-profile.html"
REQUEST_TIMEOUT = 15.0
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Distribution policy labels served by the site, by language.
DIVIDENDS_POLICY_LABELS = {
    "ausschüttend": "Distribution",
    "distribution": "Distribution",
    "distributing": "Distribution",
    "thesaurierend": "Capitalisation",
    "accumulating": "Capitalisation",
    "capitalisation": "Capitalisation",
}

# Any whitespace used as a thousands separator, including (narrow) no-break spaces.
_SPACES = r"\s  "
_FUND_SIZE_PATTERN = re.compile(
    rf"^(?:(?P<currency>[A-Z]{{3}})[{_SPACES}]+)?(?P<amount>\d[\d.,{_SPACES}]*)(?P<unit>[^\d]*)$"
)
_MILLION_UNITS = {"m", "mio", "mio."}

# Initialize tools
tools = ToolsBox()


def getRequest(url: str) -> httpx.Response:
    """Fetch a page of the website (default HTTP client of the scraper)."""
    with httpx.Client(
        timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=REQUEST_HEADERS
    ) as client:
        return client.get(url)


class JustETFScraper:
    """Professional ETF data scraper for JustETF.com"""

    def __init__(self, fetch: Optional[Callable[[str], Any]] = None):
        """
        Args:
            fetch: Optional replacement for the HTTP call. It receives a URL and returns
                an object exposing ``status_code`` and ``text``.
        """
        self.tools = tools
        self.base_url = BASE_URL
        self._fetch = fetch

    def extract_etf_data(self, isin: str, root_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Extract comprehensive ETF data from JustETF

        Args:
            isin: ISIN of the ETF to extract data for
            root_path: Optional path to local JustETF files

        Returns:
            Dictionary with JustETF data
        """
        etf_data = {"isin": isin}

        try:
            # Try local files first if path provided
            if root_path and self._extract_from_local_files(isin, etf_data, root_path):
                logger.info(f"Successfully extracted data from local files for ISIN: {isin}")
                return etf_data

            # Fallback to web scraping
            return self._extract_from_web(isin, etf_data)

        except Exception as e:
            logger.error(f"Error extracting ETF data: {str(e)}")
            return etf_data

    def _extract_from_local_files(
        self, isin: str, etf_data: Dict[str, Any], root_path: str
    ) -> bool:
        """
        Extract ETF data from local HTML files

        Args:
            isin: ETF ISIN
            etf_data: Dictionary to populate with data
            root_path: Path to directory containing JustETF HTML files

        Returns:
            True if data was successfully extracted from local files
        """
        if not os.path.isdir(root_path):
            logger.warning(f"Local files path does not exist: {root_path}")
            return False

        if not isin:
            logger.warning("No ISIN provided for local file search")
            return False

        for entry in sorted(os.listdir(root_path)):
            file_path = os.path.join(root_path, entry)
            if isin not in entry or not os.path.isfile(file_path):
                continue

            logger.info(f"Processing local file: {file_path}")
            try:
                with open(file_path, encoding="utf-8") as file:
                    soup = BeautifulSoup(file, "html.parser")
            except (OSError, UnicodeDecodeError) as e:
                logger.warning(f"Cannot read local file {file_path}: {str(e)}")
                continue

            # A file of another ETF must not leave partial data behind.
            page_data: Dict[str, Any] = {"isin": isin}
            if self._process_etf_page(soup, isin, page_data, is_local=True):
                etf_data.update(page_data)
                return True

        logger.info(f"No matching local file found for ISIN: {isin}")
        return False

    def _extract_from_web(self, isin: str, etf_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract ETF data from JustETF website

        Args:
            isin: ETF ISIN
            etf_data: Dictionary to populate with data

        Returns:
            Updated dictionary with web-scraped data
        """
        if not isin:
            logger.warning("No ISIN provided for web scraping")
            return etf_data

        url = f"{ETF_PROFILE_URL}?isin={quote(isin.strip(), safe='')}"
        logger.info(f"Scraping JustETF URL: {url}")

        try:
            response = (self._fetch or getRequest)(url)
        except httpx.HTTPError as e:
            logger.error(f"Error scraping JustETF website: {str(e)}")
            return etf_data

        if response.status_code != 200:
            logger.error(f"Failed to fetch JustETF page. Status code: {response.status_code}")
            return etf_data

        soup = BeautifulSoup(response.text, "html.parser")
        page_data: Dict[str, Any] = {"isin": isin}
        # An unknown ISIN is answered with another page (search or home): the profile
        # must be the one that was asked for.
        if self._validate_isin_match(soup, isin) and self._process_etf_page(
            soup, isin, page_data, is_local=False
        ):
            etf_data.update(page_data)
            etf_data["justETF_url"] = url
            logger.info(f"Successfully scraped data for ISIN: {isin}")
        else:
            logger.warning(f"JustETF did not return the profile page of ISIN: {isin}")

        return etf_data

    def _process_etf_page(
        self, soup: BeautifulSoup, isin: str, etf_data: Dict[str, Any], is_local: bool = False
    ) -> bool:
        """
        Process ETF page content and extract relevant data

        Args:
            soup: BeautifulSoup object of the page
            isin: ETF ISIN
            etf_data: Dictionary to populate with data
            is_local: Whether processing local file or web content

        Returns:
            True if processing was successful
        """
        try:
            # Validate ISIN if processing local file
            if is_local and not self._validate_isin_match(soup, isin):
                return False

            dom = etree.HTML(str(soup))
            if dom is None:
                return False

            # Extract all ETF data
            self._extract_etf_name(dom, etf_data)
            self._extract_logo(soup, etf_data, is_local)
            self._extract_fact_sheet_pdf(dom, etf_data)
            self._extract_dividends_policy(dom, etf_data)
            self._extract_fees(dom, etf_data)
            self._extract_holding_count(dom, etf_data)
            self._extract_fund_size(dom, etf_data)

            return True

        except Exception as e:
            logger.error(f"Error processing ETF page: {str(e)}")
            return False

    def _validate_isin_match(self, soup: BeautifulSoup, expected_isin: str) -> bool:
        """Validate that the page corresponds to the expected ISIN"""
        expected = (expected_isin or "").strip().upper()
        if not expected:
            return False

        # 1. Canonical URL: .../etf-profile.html?isin=IE00B4L5Y983
        canonical_link = soup.find("link", rel="canonical")
        href = canonical_link.get("href") if canonical_link else None
        if href:
            page_isin = parse_qs(urlparse(href).query).get("isin", [""])[0]
            if page_isin:
                return page_isin.strip().upper() == expected

        # 2. ISIN displayed in the profile header
        isin_node = soup.find(attrs={"data-testid": "etf-profile-header_isin-value"})
        if isin_node:
            return isin_node.get_text(strip=True).upper() == expected

        return False

    def _extract_etf_name(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract ETF name from the page"""
        try:
            if not etf_data.get("long_name"):
                name_elements = dom.xpath('//*[@id="etf-title"]/text()') or dom.xpath(
                    '//*[@data-testid="etf-profile-header_etf-name"]/text()'
                )
                if name_elements:
                    etf_data["long_name"] = name_elements[0].strip()
                    logger.debug(f"Extracted ETF name: {etf_data['long_name']}")
        except Exception as e:
            logger.warning(f"Failed to extract ETF name: {str(e)}")

    def _extract_logo(self, soup: BeautifulSoup, etf_data: Dict[str, Any], is_local: bool) -> None:
        """Extract ETF logo URL"""
        try:
            # Modern robust approach: Find the specific logo div using data-testid
            logo_div = soup.find(attrs={"data-testid": "etf-profile-header_provider-logo-image"})
            if logo_div and logo_div.has_attr("style"):
                match = re.search(r"url\(['\"]?(.*?)['\"]?\)", logo_div["style"])
                if match:
                    logo_url = match.group(1)
                    etf_data["logo"] = (
                        logo_url if logo_url.startswith("http") else f"{self.base_url}{logo_url}"
                    )
                    logger.debug(f"Extracted logo: {etf_data['logo']}")
                    return

            # Fallback to older mechanism (REGEX) if not found
            if is_local:
                body = soup.find("body")
                logo = (
                    self.tools.extractAnySetence(str(body), REGEX_JUSTETF_LOGO_LOCAL)
                    if body
                    else ""
                )
            else:
                logo = self.tools.extractAnySetence(str(soup), REGEX_JUSTETF_LOGO)

            if logo:
                etf_data["logo"] = f"{self.base_url}{logo}"
                logger.debug(f"Extracted logo (fallback): {etf_data['logo']}")
        except Exception as e:
            logger.warning(f"Failed to extract logo: {str(e)}")

    def _extract_fact_sheet_pdf(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract fact sheet PDF URL"""
        try:
            pdf_elements = dom.xpath(
                '//a[@data-testid="etf-documents-panel_item-link" and (contains(translate(@title, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "factsheet") or contains(translate(@title, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "fact-sheet") or contains(translate(@title, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "fiche"))]/@href'
            )
            etf_data["justETF_pdf_url"] = pdf_elements[0] if pdf_elements else ""
            if etf_data["justETF_pdf_url"]:
                logger.debug(f"Extracted PDF URL: {etf_data['justETF_pdf_url']}")
        except Exception as e:
            logger.warning(f"Failed to extract PDF URL: {str(e)}")
            etf_data["justETF_pdf_url"] = ""

    def _extract_dividends_policy(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract dividends policy (Distribution/Capitalisation)"""
        try:
            policy_elements = dom.xpath(
                '//*[@data-testid="etf-profile-header_distribution-policy-value"]//text()'
            )
            if policy_elements:
                policy = "".join(policy_elements).strip()
                # Map German/English/French terms to standardized values
                etf_data["dividendsPolicy_etf"] = DIVIDENDS_POLICY_LABELS.get(
                    policy.lower(), policy
                )
                logger.debug(f"Extracted dividends policy: {etf_data['dividendsPolicy_etf']}")
            else:
                etf_data["dividendsPolicy_etf"] = ""
        except Exception as e:
            logger.warning(f"Failed to extract dividends policy: {str(e)}")
            etf_data["dividendsPolicy_etf"] = ""

    def _extract_fees(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract ETF fees as float value"""
        fee_elements = dom.xpath('//*[@data-testid="etf-profile-header_ter-value"]//text()')
        # "0.20% p.a." (English) or "0,20% p.a." (French, German)
        match = re.search(r"\d+(?:[.,]\d+)?", "".join(fee_elements))
        etf_data["fees_etf"] = float(match.group(0).replace(",", ".")) if match else 0.0
        if match:
            logger.debug(f"Extracted fees: {etf_data['fees_etf']}%")

    def _extract_holding_count(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract number of holdings in the ETF"""
        holding_elements = dom.xpath(
            '//*[@data-testid="etf-profile-header_holdings-value"]//text()'
        )
        # "1,252" (English), "1 252" with a narrow no-break space (French), "1.252" (German)
        holding_text = re.sub(rf"[{_SPACES}.,]", "", "".join(holding_elements))
        etf_data["holdingCount_etf"] = int(holding_text) if holding_text.isdigit() else 0
        if etf_data["holdingCount_etf"]:
            logger.debug(f"Extracted holding count: {etf_data['holdingCount_etf']}")

    def _extract_fund_size(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract fund size with proper formatting

        ``fundSize_etf`` keeps the historical text format ("743 M", "1 234 Mio").
        ``fundSizeMillions_etf`` and ``fundSizeCurrency_etf`` give the same amount as
        a number of millions and its currency, when the page states them.
        """
        etf_data["fundSize_etf"] = ""
        etf_data["fundSizeMillions_etf"] = None
        etf_data["fundSizeCurrency_etf"] = None

        size_elements = dom.xpath(
            '//*[@data-testid="etf-profile-header_fund-size-value-wrapper"]//text()'
        )
        size_text = re.sub(rf"[{_SPACES}]+", " ", "".join(size_elements)).strip()
        if not size_text:
            return

        match = _FUND_SIZE_PATTERN.match(size_text)
        if not match:
            etf_data["fundSize_etf"] = size_text
            return

        # "EUR 129,628 m", "EUR 129 628 M" and "EUR 129.628 Mio." all become "129 628 <unit>"
        groups = [group for group in re.split(rf"[{_SPACES}.,]+", match.group("amount")) if group]
        unit = match.group("unit").strip()
        etf_data["fundSize_etf"] = " ".join([*groups, unit]).strip()
        etf_data["fundSizeCurrency_etf"] = match.group("currency")
        if unit.lower() in _MILLION_UNITS:
            etf_data["fundSizeMillions_etf"] = int("".join(groups))
        logger.debug(f"Extracted fund size: {etf_data['fundSize_etf']}")


# Public API functions for backward compatibility
def justETFScraping(isin: str, rootPathScrapJustETFFiles: Optional[str] = None) -> Dict[str, Any]:
    """
    Function for ETF scraping.

    Args:
        isin: ETF ISIN
        rootPathScrapJustETFFiles: Optional path to local JustETF files

    Returns:
        Dictionary with JustETF data
    """
    scraper = JustETFScraper()
    return scraper.extract_etf_data(isin, rootPathScrapJustETFFiles)


def justETFWebScraping(isin: str) -> Dict[str, Any]:
    """
    Function for web scraping.

    Args:
        isin: ETF ISIN

    Returns:
        Dictionary with web-scraped JustETF data
    """
    scraper = JustETFScraper()
    return scraper.extract_etf_data(isin)
