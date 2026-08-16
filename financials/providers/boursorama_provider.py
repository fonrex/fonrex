import asyncio
import logging
import random
from typing import List, Optional

import httpx
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# User-Agent rotation list
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]


class BoursoramaProvider(BaseProvider):
    """
    Provider asynchrone pour Boursorama utilisant httpx et selectolax.
    """

    BASE_URL = "https://www.boursorama.com"
    SEARCH_URL = "https://www.boursorama.com/recherche/ajax?query={symbol}&searchId="

    def __init__(self, max_retries: int = 3, timeout: int = 10):
        self.max_retries = max_retries
        self.timeout = timeout

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        """
        Récupère les données financières pour un ticker.
        Flux:
        1. Recherche du ticker interne Boursorama
        2. Fetch de la page
        3. Parsing
        """
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                # 1. Obtenir l'URL de la fiche valeur
                url = await self._search_symbol(client, ticker)
                if not url:
                    logger.warning(f"Ticker {ticker} non trouvé sur Boursorama")
                    return None

                # 2. Récupérer la page
                html = await self._fetch_page(client, url)
                if not html:
                    return None

                # 3. Parser les données
                metrics = self._parse_page(html, ticker)
                if metrics:
                    metrics.provider_url = url
                return metrics

            except Exception as e:
                logger.error(f"Erreur globale pour {ticker}: {e}")
                return None

    async def _search_symbol(self, client: httpx.AsyncClient, symbol: str) -> Optional[str]:
        """Recherche l'URL de la fiche valeur."""
        url = self.SEARCH_URL.format(symbol=symbol)

        try:
            response = await self._fetch_with_retry(client, url)
            if not response:
                return None

            # Le retour est souvent du HTML partiel ou une redirection
            # L'ancien code cherchait le premier lien dans une liste
            parser = HTMLParser(response.text)

            # Sélecteur exact basé sur la structure des trames réseaux de la recherche
            # Les liens des résultats (Action, ETF, etc.) ont la classe 'search__list-link'
            link = parser.css_first("a.search__list-link")

            if link:
                href = link.attributes.get("href")
                if href:
                    return f"{self.BASE_URL}{href}" if href.startswith("/") else href

            return None
        except Exception as e:
            logger.error(f"Erreur recherche symbole {symbol}: {e}")
            return None

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> Optional[HTMLParser]:
        """Récupère le HTML d'une page."""
        response = await self._fetch_with_retry(client, url)
        if response:
            return HTMLParser(response.text)
        return None

    async def _fetch_with_retry(
        self, client: httpx.AsyncClient, url: str
    ) -> Optional[httpx.Response]:
        """Fetch avec retries exponentielles et rotation User-Agent."""
        for attempt in range(self.max_retries):
            headers = {"User-Agent": random.choice(USER_AGENTS)}
            try:
                response = await client.get(url, headers=headers)

                if response.status_code == 200:
                    return response

                if response.status_code in [429, 503]:
                    wait_time = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"Status {response.status_code} pour {url}. Retry dans {wait_time:.2f}s"
                    )
                    await asyncio.sleep(wait_time)
                    continue

                logger.error(f"Erreur HTTP {response.status_code} pour {url}")
                return None

            except httpx.RequestError as e:
                logger.error(f"Erreur réseau pour {url}: {e}")
                if attempt == self.max_retries - 1:
                    return None
                await asyncio.sleep(1)
        return None

    def _parse_page(self, parser: HTMLParser, ticker: str) -> FinancialMetrics:
        """Extrait les données de la page via selectolax."""
        metrics = FinancialMetrics(ticker=ticker)

        # Extraction ISIN
        isin_node = parser.css_first(".c-faceplate__isin")
        if isin_node:
            metrics.isin = isin_node.text(strip=True).replace("ISIN :", "").strip()

        # Extraction Éligibilité (PEA, SRD, etc.)
        # Sélecteur basé sur structure commune: souvent dans le header ou 'c-list-info'
        metrics.eligibility = self._extract_eligibility(parser)

        # Score ESG (Nouveau sélecteur Gauge ou liste)
        esg_score_node = parser.css_first(".c-gauge__label")
        if not esg_score_node:
            # Essayer de trouver dans la liste d'infos
            for item in parser.css(".c-list-info__item"):
                if "Risque ESG" in item.text():
                    esg_score_node = item
                    break

        if esg_score_node:
            esg_text = esg_score_node.text(strip=True)
            if "Fermer" in esg_text:
                esg_text = esg_text.split("Fermer")[-1].strip()
            metrics.esg_score = esg_text

        # Niveau de controverse et autres données clés ESG
        for node in parser.css(".o-flex-stretch"):
            label_node = node.css_first("span")
            val_node = node.css_first("strong")
            if label_node and val_node:
                label = label_node.text().lower()
                val = val_node.text(strip=True)
                if "controverse" in label:
                    metrics.esg_controversy = val
                elif "co₂" in label:
                    metrics.esg_co2 = val
                elif "positif" in label:
                    metrics.esg_positive_impact = val
                elif "négatif" in label:
                    metrics.esg_negative_impact = val

        # Données de valorisation (Tableau consensus ou chiffres clés)
        # On essaie de trouver le tableau des chiffres clés
        # Souvent .c-table
        self._extract_key_figures(parser, metrics)

        # Risque (ETF)
        gauge = parser.css_first(".c-gauge")
        if gauge:
            current = gauge.attributes.get("data-gauge-current-step")
            max_step = gauge.attributes.get("data-gauge-steps")
            if current and max_step:
                metrics.risk_level = f"{current}/{max_step}"

        return metrics

    def _extract_eligibility(self, parser: HTMLParser) -> List[str]:
        items = []
        # Recherche des badges d'éligibilité (PEA, SRD, etc.)
        # On évite les éléments contenant des scripts ou trop longs pour être des labels
        for node in parser.css("li"):
            if node.css_first("script") or node.css_first("style"):
                continue
            text = node.text(strip=True)
            if text and any(token in text for token in ["PEA", "SRD", "CTO"]):
                if len(text) < 50:
                    items.append(text)

        return list(set(items))

    def _extract_text_by_label(
        self, parser: HTMLParser, label_part: str, parent_css: str = ""
    ) -> Optional[str]:
        selector = f"{parent_css} *" if parent_css else "*"
        for node in parser.css(selector):
            if node.css_first("script") or node.css_first("style"):
                continue
            text = node.text(strip=True)
            if label_part in text and len(text) < 100:
                return text
        return None

    def _extract_key_figures(self, parser: HTMLParser, metrics: FinancialMetrics):
        """
        Tente d'extraire PER, Rendement, BNA depuis les tableaux.
        Tableaux souvent structurés avec <thead> (années) et <tbody> (valeurs).
        """
        # Stratégie: Chercher les lignes contenant "PER", "Rendement", "BPA" (ou BNA)
        for row in parser.css("tr"):
            cells = row.css("td")
            header = row.css_first("th")
            row_title = header.text(strip=True) if header else ""

            if not row_title and cells:
                # Parfois le titre est dans le premier td
                row_title = cells[0].text(strip=True)
                values = cells[1:]
            else:
                values = cells

            if not values:
                continue

            # On prend la première valeur (souvent l'année en cours ou estimée N+1)
            # Pour PER et Rendement, Boursorama affiche souvent N, N+1, N+2
            # On essaie de prendre la colonne qui correspond à l'année la plus pertinente (souvent index 0 ou 1)
            val_text = values[0].text(strip=True)
            val = self._clean_number(val_text)

            if "PER" in row_title or "P/E" in row_title:
                metrics.pe_ratio = val
            elif "Rendement" in row_title:
                metrics.dividend_yield = val
            elif "BPA" in row_title or "BNA" in row_title:
                metrics.eps = val
            elif "Résultat net" in row_title:
                metrics.net_income = val
            elif "Chiffre d'affaires" in row_title:
                metrics.revenue = val
            elif "Dividende" in row_title and not metrics.dividend_yield:
                # Si on a le dividende brut mais pas le rendement
                pass

    def _clean_number(self, text: str) -> Optional[float]:
        try:
            # Nettoyage format français "1 234,56" -> "1234.56"
            clean = text.replace(" ", "").replace(",", ".").replace("%", "")
            return float(clean)
        except ValueError:
            return None
