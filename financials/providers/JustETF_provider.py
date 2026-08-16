"""
JustETF Provider - Professional ETF data extraction from JustETF.com
Provides comprehensive ETF analysis including fees, holdings, and fund size data.
"""

import logging
import os
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup
from lxml import etree

# Import dependencies with proper fallback handling
try:
    from moning.Request import getRequest
    from moning.ToolsBox import (
        REGEX_JUSTEFT_CANONICAL,
        REGEX_JUSTETF_LOGO,
        REGEX_JUSTETF_LOGO_LOCAL,
        ToolsBox,
    )
except ImportError:
    try:
        from Request import getRequest
        from ToolsBox import (
            REGEX_JUSTEFT_CANONICAL,
            REGEX_JUSTETF_LOGO,
            REGEX_JUSTETF_LOGO_LOCAL,
            ToolsBox,
        )
    except ImportError:
        try:
            # Try to import from fundamental.tools
            from fundamental.tools.Request import getRequest

            from fundamental.tools.ToolsBox import (
                REGEX_JUSTEFT_CANONICAL,
                REGEX_JUSTETF_LOGO,
                REGEX_JUSTETF_LOGO_LOCAL,
                ToolsBox,
            )
        except ImportError:
            # Try one more relative import path
            try:
                from ..tools.Request import getRequest
                from ..tools.ToolsBox import (
                    REGEX_JUSTEFT_CANONICAL,
                    REGEX_JUSTETF_LOGO,
                    REGEX_JUSTETF_LOGO_LOCAL,
                    ToolsBox,
                )
            except ImportError:
                # Fallback for MCP server usage
                ToolsBox = None
                getRequest = None
                REGEX_JUSTEFT_CANONICAL = None
                REGEX_JUSTETF_LOGO = None
                REGEX_JUSTETF_LOGO_LOCAL = None

# Configure logging
logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://www.justetf.com"
ETF_PROFILE_URL = f"{BASE_URL}/etf-profile.html"

# Initialize tools if available
tools = ToolsBox() if ToolsBox else None


class JustETFScraper:
    """Professional ETF data scraper for JustETF.com"""

    def __init__(self):
        self.tools = tools
        self.base_url = BASE_URL

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
        if not os.path.exists(root_path):
            logger.warning(f"Local files path does not exist: {root_path}")
            return False

        if not isin:
            logger.warning("No ISIN provided for local file search")
            return False

        try:
            entries = os.listdir(root_path)
            for entry in entries:
                if isin in entry:
                    file_path = os.path.join(root_path, entry)
                    logger.info(f"Processing local file: {file_path}")

                    with open(file_path, encoding="utf-8") as file:
                        soup = BeautifulSoup(file, "html.parser")

                        if self._process_etf_page(soup, isin, etf_data, is_local=True):
                            return True

            logger.info(f"No matching local file found for ISIN: {isin}")
            return False

        except Exception as e:
            logger.error(f"Error processing local files: {str(e)}")
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
        if not getRequest:
            logger.error("getRequest function not available")
            return etf_data

        if not isin:
            logger.warning("No ISIN provided for web scraping")
            return etf_data

        url = f"{ETF_PROFILE_URL}?isin={isin}"
        logger.info(f"Scraping JustETF URL: {url}")

        try:
            response = getRequest(url)
            if response.status_code == 200:
                etf_data["justETF_url"] = url
                soup = BeautifulSoup(response.text, "html.parser")
                self._process_etf_page(soup, isin, etf_data, is_local=False)
                logger.info(f"Successfully scraped data for ISIN: {isin}")
            else:
                logger.error(f"Failed to fetch JustETF page. Status code: {response.status_code}")

        except Exception as e:
            logger.error(f"Error scraping JustETF website: {str(e)}")

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
        if not self.tools or not REGEX_JUSTEFT_CANONICAL:
            return True  # Skip validation if tools not available

        canonical_link = soup.find("link", rel="canonical")
        if canonical_link:
            canonical_url = self.tools.extractAnySetence(canonical_link, REGEX_JUSTEFT_CANONICAL)
            if canonical_url and "isin=" in canonical_url:
                extracted_isin = (
                    canonical_url.split("isin=")[1] if len(canonical_url.split("isin=")) > 1 else ""
                )
                return extracted_isin == expected_isin
        return False

    def _extract_etf_name(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract ETF name from the page"""
        try:
            if not etf_data.get("long_name"):
                name_elements = dom.xpath('//*[@id="etf-title"]/text()')
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
                import re

                match = re.search(r"url\(['\"]?(.*?)['\"]?\)", logo_div["style"])
                if match:
                    logo_url = match.group(1)
                    etf_data["logo"] = (
                        logo_url if logo_url.startswith("http") else f"{self.base_url}{logo_url}"
                    )
                    logger.debug(f"Extracted logo: {etf_data['logo']}")
                    return

            # Fallback to older mechanism (REGEX) if not found
            if self.tools:
                if is_local and REGEX_JUSTETF_LOGO_LOCAL:
                    body = soup.find("body")
                    if body:
                        logo = self.tools.extractAnySetence(str(body), REGEX_JUSTETF_LOGO_LOCAL)
                        if logo:
                            etf_data["logo"] = f"{self.base_url}{logo}"
                elif not is_local and REGEX_JUSTETF_LOGO:
                    logo = self.tools.extractAnySetence(str(soup), REGEX_JUSTETF_LOGO)
                    if logo:
                        etf_data["logo"] = f"{self.base_url}{logo}"

                if etf_data.get("logo"):
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
                # Map German/English terms to standardized values
                policy_mapping = {
                    "Ausschüttend": "Distribution",
                    "Distribution": "Distribution",
                    "Distributing": "Distribution",
                    "Thesaurierend": "Capitalisation",
                    "Accumulating": "Capitalisation",
                    "Capitalisation": "Capitalisation",
                }
                etf_data["dividendsPolicy_etf"] = policy_mapping.get(policy, policy)
                logger.debug(f"Extracted dividends policy: {etf_data['dividendsPolicy_etf']}")
            else:
                etf_data["dividendsPolicy_etf"] = ""
        except Exception as e:
            logger.warning(f"Failed to extract dividends policy: {str(e)}")
            etf_data["dividendsPolicy_etf"] = ""

    def _extract_fees(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract ETF fees as float value"""
        try:
            fee_elements = dom.xpath('//*[@data-testid="etf-profile-header_ter-value"]//text()')
            if fee_elements:
                fee_text = "".join(fee_elements)
                # Clean and convert fee text to float
                fee_clean = (
                    fee_text.lower()
                    .replace("p.a.", "")
                    .replace(" ", "")
                    .replace("\n", "")
                    .replace("%", "")
                    .replace(",", ".")
                    .strip()
                )
                etf_data["fees_etf"] = float(fee_clean) if fee_clean else 0.0
                logger.debug(f"Extracted fees: {etf_data['fees_etf']}%")
            else:
                etf_data["fees_etf"] = 0.0
        except (ValueError, IndexError) as e:
            logger.warning(f"Failed to extract fees: {str(e)}")
            etf_data["fees_etf"] = 0.0

    def _extract_holding_count(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract number of holdings in the ETF"""
        try:
            holding_elements = dom.xpath(
                '//*[@data-testid="etf-profile-header_holdings-value"]//text()'
            )
            if holding_elements:
                holding_text = (
                    "".join(holding_elements)
                    .replace(" ", "")
                    .replace("\n", "")
                    .replace(",", "")
                    .replace(".", "")
                    .strip()
                )
                etf_data["holdingCount_etf"] = int(holding_text) if holding_text.isdigit() else 0
                logger.debug(f"Extracted holding count: {etf_data['holdingCount_etf']}")
            else:
                etf_data["holdingCount_etf"] = 0
        except (ValueError, IndexError) as e:
            logger.warning(f"Failed to extract holding count: {str(e)}")
            etf_data["holdingCount_etf"] = 0

    def _extract_fund_size(self, dom: etree._Element, etf_data: Dict[str, Any]) -> None:
        """Extract fund size with proper formatting"""
        try:
            size_elements = dom.xpath(
                '//*[@data-testid="etf-profile-header_fund-size-value-wrapper"]//text()'
            )
            if size_elements:
                size_text = "".join(size_elements).strip()
                # Remove common currency prefixes
                for curr in ["EUR", "USD", "GBP", "CHF", "JPY"]:
                    size_text = size_text.replace(curr, "").strip()
                # Ensure spacing format matches expectations (e.g. "743 M" or "1 234 Mio")
                etf_data["fundSize_etf"] = size_text.replace(".", " ")
                logger.debug(f"Extracted fund size: {etf_data['fundSize_etf']}")
            else:
                etf_data["fundSize_etf"] = ""
        except Exception as e:
            logger.warning(f"Failed to extract fund size: {str(e)}")
            etf_data["fundSize_etf"] = ""


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
