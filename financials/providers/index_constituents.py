"""
IndexConstituentsProvider — Composants des grands indices boursiers.

Sources :
- S&P 500   : https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
- CAC 40    : https://fr.wikipedia.org/wiki/CAC_40
- NASDAQ 100: https://en.wikipedia.org/wiki/Nasdaq-100
- DAX       : https://en.wikipedia.org/wiki/DAX

Wikipedia est stable, publique, et permissive pour le scraping.
Mise à jour recommandée : hebdomadaire.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import List, Optional

from bs4 import BeautifulSoup
from pydantic import BaseModel

from financials.providers.base import BaseFinancialProvider

logger = logging.getLogger(__name__)


# ── Enums & Modèles ───────────────────────────────────────────────────────────


class IndexName(str, Enum):
    SP500 = "SP500"
    CAC40 = "CAC40"
    NASDAQ100 = "NASDAQ100"
    DAX = "DAX"


class IndexConstituent(BaseModel):
    ticker: str
    isin: Optional[str] = None
    name: str
    sector: Optional[str] = None
    sub_sector: Optional[str] = None
    weight: Optional[float] = None
    country: Optional[str] = None
    cik: Optional[str] = None  # US uniquement


class IndexConstituentsResult(BaseModel):
    index_name: str
    constituents: List[IndexConstituent] = []
    total_count: int = 0
    source_url: str
    source: str = "Wikipedia"


WIKIPEDIA_URLS = {
    IndexName.SP500: "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    IndexName.CAC40: "https://fr.wikipedia.org/wiki/CAC_40",
    IndexName.NASDAQ100: "https://en.wikipedia.org/wiki/Nasdaq-100",
    IndexName.DAX: "https://en.wikipedia.org/wiki/DAX",
}


# ── Provider ──────────────────────────────────────────────────────────────────


class IndexConstituentsProvider(BaseFinancialProvider):
    """Provider pour les composants des grands indices boursiers (Wikipedia)."""

    name = "IndexConstituents"
    timeout = 20.0
    max_retries = 3
    retry_delay = 1.0

    def _wiki_headers(self, lang: str = "en") -> dict:
        return self._get_headers(
            {
                "Accept-Language": f"{lang},{lang}-{lang.upper()};q=0.9,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            }
        )

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        index_name: IndexName = None,
        **kwargs,
    ) -> Optional[IndexConstituentsResult]:
        if not index_name:
            logger.warning("[IndexConstituents] index_name requis")
            return None
        try:
            if index_name == IndexName.SP500:
                return await self.fetch_sp500()
            elif index_name == IndexName.CAC40:
                return await self.fetch_cac40()
            elif index_name == IndexName.NASDAQ100:
                return await self.fetch_nasdaq100()
            elif index_name == IndexName.DAX:
                return await self.fetch_dax()
            else:
                logger.warning("[IndexConstituents] Index non supporté: %s", index_name)
                return None
        except Exception as exc:
            logger.error("[IndexConstituents] Erreur fetch(%s): %s", index_name, exc)
            return None

    # ── S&P 500 ───────────────────────────────────────────────────────────────

    async def fetch_sp500(self) -> IndexConstituentsResult:
        """
        Parse la table Wikipedia du S&P 500.
        Colonnes : Symbol | Security | GICS Sector | GICS Sub-Industry |
                   Headquarters | Date Added | CIK | Founded
        """
        url = WIKIPEDIA_URLS[IndexName.SP500]
        html = await self._get(url, headers=self._wiki_headers("en"))
        if not html:
            return IndexConstituentsResult(index_name="SP500", source_url=url, total_count=0)

        rows = self._parse_wikipedia_table(html, table_id="constituents")
        constituents = []
        for row in rows:
            ticker = row.get("Symbol") or row.get("Ticker") or ""
            name = row.get("Security") or row.get("Company") or ""
            if not ticker or not name:
                continue
            constituents.append(
                IndexConstituent(
                    ticker=ticker.strip(),
                    name=name.strip(),
                    sector=row.get("GICS Sector") or row.get("Sector"),
                    sub_sector=row.get("GICS Sub-Industry"),
                    cik=row.get("CIK"),
                    country="US",
                )
            )

        return IndexConstituentsResult(
            index_name="SP500",
            constituents=constituents,
            total_count=len(constituents),
            source_url=url,
        )

    # ── CAC 40 ────────────────────────────────────────────────────────────────

    async def fetch_cac40(self) -> IndexConstituentsResult:
        """
        Parse la table Wikipedia du CAC 40 (version française).
        Colonnes variables selon la version de la page.
        """
        url = WIKIPEDIA_URLS[IndexName.CAC40]
        html = await self._get(url, headers=self._wiki_headers("fr"))
        if not html:
            return IndexConstituentsResult(index_name="CAC40", source_url=url, total_count=0)

        rows = self._parse_wikipedia_table(html, table_index=1)
        constituents = []
        for row in rows:
            # Les noms de colonnes varient — on cherche de façon flexible
            name = (
                row.get("Entreprise")
                or row.get("Société")
                or row.get("Nom")
                or row.get("Company")
                or ""
            )
            isin = row.get("ISIN") or row.get("Code ISIN") or ""
            ticker = row.get("Mnémonique") or row.get("Ticker") or row.get("Code") or ""
            sector = (
                row.get("Secteur") or row.get("Secteur d'activité") or row.get("Secteur ICB") or ""
            )
            if not name:
                continue

            # Dériver le pays depuis l'ISIN (2 premiers caractères)
            country = isin[:2].upper() if isin and len(isin) >= 2 else None

            # Ajouter suffixe .PA si pas de ticker
            if not ticker and name:
                ticker = name  # Sera raffiné en DB

            constituents.append(
                IndexConstituent(
                    ticker=ticker.strip() if ticker else name.strip(),
                    isin=isin.strip() if isin else None,
                    name=name.strip(),
                    sector=sector.strip() if sector else None,
                    country=country,
                )
            )

        return IndexConstituentsResult(
            index_name="CAC40",
            constituents=constituents,
            total_count=len(constituents),
            source_url=url,
        )

    # ── NASDAQ 100 ────────────────────────────────────────────────────────────

    async def fetch_nasdaq100(self) -> IndexConstituentsResult:
        """
        Parse la table Wikipedia du NASDAQ 100.
        Colonnes : Company | Ticker | GICS Sector | GICS Sub-Industry
        """
        url = WIKIPEDIA_URLS[IndexName.NASDAQ100]
        html = await self._get(url, headers=self._wiki_headers("en"))
        if not html:
            return IndexConstituentsResult(index_name="NASDAQ100", source_url=url, total_count=0)

        rows = self._parse_wikipedia_table(html, table_index=3)
        constituents = []
        for row in rows:
            ticker = row.get("Ticker") or row.get("Symbol") or ""
            name = row.get("Company") or row.get("Security") or ""
            if not ticker or not name:
                continue
            constituents.append(
                IndexConstituent(
                    ticker=ticker.strip(),
                    name=name.strip(),
                    sector=row.get("GICS Sector"),
                    sub_sector=row.get("GICS Sub-Industry"),
                    country="US",
                )
            )

        return IndexConstituentsResult(
            index_name="NASDAQ100",
            constituents=constituents,
            total_count=len(constituents),
            source_url=url,
        )

    # ── DAX ───────────────────────────────────────────────────────────────────

    async def fetch_dax(self) -> IndexConstituentsResult:
        """
        Parse la table Wikipedia du DAX.
        Colonnes : Company | Ticker | ISIN | Sector | Employees
        """
        url = WIKIPEDIA_URLS[IndexName.DAX]
        html = await self._get(url, headers=self._wiki_headers("en"))
        if not html:
            return IndexConstituentsResult(index_name="DAX", source_url=url, total_count=0)

        rows = self._parse_wikipedia_table(html, table_index=3)
        constituents = []
        for row in rows:
            ticker = row.get("Ticker") or row.get("Symbol") or ""
            name = row.get("Company") or row.get("Name") or ""
            isin = row.get("ISIN") or ""
            if not name:
                continue
            constituents.append(
                IndexConstituent(
                    ticker=ticker.strip() if ticker else name.strip(),
                    isin=isin.strip() if isin else None,
                    name=name.strip(),
                    sector=row.get("Sector") or row.get("Industry"),
                    country="DE",
                )
            )

        return IndexConstituentsResult(
            index_name="DAX",
            constituents=constituents,
            total_count=len(constituents),
            source_url=url,
        )

    # ── Core parser ───────────────────────────────────────────────────────────

    def _parse_wikipedia_table(
        self,
        html: str,
        table_index: int = 0,
        table_id: Optional[str] = None,
    ) -> List[dict]:
        """
        Parse une table HTML Wikipedia en liste de dicts.

        Stratégie :
        1. Cherche la table par id (si fourni)
        2. Sinon prend la table à l'index donné parmi les wikitables
        3. Gère les <th> en en-têtes et <td> en valeurs
        """
        soup = BeautifulSoup(html, "html.parser")

        # Trouver la table cible
        if table_id:
            table = soup.find("table", {"id": table_id})
            if not table:
                table = soup.find("table", {"class": re.compile(r"wikitable", re.I)})
        else:
            tables = soup.find_all("table", {"class": re.compile(r"wikitable", re.I)})
            if table_index < len(tables):
                table = tables[table_index]
            elif tables:
                table = tables[0]
            else:
                logger.warning("[IndexConstituents] Aucune wikitable trouvée")
                return []

        if not table:
            logger.warning("[IndexConstituents] Table introuvable")
            return []

        # Extraire les en-têtes
        headers = []
        header_row = table.find("tr")
        if header_row:
            for th in header_row.find_all(["th", "td"]):
                text = th.get_text(separator=" ", strip=True)
                # Nettoyer les annotations Wikipedia [1], [note], etc.
                text = re.sub(r"\[.*?\]", "", text).strip()
                headers.append(text)

        if not headers:
            return []

        # Extraire les lignes de données
        rows = []
        data_rows = table.find_all("tr")[1:]  # Skip header row

        for tr in data_rows:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue

            row = {}
            for i, cell in enumerate(cells):
                if i >= len(headers):
                    break
                # Extraire le texte en ignorant les balises internes superflues
                text = cell.get_text(separator=" ", strip=True)
                text = re.sub(r"\[.*?\]", "", text).strip()
                # Pour les liens, préférer le title ou le text du premier <a>
                link = cell.find("a")
                if link and not text:
                    text = link.get("title") or link.get_text(strip=True)
                row[headers[i]] = text

            if any(v for v in row.values()):
                rows.append(row)

        return rows
