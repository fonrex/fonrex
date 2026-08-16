import asyncio
import logging
import re
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from financials.exchange import GURUFOCUS_TO_YAHOO
from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class GurufocusProvider(BaseProvider):
    """
    Gurufocus Provider.
    Fetches specific scores (Piotroski, Beneish, etc.) from term definitions pages.
    """

    BASE_URL = "https://www.gurufocus.com"

    def __init__(self, max_retries: int = 3, timeout: int = 20):
        self.max_retries = max_retries
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,fr-FR;q=0.8,fr;q=0.7",
            "Origin": "https://www.gurufocus.com",
            "Referer": "https://www.gurufocus.com/",
        }

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """
        Récupère les métriques financières en résolvant d'abord le ticker via l'API de recherche.
        """
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=self.headers
        ) as client:
            # 1. Résolution du ticker GuruFocus (ex: TFI.PA -> XPAR:TFI)
            gf_ticker = await self._resolve_gf_ticker(client, ticker)
            if not gf_ticker:
                logger.warning(f"Could not resolve GuruFocus ticker for {ticker}")
                return None

            logger.info(f"Resolved GuruFocus ticker: {ticker} -> {gf_ticker}")
            metrics = FinancialMetrics(ticker=gf_ticker)
            metrics.provider_url = f"{self.BASE_URL}/stock/{gf_ticker}/"

            # 2. Récupération des scores en parallèle
            tasks = [
                self._fetch_score(client, gf_ticker, "fscore", "Piotroski-F-Score"),
                self._fetch_score(client, gf_ticker, "mscore", "Beneish-M-Score"),
                self._fetch_score(client, gf_ticker, "ROIC", "ROIC-Percentage"),
                self._fetch_score(client, gf_ticker, "gf_score", "GF-Score"),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Unpack results
            f_score, m_score, roic, gf_score = results

            # Assign if valid
            if isinstance(f_score, float):
                metrics.piotroski_score = int(f_score)
            if isinstance(m_score, float):
                metrics.beneish_m_score = m_score
            if isinstance(roic, float):
                metrics.roic = roic
            # gf_score extraction logic could be added to models if needed

            return metrics

    async def _resolve_gf_ticker(self, client: httpx.AsyncClient, ticker: str) -> Optional[str]:
        """
        Résout le ticker GuruFocus en utilisant l'API de recherche et le mapping d'exchange.
        Ex: TFI.PA -> XPAR:TFI
        """
        # On sépare le symbole et l'éventuel suffixe d'exchange
        parts = ticker.split(".")
        symbol = parts[0]
        target_suffix = f".{parts[1].upper()}" if len(parts) > 1 else ""

        # Si le ticker contient déjà un exchange GuruFocus (ex: NAS:AAPL)
        if ":" in ticker:
            return ticker

        resolved = await self._search_symbol(client, symbol, target_suffix)
        if resolved:
            return resolved

        # Fallback: si la recherche échoue, on tente une reconstruction via le mapping d'exchange
        if target_suffix is not None:
            for gf_exch, suffix in GURUFOCUS_TO_YAHOO.items():
                if suffix.upper() == target_suffix.upper():
                    # Si pas de suffixe (marché US), on privilégie NAS ou NYSE
                    if target_suffix == "" and gf_exch not in ["NAS", "NYSE"]:
                        continue
                    return f"{gf_exch}:{symbol}"

        return None

    async def _search_symbol(
        self, client: httpx.AsyncClient, query: str, target_suffix: Optional[str]
    ) -> Optional[str]:
        """
        Interroge l'API de recherche GuruFocus et sélectionne le meilleur résultat.
        """
        search_url = f"{self.BASE_URL}/reader/_api/_search?v=1.8.50&text={query}&type="
        try:
            resp = await client.get(search_url)
            if resp.status_code == 200:
                results = resp.json()

                # On cherche d'abord une correspondance exacte via le suffixe d'exchange
                for item in results:
                    if item.get("type") == "stock":
                        data = item.get("data", {})
                        gf_symbol = data.get("symbol", "").upper()
                        gf_exchange = data.get("exchange", "").upper()

                        # Si on a un suffixe cible (ex: .PA)
                        if target_suffix:
                            # On convertit l'exchange GF en suffixe Yahoo pour comparer
                            mapped_suffix = GURUFOCUS_TO_YAHOO.get(gf_exchange)
                            if mapped_suffix and mapped_suffix.upper() == target_suffix.upper():
                                return f"{gf_exchange}:{gf_symbol}"
                        else:
                            # Si pas de suffixe, on prend la première correspondance exacte de symbole (souvent US)
                            if gf_symbol == query.upper():
                                return f"{gf_exchange}:{gf_symbol}" if gf_exchange else gf_symbol

                # Fallback : premier résultat de type stock si le symbole correspond
                for item in results:
                    if item.get("type") == "stock":
                        data = item.get("data", {})
                        if data.get("symbol", "").upper() == query.upper():
                            exch = data.get("exchange")
                            symb = data.get("symbol")
                            return f"{exch}:{symb}" if exch else symb

        except Exception as e:
            logger.error(f"Error during GuruFocus search for {query}: {e}")
        return None

    async def _fetch_score(
        self, client: httpx.AsyncClient, ticker: str, term_code: str, term_name: str
    ) -> Optional[float]:
        """
        Extrait une valeur numérique depuis une page de définition de terme GuruFocus.
        """
        url = f"{self.BASE_URL}/term/{term_code}/{ticker}/{term_name}/{ticker}"
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                html = HTMLParser(resp.text)
                # Heuristique : on cherche le premier nombre flottant dans le contenu principal
                # GuruFocus affiche généralement la valeur en évidence.
                content = html.text()
                # On cherche un pattern comme "8" ou "0.45" après avoir éliminé les bruits potentiels
                # Dans le code legacy, c'était le premier <font> dans un bloc spécifique.
                match = re.search(r"([-+]?\d*\.\d+|\d+)", content)
                if match:
                    return float(match.group(1))
        except Exception as e:
            logger.error(f"Error fetching {term_name} for {ticker}: {e}")
        return None
