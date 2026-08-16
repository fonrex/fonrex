"""
Tests du OpenFIGIProvider.

Mocks complets — aucune requête réseau réelle.
"""

import unittest
from unittest.mock import AsyncMock, patch

from financials.providers.openfigi import (
    FIGIMapping,
    OpenFIGIProvider,
    OpenFIGIResult,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

OPENFIGI_AAPL_RESPONSE = [
    {
        "data": [
            {
                "figi": "BBG000B9XRY4",
                "compositeFIGI": "BBG000B9XRY4",
                "shareClassFIGI": "BBG001S5N8V8",
                "ticker": "AAPL",
                "exchCode": "US",
                "marketIdentifierCode": "XNAS",
                "name": "APPLE INC",
                "marketSector": "Equity",
                "securityType": "Common Stock",
                "currency": "USD",
            }
        ]
    }
]

OPENFIGI_NOT_FOUND = [{"error": "No identifier found."}]

OPENFIGI_BATCH_RESPONSE = [
    {
        "data": [
            {
                "figi": "BBG000B9XRY4",
                "ticker": "AAPL",
                "exchCode": "US",
                "marketSector": "Equity",
            }
        ]
    },
    {"error": "No identifier found."},
    {
        "data": [
            {
                "figi": "BBG000BPH459",
                "ticker": "AIR",
                "exchCode": "FP",
                "marketIdentifierCode": "XPAR",
                "name": "AIRBUS SE",
                "currency": "EUR",
            }
        ]
    },
]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestOpenFIGIFetch(unittest.IsolatedAsyncioTestCase):
    """Tests du fetch unitaire."""

    def setUp(self):
        self.provider = OpenFIGIProvider()

    async def test_fetch_by_isin_success(self):
        """Fetch par ISIN → FIGIMapping correctement parsé."""
        with patch.object(
            self.provider, "_post_json", new_callable=AsyncMock, return_value=OPENFIGI_AAPL_RESPONSE
        ):
            result = await self.provider.fetch(isin="US0378331005")

        self.assertIsInstance(result, OpenFIGIResult)
        self.assertEqual(result.isin, "US0378331005")
        self.assertEqual(len(result.mappings), 1)
        self.assertEqual(result.mappings[0].figi, "BBG000B9XRY4")
        self.assertEqual(result.mappings[0].ticker, "AAPL")
        self.assertEqual(result.mappings[0].mic, "XNAS")
        self.assertEqual(result.mappings[0].currency, "USD")

    async def test_fetch_isin_not_found(self):
        """ISIN non trouvé → OpenFIGIResult avec mappings=[]."""
        with patch.object(
            self.provider, "_post_json", new_callable=AsyncMock, return_value=OPENFIGI_NOT_FOUND
        ):
            result = await self.provider.fetch(isin="XX0000000000")

        self.assertIsInstance(result, OpenFIGIResult)
        self.assertEqual(result.mappings, [])

    async def test_fetch_no_identifier_returns_none(self):
        """Sans ISIN ni ticker → None."""
        result = await self.provider.fetch()
        self.assertIsNone(result)

    async def test_fetch_api_failure_returns_empty(self):
        """Si l'API est KO → résultat vide sans exception."""
        with patch.object(self.provider, "_post_json", new_callable=AsyncMock, return_value=None):
            result = await self.provider.fetch(isin="US0378331005")

        self.assertIsInstance(result, OpenFIGIResult)
        self.assertEqual(result.mappings, [])


class TestOpenFIGIBatchFetch(unittest.IsolatedAsyncioTestCase):
    """Tests du fetch batch."""

    def setUp(self):
        self.provider = OpenFIGIProvider()

    async def test_fetch_batch_returns_correct_count(self):
        """Le batch retourne un résultat par ISIN (même pour les non-trouvés)."""
        with patch.object(
            self.provider,
            "_post_json",
            new_callable=AsyncMock,
            return_value=OPENFIGI_BATCH_RESPONSE,
        ):
            results = await self.provider.fetch_batch(
                ["US0378331005", "XX0000000000", "NL0000235190"]
            )

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].isin, "US0378331005")
        self.assertEqual(len(results[0].mappings), 1)
        # ISIN non trouvé
        self.assertEqual(results[1].isin, "XX0000000000")
        self.assertEqual(results[1].mappings, [])
        # Airbus
        self.assertEqual(results[2].isin, "NL0000235190")
        self.assertEqual(results[2].mappings[0].ticker, "AIR")

    async def test_fetch_batch_empty_list(self):
        """Un batch vide retourne une liste vide."""
        results = await self.provider.fetch_batch([])
        self.assertEqual(results, [])

    async def test_fetch_batch_chunks_at_100(self):
        """Plus de 100 ISINs → plusieurs appels POST."""
        call_count = 0

        async def mock_post(url, body, **kwargs):
            nonlocal call_count
            call_count += 1
            return [
                {"data": [{"figi": f"BBG{i:010d}", "ticker": f"T{i}"}]} for i in range(len(body))
            ]

        with patch.object(self.provider, "_post_json", side_effect=mock_post):
            results = await self.provider.fetch_batch([f"US{i:010d}" for i in range(150)])

        self.assertEqual(call_count, 2)  # 100 + 50
        self.assertEqual(len(results), 150)


class TestOpenFIGIRequestBody(unittest.TestCase):
    """Tests de la construction du body de requête."""

    def setUp(self):
        self.provider = OpenFIGIProvider()

    def test_build_request_body_format(self):
        body = self.provider._build_request_body(["US0378331005", "NL0000235190"])
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0], {"idType": "ID_ISIN", "idValue": "US0378331005"})
        self.assertEqual(body[1], {"idType": "ID_ISIN", "idValue": "NL0000235190"})

    def test_parse_response_all_fields(self):
        mapping = self.provider._parse_response(
            "US0378331005",
            {
                "figi": "BBG000B9XRY4",
                "compositeFIGI": "BBG000B9XRY4",
                "shareClassFIGI": "BBG001S5N8V8",
                "ticker": "AAPL",
                "exchCode": "US",
                "marketIdentifierCode": "XNAS",
                "name": "APPLE INC",
                "marketSector": "Equity",
                "securityType": "Common Stock",
                "currency": "USD",
            },
        )
        self.assertIsInstance(mapping, FIGIMapping)
        self.assertEqual(mapping.figi, "BBG000B9XRY4")
        self.assertEqual(mapping.ticker, "AAPL")
        self.assertEqual(mapping.mic, "XNAS")
        self.assertEqual(mapping.isin, "US0378331005")

    def test_parse_response_missing_fields(self):
        """Les champs manquants doivent être None, pas de KeyError."""
        mapping = self.provider._parse_response("XX0000000000", {"figi": "BBG000XXXXX"})
        self.assertEqual(mapping.figi, "BBG000XXXXX")
        self.assertIsNone(mapping.ticker)
        self.assertIsNone(mapping.mic)


class TestOpenFIGIHeaders(unittest.TestCase):
    """Tests des headers HTTP."""

    def test_headers_without_api_key(self):
        """Sans clé API, pas d'en-tête X-OPENFIGI-APIKEY."""
        provider = OpenFIGIProvider()
        provider.api_key = ""
        headers = provider._openfigi_headers()
        self.assertNotIn("X-OPENFIGI-APIKEY", headers)
        self.assertIn("Content-Type", headers)

    def test_headers_with_api_key(self):
        """Avec clé API, l'en-tête doit être présent."""
        provider = OpenFIGIProvider()
        provider.api_key = "test-key-12345"
        headers = provider._openfigi_headers()
        self.assertEqual(headers["X-OPENFIGI-APIKEY"], "test-key-12345")


if __name__ == "__main__":
    unittest.main()
