import logging
import random
from typing import Any, Dict, Optional

import httpx
from selectolax.parser import HTMLParser

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


class MorningStarProvider:
    """
    MorningStar Provider.
    Professional data extraction from Morningstar.fr
    """

    SEARCH_API = "https://www.morningstar.fr/fr/util/SecuritySearch.ashx"

    def __init__(self, max_retries: int = 3, timeout: int = 15):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Extract financial metrics based purely on ISIN.
        """
        if not isin:
            logger.warning("No ISIN provided for MorningStar search.")
            return None

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            ms_id = await self._search_id(client, isin)
            if not ms_id:
                logger.warning(f"Could not find MorningStar internal ID for ISIN: {isin}")
                return None

            # Construct URL
            url = f"https://tools.morningstar.fr/fr/stockreport/default.aspx?Site=fr&id={ms_id}&LanguageId=fr-FR&SecurityToken={ms_id}]3]0]E0WWE$$ALL"
            pdf_url = f"https://tools.morningstar.fr/fr/util/documentproxy.aspx?key=EquityQuant&secId={ms_id}"
            # Base data we know even if HTML fetch fails
            base_data = {
                "isin": isin,
                "morningstar_id": ms_id,
                "provider_url": url,
                "moringstar_pdf_url": pdf_url,
            }

            logger.info(f"Scraping MorningStar URL for {isin}: {url}")

            try:
                resp = await client.get(url, headers={"User-Agent": random.choice(USER_AGENTS)})
                if resp.status_code in [200, 202] and len(resp.text) > 0:
                    parsed_data = self._parse_page(HTMLParser(resp.text), isin, url)
                    if parsed_data:
                        base_data.update(parsed_data)
                else:
                    logger.warning(
                        f"Failed to fetch MorningStar page or empty. Status code: {resp.status_code}"
                    )
            except Exception as e:
                logger.error(f"MorningStar fetch error for {isin}: {str(e)}")

            return base_data

    async def _search_id(self, client: httpx.AsyncClient, isin: str) -> Optional[str]:
        # POST to search API
        url = self.SEARCH_API + "?source=nav&moduleId=6&ifIncludeAds=True&usrtType=v"
        try:
            resp = await client.post(url, data={"q": isin})
            if resp.status_code == 200:
                import re

                # Common pattern for JSON response or specific data attributes
                match = re.search(r'{"i":"([^"]+)"', resp.text)
                if match:
                    return match.group(1)

                # Fallback: look for 0P... pattern commonly used by MS tools
                match = re.search(r"(0P\w{8})", resp.text)
                if match:
                    return match.group(1)
        except Exception as e:
            logger.warning(f"MorningStar search error for {isin}: {str(e)}")
        return None

    def _parse_page(self, parser: HTMLParser, isin: str, url: str) -> Dict[str, Any]:
        """
        Parse the HTML using robust CSS selectors / Xpaths observed on the page.
        """
        data = {"isin": isin, "morningstar_url": url}

        try:
            # 1. Name
            name_node = parser.css_first("h1 span")
            if name_node:
                data["long_name"] = name_node.text(strip=True)

            # For Selectolax, finding following-siblings of text nodes requires iterating or parent checking.
            # We can find all elements with class 'sal-dp-name' and look at their text, then get their next sibling.
            for dp_name in parser.css(".sal-dp-name, h3"):
                label = dp_name.text(strip=True).lower()
                sibling = dp_name.next
                # Skip text nodes until we get an element
                while sibling and not hasattr(sibling, "text"):
                    sibling = sibling.next

                if not sibling:
                    continue

                val_text = sibling.text(strip=True)

                # 2. Sector
                if "secteur" in label:
                    data["sector"] = val_text

                # 3. Industry
                elif "industrie" in label:
                    data["industry"] = val_text

                # 4. Market Cap
                elif "cap. boursi" in label or "capitalisation" in label:
                    data["market_cap_str"] = val_text

                # 5. P/E Ratio
                elif "cours/bénéfices" in label or "price/earnings" in label or "per" in label:
                    try:
                        clean_per = val_text.replace(",", ".").strip()
                        if clean_per and clean_per != "-":
                            data["pe_ratio"] = float(clean_per)
                    except ValueError:
                        pass

                # 6. Dividend Yield
                elif "rendement div" in label or "dividend yield" in label:
                    try:
                        clean_div = val_text.replace("%", "").replace(",", ".").strip()
                        if clean_div and clean_div != "-":
                            data["dividend_yield"] = float(clean_div)
                    except ValueError:
                        pass

            # 7. Price
            price_node = parser.css_first("#message-box-price")
            if price_node:
                data["current_price_str"] = price_node.text(strip=True)

        except Exception as e:
            logger.error(f"Error parsing MorningStar page for {isin}: {str(e)}")

        return data
