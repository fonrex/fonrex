"""
Tests du SECEdgarProvider.

Mocks complets — aucune requête réseau réelle.
"""

import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from financials.providers.sec_edgar import (
    InsiderTransactionsResult,
    SECEdgarProvider,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

COMPANY_TICKERS_JSON = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
}

SUBMISSIONS_JSON = {
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "form": ["4", "4", "10-K", "4"],
            "filingDate": ["2026-04-15", "2026-03-10", "2026-02-01", "2026-01-20"],
            "accessionNumber": [
                "0000320193-26-000100",
                "0000320193-26-000050",
                "0000320193-26-000010",
                "0000320193-25-000900",
            ],
        }
    },
}

INDEX_HTML = """
<html>
<body>
<a href="/Archives/edgar/data/320193/000032019326000100/form4.xml">form4.xml</a>
</body>
</html>
"""

FORM4_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>Cook Timothy D</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTransaction>
    <securityTitle><value>Common Stock</value></securityTitle>
    <transactionDate><value>2026-04-12</value></transactionDate>
    <transactionCoding>
      <transactionCode>S</transactionCode>
    </transactionCoding>
    <transactionAmounts>
      <transactionShares><value>50000</value></transactionShares>
      <transactionPricePerShare><value>172.45</value></transactionPricePerShare>
    </transactionAmounts>
    <postTransactionAmounts>
      <sharesOwnedFollowingTransaction><value>3298456</value></sharesOwnedFollowingTransaction>
    </postTransactionAmounts>
  </nonDerivativeTransaction>
</ownershipDocument>
"""


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestSECEdgarCIKResolution(unittest.IsolatedAsyncioTestCase):
    """Tests de résolution du CIK."""

    def setUp(self):
        self.provider = SECEdgarProvider()

    async def test_resolve_cik_via_static_aapl(self):
        """Doit trouver le CIK de AAPL dans le JSON statique."""
        with patch.object(
            self.provider, "_get_json", new_callable=AsyncMock, return_value=COMPANY_TICKERS_JSON
        ):
            cik = await self.provider._resolve_cik_via_static("AAPL")
        self.assertEqual(cik, "0000320193")

    async def test_resolve_cik_strips_exchange_suffix(self):
        """Le suffixe .PA doit être retiré avant la recherche."""
        with patch.object(
            self.provider, "_get_json", new_callable=AsyncMock, return_value=COMPANY_TICKERS_JSON
        ):
            cik = await self.provider._resolve_cik_via_static("MSFT.US")
        self.assertEqual(cik, "0000789019")

    async def test_resolve_cik_not_found_returns_none(self):
        """Un ticker EU sans CIK doit retourner None."""
        with patch.object(
            self.provider, "_get_json", new_callable=AsyncMock, return_value=COMPANY_TICKERS_JSON
        ):
            cik = await self.provider._resolve_cik_via_static("AIR.PA")
        self.assertIsNone(cik)

    async def test_resolve_cik_api_failure_returns_none(self):
        """Si l'API échoue, retourner None sans lever d'exception."""
        with patch.object(self.provider, "_get_json", new_callable=AsyncMock, return_value=None):
            cik = await self.provider._resolve_cik_via_static("AAPL")
        self.assertIsNone(cik)


class TestSECEdgarFetch(unittest.IsolatedAsyncioTestCase):
    """Tests du fetch complet."""

    def setUp(self):
        self.provider = SECEdgarProvider()

    async def test_fetch_eu_ticker_returns_empty_result(self):
        """Un ticker EU sans CIK → InsiderTransactionsResult vide, pas d'erreur."""
        with patch.object(self.provider, "_resolve_cik", new_callable=AsyncMock, return_value=None):
            result = await self.provider.fetch(ticker="AIR.PA")
        self.assertIsInstance(result, InsiderTransactionsResult)
        self.assertEqual(result.ticker, "AIR.PA")
        self.assertEqual(result.transactions, [])
        self.assertEqual(result.total_count, 0)

    async def test_fetch_us_ticker_with_transactions(self):
        """AAPL avec CIK connu → transactions parsées."""

        async def mock_get_json(url, **kwargs):
            if "company_tickers" in url:
                return COMPANY_TICKERS_JSON
            if "submissions" in url:
                return SUBMISSIONS_JSON
            return None

        async def mock_get(url, **kwargs):
            if "index" in url:
                return INDEX_HTML
            if url.endswith(".xml"):
                return FORM4_XML
            return None

        with (
            patch.object(self.provider, "_get_json", side_effect=mock_get_json),
            patch.object(self.provider, "_get", side_effect=mock_get),
        ):
            result = await self.provider.fetch(ticker="AAPL", limit=5)

        self.assertIsInstance(result, InsiderTransactionsResult)
        self.assertEqual(result.ticker, "AAPL")
        self.assertEqual(result.cik, "0000320193")
        self.assertEqual(result.company_name, "Apple Inc.")
        self.assertGreater(result.total_count, 0)

    async def test_fetch_parses_transaction_correctly(self):
        """Les champs de la transaction doivent être correctement parsés."""

        async def mock_get_json(url, **kwargs):
            if "company_tickers" in url:
                return COMPANY_TICKERS_JSON
            if "submissions" in url:
                return SUBMISSIONS_JSON
            return None

        async def mock_get(url, **kwargs):
            if "index" in url:
                return INDEX_HTML
            if url.endswith(".xml"):
                return FORM4_XML
            return None

        with (
            patch.object(self.provider, "_get_json", side_effect=mock_get_json),
            patch.object(self.provider, "_get", side_effect=mock_get),
        ):
            result = await self.provider.fetch(ticker="AAPL", limit=5)

        if result.transactions:
            txn = result.transactions[0]
            self.assertEqual(txn.insider_name, "Cook Timothy D")
            self.assertEqual(txn.insider_title, "Chief Executive Officer")
            self.assertEqual(txn.transaction_type, "Sell")
            self.assertEqual(txn.shares, 50000)
            self.assertAlmostEqual(txn.price_per_share, 172.45, places=2)
            self.assertEqual(txn.shares_owned_after, 3298456)


class TestSECEdgarRateLimit(unittest.IsolatedAsyncioTestCase):
    """Vérifie que le semaphore est bien défini au niveau classe."""

    def test_semaphore_is_class_level(self):
        """Le semaphore doit être partagé entre toutes les instances."""
        p1 = SECEdgarProvider()
        p2 = SECEdgarProvider()
        self.assertIs(type(p1)._semaphore, type(p2)._semaphore)
        self.assertIsNotNone(SECEdgarProvider._semaphore)


class TestSECEdgarXMLParsing(unittest.TestCase):
    """Tests unitaires du parser XML Form 4."""

    def setUp(self):
        self.provider = SECEdgarProvider()

    def test_parse_form4_xml_returns_transaction(self):
        txns = self.provider._parse_form4_xml(
            FORM4_XML, date(2026, 4, 15), "https://www.sec.gov/Archives/test/"
        )
        self.assertEqual(len(txns), 1)
        txn = txns[0]
        self.assertEqual(txn.insider_name, "Cook Timothy D")
        self.assertEqual(txn.transaction_type, "Sell")
        self.assertEqual(txn.shares, 50000)

    def test_parse_form4_xml_invalid_xml(self):
        """Un XML invalide ne doit pas lever d'exception."""
        txns = self.provider._parse_form4_xml(
            "<invalid>xml<unclosed>", date(2026, 1, 1), "https://test.com/"
        )
        self.assertEqual(txns, [])


class TestSECEdgarCircuitBreakerAndUA(unittest.IsolatedAsyncioTestCase):
    """Tests du circuit breaker et du User-Agent."""

    def setUp(self):
        self.provider = SECEdgarProvider()

    async def test_consecutive_http_failures_breaks_early(self):
        """Si _get retourne None (échec HTTP) 3 fois consécutives, la boucle s'arrête."""
        mock_submissions = {
            "name": "Test Company",
            "filings": {
                "recent": {
                    "form": ["4"] * 10,
                    "filingDate": ["2026-01-01"] * 10,
                    "accessionNumber": [f"0000320193-26-00000{i}" for i in range(10)],
                }
            },
        }

        async def mock_get_json(url, **kwargs):
            return mock_submissions

        async def mock_get(url, **kwargs):
            return None  # Toujours en échec HTTP

        with (
            patch.object(self.provider, "_get_json", side_effect=mock_get_json),
            patch.object(self.provider, "_get", side_effect=mock_get) as mock_get_call,
        ):
            txns = await self.provider._fetch_form4_transactions("320193", limit=10)

        self.assertEqual(txns, [])
        # Doit s'arrêter après exactement 3 échecs consécutifs au lieu de tenter les 10 filings
        self.assertEqual(mock_get_call.call_count, 3)

    async def test_custom_user_agent_preserved_in_base_provider(self):
        """Le User-Agent spécifique SEC doit être préservé par _get sans être écrasé."""
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"

        with patch.object(self.provider, "_execute_get", new_callable=AsyncMock, return_value=mock_response) as mock_exec:
            res = await self.provider._get("https://www.sec.gov/test", headers=self.provider._sec_headers())
            self.assertEqual(res, "OK")
            # Vérifier que le User-Agent transmis est bien celui du SECEdgarProvider
            called_headers = mock_exec.call_args[0][1]
            self.assertEqual(called_headers["User-Agent"], "Fonrex contact@fonrex.io")


if __name__ == "__main__":
    unittest.main()

