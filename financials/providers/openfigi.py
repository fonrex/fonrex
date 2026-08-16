"""
OpenFIGIProvider — Résolution d'identifiants financiers via l'API OpenFIGI.

API : https://api.openfigi.com/v3/mapping (Bloomberg, gratuit avec clé)
Utilité : ISIN → FIGI, Ticker, MIC, exchange code, sécurité type, etc.

Rate limits (clé gratuite) : 25 req/min, 100 ISIN/requête max.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

from pydantic import BaseModel

from financials.providers.base import BaseFinancialProvider

logger = logging.getLogger(__name__)


# ── Modèles ───────────────────────────────────────────────────────────────────


class FIGIMapping(BaseModel):
    figi: Optional[str] = None
    composite_figi: Optional[str] = None
    share_class_figi: Optional[str] = None
    ticker: Optional[str] = None
    exchange_code: Optional[str] = None
    mic: Optional[str] = None
    name: Optional[str] = None
    market_sector: Optional[str] = None
    security_type: Optional[str] = None
    security_type2: Optional[str] = None
    isin: Optional[str] = None
    currency: Optional[str] = None


class OpenFIGIResult(BaseModel):
    isin: str
    mappings: List[FIGIMapping] = []
    source: str = "OpenFIGI"


# ── Provider ──────────────────────────────────────────────────────────────────


class OpenFIGIProvider(BaseFinancialProvider):
    """
    Provider OpenFIGI pour la résolution d'identifiants.
    Utilisé principalement lors de l'import pour enrichir asset_mappings.
    """

    name = "OpenFIGI"
    timeout = 12.0
    max_retries = 2
    retry_delay = 2.5

    # Rate limit conservateur : 20 req/min avec clé
    _semaphore: asyncio.Semaphore = asyncio.Semaphore(5)

    API_URL = "https://api.openfigi.com/v3/mapping"

    def __init__(self):
        self.api_key = os.getenv("OPENFIGI_API_KEY", "")

    def _openfigi_headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key
        return headers

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        **kwargs,
    ) -> Optional[OpenFIGIResult]:
        """
        Résout les identifiants pour un ISIN ou ticker.
        Priorité : ISIN > ticker.
        """
        identifier = isin or ticker
        if not identifier:
            return None

        id_type = "ID_ISIN" if isin else "TICKER"
        try:
            results = await self.fetch_batch([identifier], id_type=id_type)
            if results:
                return results[0]
            return OpenFIGIResult(isin=identifier, mappings=[])
        except Exception as exc:
            logger.error("[OpenFIGI] Erreur fetch(%s): %s", identifier, exc)
            return OpenFIGIResult(isin=identifier, mappings=[])

    async def fetch_batch(
        self,
        isins: List[str],
        id_type: str = "ID_ISIN",
    ) -> List[OpenFIGIResult]:
        """
        Résout jusqu'à 100 ISINs en une seule requête POST.

        Body : [{"idType": "ID_ISIN", "idValue": "ISIN1"}, ...]
        Réponse : array de la même taille — chaque élément est
                  {"data": [...mappings...]} ou {"error": "..."}
        """
        if not isins:
            return []

        # Limiter à 100 par batch (limite API)
        results: List[OpenFIGIResult] = []
        chunk_size = 100

        for start in range(0, len(isins), chunk_size):
            chunk = isins[start : start + chunk_size]
            body = self._build_request_body(chunk, id_type)
            headers = self._openfigi_headers()

            try:
                raw = await self._post_json(self.API_URL, body, headers=headers)
                if not raw or not isinstance(raw, list):
                    # Retourner des résultats vides pour ce chunk
                    results.extend([OpenFIGIResult(isin=isin, mappings=[]) for isin in chunk])
                    continue

                for i, item in enumerate(raw):
                    isin = chunk[i] if i < len(chunk) else "unknown"
                    if "error" in item:
                        logger.debug("[OpenFIGI] ISIN %s non trouvé: %s", isin, item["error"])
                        results.append(OpenFIGIResult(isin=isin, mappings=[]))
                    elif "data" in item:
                        mappings = [self._parse_response(isin, m) for m in item["data"]]
                        results.append(OpenFIGIResult(isin=isin, mappings=mappings))
                    else:
                        results.append(OpenFIGIResult(isin=isin, mappings=[]))

            except Exception as exc:
                logger.error("[OpenFIGI] Erreur fetch_batch: %s", exc)
                results.extend([OpenFIGIResult(isin=isin, mappings=[]) for isin in chunk])

        return results

    def _build_request_body(self, isins: List[str], id_type: str = "ID_ISIN") -> List[dict]:
        """Construit le body de la requête batch OpenFIGI."""
        return [{"idType": id_type, "idValue": isin} for isin in isins]

    def _parse_response(self, isin: str, data: dict) -> FIGIMapping:
        """Parse un élément de réponse OpenFIGI en FIGIMapping."""
        return FIGIMapping(
            figi=data.get("figi"),
            composite_figi=data.get("compositeFIGI"),
            share_class_figi=data.get("shareClassFIGI"),
            ticker=data.get("ticker"),
            exchange_code=data.get("exchCode"),
            mic=data.get("marketIdentifierCode"),
            name=data.get("name") or data.get("securityDescription"),
            market_sector=data.get("marketSector"),
            security_type=data.get("securityType"),
            security_type2=data.get("securityType2"),
            isin=isin,
            currency=data.get("currency"),
        )
