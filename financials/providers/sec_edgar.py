"""
SECEdgarProvider — Insider transactions depuis l'API publique SEC EDGAR.

Source : https://data.sec.gov (officiel, gratuit, sans scraping)
Rate limit officiel : 10 req/s par IP.
User-Agent obligatoire : "Fonrex contact@fonrex.io" (policy SEC).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel

from financials.providers.base import BaseFinancialProvider

logger = logging.getLogger(__name__)


# ── Modèles Pydantic ──────────────────────────────────────────────────────────


class InsiderTransaction(BaseModel):
    filing_date: date
    insider_name: str
    insider_title: Optional[str] = None
    transaction_date: Optional[date] = None
    transaction_type: str
    shares: Optional[int] = None
    price_per_share: Optional[float] = None
    total_value: Optional[float] = None
    shares_owned_after: Optional[int] = None
    sec_filing_url: Optional[str] = None


class InsiderTransactionsResult(BaseModel):
    ticker: str
    cik: Optional[str] = None
    company_name: Optional[str] = None
    transactions: List[InsiderTransaction] = []
    total_count: int = 0
    source: str = "SEC EDGAR"


_TRANSACTION_CODES = {
    "P": "Buy",
    "S": "Sell",
    "A": "Award",
    "M": "Option Exercise",
    "G": "Gift",
    "F": "Tax Withholding",
    "D": "Sale to Issuer",
    "I": "Discretionary",
    "C": "Conversion",
    "W": "Will/Inheritance",
    "X": "Option Exercise",
}


# ── Provider ──────────────────────────────────────────────────────────────────


class SECEdgarProvider(BaseFinancialProvider):
    """Provider SEC EDGAR pour les insider transactions Form 4."""

    name = "SECEdgar"
    timeout = 12.0
    max_retries = 3
    retry_delay = 2.0
    _semaphore: asyncio.Semaphore = asyncio.Semaphore(8)

    BASE_URL = "https://data.sec.gov"
    TICKER_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

    def __init__(self):
        self._sec_email = os.environ.get("SEC_EDGAR_EMAIL", "contact@fonrex.io")
        self._sec_ua = f"Fonrex {self._sec_email}"

    def _sec_headers(self) -> dict:
        return {"User-Agent": self._sec_ua, "Accept": "application/json"}

    async def fetch(
        self,
        ticker: str = None,
        isin: str = None,
        provider_url: str = None,
        cik: str = None,
        limit: int = 20,
        **kwargs,
    ) -> Optional[InsiderTransactionsResult]:
        if not ticker:
            return None
        try:
            resolved_cik = cik or await self._resolve_cik(ticker)
            if not resolved_cik:
                return InsiderTransactionsResult(ticker=ticker, transactions=[], total_count=0)
            transactions = await self._fetch_form4_transactions(resolved_cik, limit)
            company_name = await self._get_company_name(resolved_cik)
            return InsiderTransactionsResult(
                ticker=ticker,
                cik=resolved_cik,
                company_name=company_name,
                transactions=transactions,
                total_count=len(transactions),
            )
        except Exception as exc:
            logger.error("[SECEdgar] Erreur fetch(%s): %s", ticker, exc)
            return InsiderTransactionsResult(ticker=ticker, transactions=[], total_count=0)

    async def _resolve_cik(self, ticker: str) -> Optional[str]:
        cik = await self._resolve_cik_via_static(ticker)
        if cik:
            return cik
        return await self._resolve_cik_via_search(ticker)

    async def _resolve_cik_via_static(self, ticker: str) -> Optional[str]:
        url = "https://www.sec.gov/files/company_tickers.json"
        data = await self._get_json(url, headers=self._sec_headers())
        if not data:
            return None
        try:
            ticker_upper = ticker.upper().split(".")[0]
            for entry in data.values():
                if entry.get("ticker", "").upper() == ticker_upper:
                    return str(entry.get("cik_str", "")).zfill(10)
        except Exception as exc:
            logger.warning("[SECEdgar] Erreur company_tickers.json: %s", exc)
        return None

    async def _resolve_cik_via_search(self, ticker: str) -> Optional[str]:
        params = {
            "action": "getcompany",
            "company": ticker.upper().split(".")[0],
            "type": "10-K",
            "dateb": "",
            "owner": "include",
            "count": "5",
            "search_text": "",
            "output": "atom",
        }
        try:
            text = await self._get(self.TICKER_URL, headers=self._sec_headers(), params=params)
            if not text:
                return None
            match = re.search(r"<cik>(\d+)</cik>", text, re.IGNORECASE)
            if not match:
                match = re.search(r"CIK=(\d+)", text, re.IGNORECASE)
            if match:
                return match.group(1).zfill(10)
        except Exception as exc:
            logger.warning("[SECEdgar] Erreur _resolve_cik_via_search: %s", exc)
        return None

    async def _get_company_name(self, cik: str) -> Optional[str]:
        url = f"{self.BASE_URL}/submissions/CIK{cik.zfill(10)}.json"
        data = await self._get_json(url, headers=self._sec_headers())
        return data.get("name") if data else None

    async def _fetch_form4_transactions(self, cik: str, limit: int) -> List[InsiderTransaction]:
        url = f"{self.BASE_URL}/submissions/CIK{cik.zfill(10)}.json"
        data = await self._get_json(url, headers=self._sec_headers())
        if not data:
            return []
        try:
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            filing_dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            max_filings = min(limit * 2, 15)
            form4_indices = [i for i, f in enumerate(forms) if f == "4"][:max_filings]
            transactions: List[InsiderTransaction] = []
            cik_num = str(int(cik))
            consecutive_failures = 0
            for idx in form4_indices:
                if len(transactions) >= limit:
                    break
                if consecutive_failures >= 3:
                    logger.warning(
                        "[SECEdgar] Interruption de _fetch_form4_transactions après %d échecs HTTP consécutifs",
                        consecutive_failures,
                    )
                    break
                try:
                    filing_date_str = filing_dates[idx] if idx < len(filing_dates) else None
                    accession = accessions[idx] if idx < len(accessions) else None
                    if not filing_date_str or not accession:
                        continue
                    filing_date = datetime.strptime(filing_date_str, "%Y-%m-%d").date()
                    accession_nd = accession.replace("-", "")
                    filing_url = (
                        f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{accession_nd}/"
                    )
                    txns = await self._parse_form4_filing(
                        cik_num, accession_nd, filing_date, filing_url
                    )
                    if txns is None:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0
                        transactions.extend(txns)
                except Exception as exc:
                    consecutive_failures += 1
                    logger.warning("[SECEdgar] idx=%d parsing error: %s", idx, exc)
            return transactions[:limit]
        except Exception as exc:
            logger.error("[SECEdgar] _fetch_form4_transactions error: %s", exc)
            return []

    async def _parse_form4_filing(
        self, cik_num: str, accession_nd: str, filing_date: date, filing_url: str
    ) -> Optional[List[InsiderTransaction]]:
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_num}/{accession_nd}/{accession_nd}-index.htm"
        )
        index_html = await self._get(index_url, headers=self._sec_headers())
        if index_html is None:
            return None
        xml_match = re.search(
            r'href="(/Archives/edgar/data/[^"]+\.xml)"', index_html, re.IGNORECASE
        )
        if not xml_match:
            return []
        xml_url = "https://www.sec.gov" + xml_match.group(1)
        xml_text = await self._get(xml_url, headers=self._sec_headers())
        if xml_text is None:
            return None
        return self._parse_form4_xml(xml_text, filing_date, filing_url)

    def _parse_form4_xml(
        self, xml_text: str, filing_date: date, filing_url: str
    ) -> List[InsiderTransaction]:
        transactions: List[InsiderTransaction] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning("[SECEdgar] XML parse error: %s", exc)
            return []

        owner = root.find(".//reportingOwner")
        insider = "Unknown"
        title = None
        if owner is not None:
            name_el = owner.find(".//rptOwnerName")
            if name_el is not None and name_el.text:
                insider = name_el.text.strip()
            title_el = owner.find(".//officerTitle")
            if title_el is not None and title_el.text:
                title = title_el.text.strip()

        for txn in root.findall(".//nonDerivativeTransaction"):
            try:
                txn_date = None
                date_el = txn.find(".//transactionDate/value")
                if date_el is not None and date_el.text:
                    txn_date = datetime.strptime(date_el.text.strip(), "%Y-%m-%d").date()
                code_el = txn.find(".//transactionCode")
                code = code_el.text.strip() if code_el is not None and code_el.text else "S"
                txn_type = _TRANSACTION_CODES.get(code, code)
                shares_el = txn.find(".//transactionShares/value")
                shares = (
                    self._safe_int(shares_el.text.strip())
                    if shares_el is not None and shares_el.text
                    else None
                )
                price_el = txn.find(".//transactionPricePerShare/value")
                price = (
                    self._safe_float(price_el.text.strip())
                    if price_el is not None and price_el.text
                    else None
                )
                total_val = (shares * price) if shares and price else None
                after_el = txn.find(".//sharesOwnedFollowingTransaction/value")
                after = (
                    self._safe_int(after_el.text.strip())
                    if after_el is not None and after_el.text
                    else None
                )
                transactions.append(
                    InsiderTransaction(
                        filing_date=filing_date,
                        insider_name=insider,
                        insider_title=title,
                        transaction_date=txn_date,
                        transaction_type=txn_type,
                        shares=shares,
                        price_per_share=price,
                        total_value=total_val,
                        shares_owned_after=after,
                        sec_filing_url=filing_url,
                    )
                )
            except Exception as exc:
                logger.debug("[SECEdgar] nonDerivativeTxn parse: %s", exc)
        return transactions
