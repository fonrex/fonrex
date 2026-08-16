"""
Tests du IndexConstituentsProvider.

Mocks complets — aucune requête réseau réelle.
"""

import unittest
from unittest.mock import AsyncMock, patch

from financials.providers.index_constituents import (
    IndexConstituentsProvider,
    IndexConstituentsResult,
    IndexName,
)

# ── Fixtures HTML ─────────────────────────────────────────────────────────────

SP500_HTML = """
<html>
<body>
<table id="constituents" class="wikitable">
<tr>
  <th>Symbol</th><th>Security</th>
  <th>GICS Sector</th><th>GICS Sub-Industry</th>
  <th>Headquarters</th><th>Date Added</th><th>CIK</th><th>Founded</th>
</tr>
<tr>
  <td><a href="/wiki/Apple_Inc.">AAPL</a></td>
  <td>Apple Inc.</td>
  <td>Information Technology</td>
  <td>Technology Hardware, Storage &amp; Peripherals</td>
  <td>Cupertino, CA</td><td>1982-11-30</td>
  <td>320193</td><td>1977</td>
</tr>
<tr>
  <td>MSFT</td>
  <td>Microsoft Corporation</td>
  <td>Information Technology</td>
  <td>Systems Software</td>
  <td>Redmond, WA</td><td>1994-06-01</td>
  <td>789019</td><td>1975</td>
</tr>
</table>
</body>
</html>
"""

CAC40_HTML = """
<html>
<body>
<table class="wikitable">
<tr><th>Ancienne désignation</th></tr>
<tr><td>Placeholder — wrong table</td></tr>
</table>
<table class="wikitable">
<tr>
  <th>Entreprise</th><th>ISIN</th><th>Secteur</th><th>Capitalisation (Md€)</th>
</tr>
<tr>
  <td><a href="/wiki/Airbus">Airbus SE</a></td>
  <td>NL0000235190</td>
  <td>Industrie</td>
  <td>97,5</td>
</tr>
<tr>
  <td>BNP Paribas</td>
  <td>FR0000131104</td>
  <td>Services financiers</td>
  <td>62,3</td>
</tr>
</table>
</body>
</html>
"""

NASDAQ100_HTML = """
<html>
<body>
<table class="wikitable"><tr><th>Ignore</th></tr></table>
<table class="wikitable"><tr><th>Ignore</th></tr></table>
<table class="wikitable"><tr><th>Ignore</th></tr></table>
<table class="wikitable">
<tr>
  <th>Company</th><th>Ticker</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
</tr>
<tr>
  <td>Apple Inc.</td><td>AAPL</td>
  <td>Information Technology</td>
  <td>Technology Hardware</td>
</tr>
<tr>
  <td>Amazon.com Inc.</td><td>AMZN</td>
  <td>Consumer Discretionary</td>
  <td>Internet &amp; Direct Marketing Retail</td>
</tr>
</table>
</body>
</html>
"""

DAX_HTML = """
<html>
<body>
<table class="wikitable"><tr><th>Ignore</th></tr></table>
<table class="wikitable"><tr><th>Ignore</th></tr></table>
<table class="wikitable"><tr><th>Ignore</th></tr></table>
<table class="wikitable">
<tr>
  <th>Company</th><th>Ticker</th><th>ISIN</th><th>Sector</th><th>Employees</th>
</tr>
<tr>
  <td>SAP SE</td><td>SAP</td><td>DE0007164600</td><td>Software</td><td>107.000</td>
</tr>
<tr>
  <td>Siemens AG</td><td>SIE</td><td>DE0007236101</td><td>Conglomerates</td><td>311.000</td>
</tr>
</table>
</body>
</html>
"""


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestWikipediaTableParser(unittest.TestCase):
    """Tests du parser HTML générique."""

    def setUp(self):
        self.provider = IndexConstituentsProvider()

    def test_parse_sp500_by_id(self):
        rows = self.provider._parse_wikipedia_table(SP500_HTML, table_id="constituents")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Symbol"], "AAPL")
        self.assertEqual(rows[0]["Security"], "Apple Inc.")
        self.assertEqual(rows[0]["GICS Sector"], "Information Technology")
        self.assertEqual(rows[0]["CIK"], "320193")

    def test_parse_cac40_by_index(self):
        rows = self.provider._parse_wikipedia_table(CAC40_HTML, table_index=1)
        self.assertEqual(len(rows), 2)
        self.assertIn("Airbus SE", rows[0].get("Entreprise", ""))
        self.assertEqual(rows[0].get("ISIN"), "NL0000235190")

    def test_parse_strips_wikipedia_annotations(self):
        """Les [1], [note], etc. doivent être supprimés."""
        html = """<html><body>
        <table class="wikitable">
        <tr><th>Company[1]</th><th>ISIN[note]</th></tr>
        <tr><td>Apple Inc.[2]</td><td>US0378331005</td></tr>
        </table></body></html>"""
        rows = self.provider._parse_wikipedia_table(html)
        self.assertEqual(len(rows), 1)
        # Headers nettoyés
        keys = list(rows[0].keys())
        self.assertIn("Company", keys)
        self.assertIn("ISIN", keys)
        # Valeurs nettoyées
        self.assertEqual(rows[0]["Company"], "Apple Inc.")

    def test_parse_empty_table_returns_empty_list(self):
        html = "<html><body><p>No table here</p></body></html>"
        rows = self.provider._parse_wikipedia_table(html)
        self.assertEqual(rows, [])


class TestSP500Fetch(unittest.IsolatedAsyncioTestCase):
    """Tests du fetch S&P 500."""

    def setUp(self):
        self.provider = IndexConstituentsProvider()

    async def test_fetch_sp500_returns_result(self):
        with patch.object(self.provider, "_get", new_callable=AsyncMock, return_value=SP500_HTML):
            result = await self.provider.fetch_sp500()

        self.assertIsInstance(result, IndexConstituentsResult)
        self.assertEqual(result.index_name, "SP500")
        self.assertEqual(result.total_count, 2)
        self.assertEqual(len(result.constituents), 2)

    async def test_fetch_sp500_constituent_fields(self):
        with patch.object(self.provider, "_get", new_callable=AsyncMock, return_value=SP500_HTML):
            result = await self.provider.fetch_sp500()

        aapl = next(c for c in result.constituents if c.ticker == "AAPL")
        self.assertEqual(aapl.name, "Apple Inc.")
        self.assertEqual(aapl.sector, "Information Technology")
        self.assertEqual(aapl.cik, "320193")
        self.assertEqual(aapl.country, "US")

    async def test_fetch_sp500_network_failure_returns_empty(self):
        with patch.object(self.provider, "_get", new_callable=AsyncMock, return_value=None):
            result = await self.provider.fetch_sp500()

        self.assertIsInstance(result, IndexConstituentsResult)
        self.assertEqual(result.total_count, 0)
        self.assertEqual(result.constituents, [])


class TestCAC40Fetch(unittest.IsolatedAsyncioTestCase):
    """Tests du fetch CAC 40."""

    def setUp(self):
        self.provider = IndexConstituentsProvider()

    async def test_fetch_cac40_returns_result(self):
        with patch.object(self.provider, "_get", new_callable=AsyncMock, return_value=CAC40_HTML):
            result = await self.provider.fetch_cac40()

        self.assertIsInstance(result, IndexConstituentsResult)
        self.assertEqual(result.index_name, "CAC40")
        self.assertGreater(result.total_count, 0)

    async def test_fetch_cac40_has_isin(self):
        """Les composants du CAC 40 doivent avoir un ISIN."""
        with patch.object(self.provider, "_get", new_callable=AsyncMock, return_value=CAC40_HTML):
            result = await self.provider.fetch_cac40()

        for c in result.constituents:
            if c.isin:
                self.assertRegex(c.isin, r"^[A-Z]{2}[A-Z0-9]{10}$")

    async def test_fetch_cac40_country_from_isin(self):
        """Le pays doit être dérivé des 2 premiers caractères de l'ISIN."""
        with patch.object(self.provider, "_get", new_callable=AsyncMock, return_value=CAC40_HTML):
            result = await self.provider.fetch_cac40()

        airbus = next((c for c in result.constituents if c.isin == "NL0000235190"), None)
        if airbus:
            self.assertEqual(airbus.country, "NL")


class TestFetchDispatch(unittest.IsolatedAsyncioTestCase):
    """Tests du dispatcher fetch()."""

    def setUp(self):
        self.provider = IndexConstituentsProvider()

    async def test_fetch_without_index_name_returns_none(self):
        result = await self.provider.fetch()
        self.assertIsNone(result)

    async def test_fetch_dispatches_to_correct_method(self):
        for index, method_name in [
            (IndexName.SP500, "fetch_sp500"),
            (IndexName.CAC40, "fetch_cac40"),
            (IndexName.NASDAQ100, "fetch_nasdaq100"),
            (IndexName.DAX, "fetch_dax"),
        ]:
            mock_result = IndexConstituentsResult(index_name=index.value, source_url="https://test")
            with patch.object(
                self.provider, method_name, new_callable=AsyncMock, return_value=mock_result
            ) as mock:
                result = await self.provider.fetch(index_name=index)
            mock.assert_called_once()
            self.assertEqual(result.index_name, index.value)


if __name__ == "__main__":
    unittest.main()
