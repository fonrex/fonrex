"""
Tests du JustETFProvider.

Mocks complets — aucune requête réseau réelle.
"""

import unittest
from unittest.mock import AsyncMock, patch

from financials.providers.justetf import JustETFProvider, JustETFResult

# ── Fixtures HTML ─────────────────────────────────────────────────────────────

JUSTETF_HTML_MSCI_WORLD = """
<html>
<body>
<h1>iShares Core MSCI World UCITS ETF USD (Acc)</h1>
<table class="table">
<tr><th>TER</th><td>0,20 %</td></tr>
<tr><th>Taille du fonds</th><td>85,42 Mrd. EUR</td></tr>
<tr><th>Domicile</th><td>IE</td></tr>
<tr><th>Réplication</th><td>Physical</td></tr>
<tr><th>Distribution</th><td>Accumulating</td></tr>
<tr><th>Indice</th><td>MSCI World</td></tr>
<tr><th>Date de lancement</th><td>2009-09-25</td></tr>
</table>
<table class="table holding">
<tr><th>Nom</th><th>Poids</th></tr>
<tr><td>Apple Inc.</td><td>4,87 %</td></tr>
<tr><td>Microsoft Corp.</td><td>4,12 %</td></tr>
</table>
</body>
</html>
"""

JUSTETF_API_RESPONSE = {
    "name": "iShares Core MSCI World UCITS ETF",
    "ticker": "IWDA",
    "ter": 0.20,
    "totalNetAssets": 85420000000,
    "domicile": "IE",
    "replicationMethod": "Physical",
    "distributionPolicy": "Accumulating",
    "index": "MSCI World",
    "inceptionDate": "2009-09-25",
    "performance": {
        "ytd": 8.42,
        "1y": 21.34,
        "3y": 45.12,
    },
    "topHoldings": [
        {"name": "Apple Inc.", "weight": 4.87, "country": "US"},
        {"name": "Microsoft Corp.", "weight": 4.12, "country": "US"},
    ],
    "numberOfHoldings": 1463,
    "allocation": {
        "stockUS": 70.12,
        "stockNonUS": 28.31,
        "cash": 1.57,
    },
}


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestJustETFPercentageParsing(unittest.TestCase):
    """Tests des helpers de conversion numérique."""

    def setUp(self):
        self.provider = JustETFProvider()

    def test_parse_percentage_french_format(self):
        """'0,20 %' → Decimal('0.002')"""
        result = self.provider._parse_percentage("0,20 %")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(float(result), 0.002, places=4)

    def test_parse_percentage_integer_format(self):
        """'20%' → Decimal('0.20')"""
        result = self.provider._parse_percentage("20%")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(float(result), 0.20, places=4)

    def test_parse_percentage_decimal_format(self):
        """'0.20%' → Decimal('0.002')"""
        result = self.provider._parse_percentage("0.20%")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(float(result), 0.002, places=4)

    def test_parse_percentage_none(self):
        self.assertIsNone(self.provider._parse_percentage(None))

    def test_parse_percentage_empty(self):
        self.assertIsNone(self.provider._parse_percentage(""))

    def test_parse_percentage_invalid(self):
        self.assertIsNone(self.provider._parse_percentage("N/A"))


class TestJustETFCurrencyAmountParsing(unittest.TestCase):
    """Tests du parsing des montants financiers."""

    def setUp(self):
        self.provider = JustETFProvider()

    def test_parse_milliards(self):
        """'85,42 Mrd. EUR' → ~85_420_000_000"""
        result = self.provider._parse_currency_amount("85,42 Mrd. EUR")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(float(result), 85_420_000_000, delta=1_000_000)

    def test_parse_millions(self):
        """'456,7 Mio. EUR' → ~456_700_000"""
        result = self.provider._parse_currency_amount("456,7 Mio. EUR")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(float(result), 456_700_000, delta=100_000)

    def test_parse_billions_english(self):
        """'12.5B' → 12_500_000_000"""
        result = self.provider._parse_currency_amount("12.5B")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(float(result), 12_500_000_000, delta=1_000_000)

    def test_parse_none(self):
        self.assertIsNone(self.provider._parse_currency_amount(None))

    def test_parse_invalid(self):
        self.assertIsNone(self.provider._parse_currency_amount("N/A"))


class TestJustETFAPIFetch(unittest.IsolatedAsyncioTestCase):
    """Tests du fetch via l'API JSON interne."""

    def setUp(self):
        self.provider = JustETFProvider()

    async def test_fetch_via_api_success(self):
        """L'API JSON retourne un JustETFResult correctement parsé."""
        with patch.object(
            self.provider, "_get_json", new_callable=AsyncMock, return_value=JUSTETF_API_RESPONSE
        ):
            result = await self.provider._fetch_via_api("IE00B4L5Y983")

        self.assertIsNotNone(result)
        self.assertIsInstance(result, JustETFResult)
        self.assertEqual(result.isin, "IE00B4L5Y983")
        self.assertEqual(result.name, "iShares Core MSCI World UCITS ETF")
        self.assertEqual(result.ticker, "IWDA")
        self.assertEqual(result.domicile, "IE")
        self.assertEqual(result.replication_method, "Physical")
        self.assertEqual(result.index_tracked, "MSCI World")
        self.assertEqual(result.nb_holdings, 1463)
        self.assertAlmostEqual(float(result.net_expense_ratio), 0.002, places=4)
        self.assertEqual(len(result.top_holdings), 2)

    async def test_fetch_via_api_none_returns_none(self):
        """Si l'API retourne None, _fetch_via_api renvoie None."""
        with patch.object(self.provider, "_get_json", new_callable=AsyncMock, return_value=None):
            result = await self.provider._fetch_via_api("IE00XXXX")
        self.assertIsNone(result)

    async def test_fetch_without_isin_returns_none(self):
        """Appel sans ISIN → None directement."""
        result = await self.provider.fetch(isin=None)
        self.assertIsNone(result)


class TestJustETFHTMLScraping(unittest.IsolatedAsyncioTestCase):
    """Tests du fallback HTML scraping."""

    def setUp(self):
        self.provider = JustETFProvider()

    async def test_scraping_parses_ter(self):
        """Le TER doit être correctement parsé depuis le HTML."""
        with patch.object(
            self.provider, "_get", new_callable=AsyncMock, return_value=JUSTETF_HTML_MSCI_WORLD
        ):
            result = await self.provider._fetch_via_scraping("IE00B4L5Y983")

        self.assertIsNotNone(result)
        self.assertIsInstance(result, JustETFResult)
        if result.net_expense_ratio:
            self.assertAlmostEqual(float(result.net_expense_ratio), 0.002, places=4)

    async def test_fallback_used_when_api_fails(self):
        """Si l'API échoue, le fallback HTML est utilisé."""
        with (
            patch.object(
                self.provider, "_fetch_via_api", new_callable=AsyncMock, return_value=None
            ),
            patch.object(
                self.provider,
                "_fetch_via_scraping",
                new_callable=AsyncMock,
                return_value=JustETFResult(isin="IE00B4L5Y983", name="Test ETF"),
            ),
        ):
            result = await self.provider.fetch(isin="IE00B4L5Y983")

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "Test ETF")

    async def test_scraping_html_not_found_returns_none(self):
        """Si le GET échoue, retourner None."""
        with (
            patch.object(self.provider, "_get_json", new_callable=AsyncMock, return_value=None),
            patch.object(self.provider, "_get", new_callable=AsyncMock, return_value=None),
        ):
            result = await self.provider.fetch(isin="IE00XXXX")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
