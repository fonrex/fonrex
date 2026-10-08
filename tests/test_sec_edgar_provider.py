"""
SECEdgarProvider unit tests.

Full mocks — no actual network requests.
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

# Shape of https://data.sec.gov/submissions/CIK0000320193.json (October 2026):
# ``primaryDocument`` names the version rendered for reading, in an ``xsl...`` folder.
SUBMISSIONS_JSON = {
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "form": ["4", "4", "10-K", "4"],
            "filingDate": ["2026-04-15", "2026-03-10", "2026-02-01", "2026-01-20"],
            "accessionNumber": [
                "0001140361-26-038307",
                "0000320193-26-000050",
                "0000320193-26-000010",
                "0000320193-25-000900",
            ],
            "primaryDocument": [
                "xslF345X06/form4.xml",
                "xslF345X06/form4.xml",
                "aapl-20260926.htm",
                "xslF345X05/wk-form4_1737400000.xml",
            ],
        }
    },
}

ARCHIVES = "https://www.sec.gov/Archives/edgar/data/320193"
FORM4_URLS = [
    f"{ARCHIVES}/000114036126038307/form4.xml",
    f"{ARCHIVES}/000032019326000050/form4.xml",
    f"{ARCHIVES}/000032019325000900/wk-form4_1737400000.xml",
]

# Links of the index page of a filing, as served by sec.gov: the rendered
# version comes first, the data file second.
INDEX_HTML = """
<html>
<body>
<a href="/Archives/edgar/data/320193/000114036126038307/xslF345X06/form4.xml">form4.html</a>
<a href="/Archives/edgar/data/320193/000114036126038307/form4.xml">form4.xml</a>
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
    """CIK resolution tests."""

    def setUp(self):
        self.provider = SECEdgarProvider()

    async def test_resolve_cik_via_static_aapl(self):
        """Should find AAPL CIK in static JSON."""
        with patch.object(
            self.provider, "_get_json", new_callable=AsyncMock, return_value=COMPANY_TICKERS_JSON
        ):
            cik = await self.provider._resolve_cik_via_static("AAPL")
        self.assertEqual(cik, "0000320193")

    async def test_resolve_cik_strips_exchange_suffix(self):
        """The .PA suffix must be removed before search."""
        with patch.object(
            self.provider, "_get_json", new_callable=AsyncMock, return_value=COMPANY_TICKERS_JSON
        ):
            cik = await self.provider._resolve_cik_via_static("MSFT.US")
        self.assertEqual(cik, "0000789019")

    async def test_resolve_cik_not_found_returns_none(self):
        """An EU ticker without CIK must return None."""
        with patch.object(
            self.provider, "_get_json", new_callable=AsyncMock, return_value=COMPANY_TICKERS_JSON
        ):
            cik = await self.provider._resolve_cik_via_static("AIR.PA")
        self.assertIsNone(cik)

    async def test_resolve_cik_api_failure_returns_none(self):
        """If API fails, return None without raising an exception."""
        with patch.object(self.provider, "_get_json", new_callable=AsyncMock, return_value=None):
            cik = await self.provider._resolve_cik_via_static("AAPL")
        self.assertIsNone(cik)


class TestSECEdgarFetch(unittest.IsolatedAsyncioTestCase):
    """Full fetch tests."""

    def setUp(self):
        self.provider = SECEdgarProvider()

    async def test_fetch_eu_ticker_returns_empty_result(self):
        """An EU ticker without CIK → empty InsiderTransactionsResult, no error."""
        with patch.object(self.provider, "_resolve_cik", new_callable=AsyncMock, return_value=None):
            result = await self.provider.fetch(ticker="AIR.PA")
        self.assertIsInstance(result, InsiderTransactionsResult)
        self.assertEqual(result.ticker, "AIR.PA")
        self.assertEqual(result.transactions, [])
        self.assertEqual(result.total_count, 0)

    def _network(self, pages: dict):
        """Answer only the exact URLs of ``pages``; every request is recorded."""
        requested = []

        async def get_json(url, **kwargs):
            requested.append(url)
            if "company_tickers" in url:
                return COMPANY_TICKERS_JSON
            if url == "https://data.sec.gov/submissions/CIK0000320193.json":
                return pages.get("submissions", SUBMISSIONS_JSON)
            return None

        async def get(url, **kwargs):
            requested.append(url)
            return pages.get(url)

        return (
            patch.object(self.provider, "_get_json", side_effect=get_json),
            patch.object(self.provider, "_get", side_effect=get),
            requested,
        )

    async def test_fetch_reads_each_form4_at_its_real_address(self):
        """The data file sits in the folder of the filing, beside its rendered version.

        The former address (an index page named without dashes) answers 503 on
        sec.gov: no transaction was ever returned and each request waited 45 s.
        """
        json_patch, get_patch, requested = self._network(dict.fromkeys(FORM4_URLS, FORM4_XML))
        with json_patch, get_patch:
            result = await self.provider.fetch(ticker="AAPL", limit=5)

        self.assertEqual(result.cik, "0000320193")
        self.assertEqual(result.company_name, "Apple Inc.")
        self.assertEqual(result.total_count, 3)
        self.assertEqual([url for url in requested if "Archives" in url], FORM4_URLS)
        # The company and its filings come from one document, requested once.
        self.assertEqual(sum("submissions" in url for url in requested), 1)

    async def test_fetch_parses_transaction_correctly(self):
        json_patch, get_patch, _requested = self._network({FORM4_URLS[0]: FORM4_XML})
        with json_patch, get_patch:
            result = await self.provider.fetch(ticker="AAPL", limit=5)

        self.assertEqual(result.total_count, 1)
        txn = result.transactions[0]
        self.assertEqual(txn.insider_name, "Cook Timothy D")
        self.assertEqual(txn.insider_title, "Chief Executive Officer")
        self.assertEqual(txn.transaction_type, "Sell")
        self.assertEqual(txn.shares, 50000)
        self.assertAlmostEqual(txn.price_per_share, 172.45, places=2)
        self.assertEqual(txn.shares_owned_after, 3298456)
        self.assertEqual(txn.total_value, 8622500.0)
        self.assertEqual(txn.filing_date, date(2026, 4, 15))
        # The link given to the reader is the index page, named with the dashes.
        self.assertEqual(
            txn.sec_filing_url,
            f"{ARCHIVES}/000114036126038307/0001140361-26-038307-index.htm",
        )

    async def test_filing_without_document_name_is_found_through_its_index(self):
        submissions = {
            "name": "Apple Inc.",
            "filings": {
                "recent": {
                    "form": ["4"],
                    "filingDate": ["2026-04-15"],
                    "accessionNumber": ["0001140361-26-038307"],
                }
            },
        }
        index_url = f"{ARCHIVES}/000114036126038307/0001140361-26-038307-index.htm"
        json_patch, get_patch, requested = self._network(
            {"submissions": submissions, index_url: INDEX_HTML, FORM4_URLS[0]: FORM4_XML}
        )
        with json_patch, get_patch:
            result = await self.provider.fetch(ticker="AAPL", limit=5)

        self.assertEqual(result.total_count, 1)
        self.assertEqual(requested[-2:], [index_url, FORM4_URLS[0]])

    async def test_filing_without_data_file_is_skipped_without_request(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "filingDate": ["1999-04-15"],
                    "accessionNumber": ["0000320193-99-000007"],
                    "primaryDocument": ["0000320193-99-000007.txt"],
                }
            },
        }
        json_patch, get_patch, requested = self._network({"submissions": submissions})
        with json_patch, get_patch:
            result = await self.provider.fetch(ticker="AAPL", limit=5)

        self.assertEqual(result.transactions, [])
        self.assertFalse([url for url in requested if "Archives" in url])


class TestSECEdgarRateLimit(unittest.IsolatedAsyncioTestCase):
    """Verifies that semaphore is properly defined at class level."""

    def test_semaphore_is_class_level(self):
        """Semaphore must be shared across all instances."""
        p1 = SECEdgarProvider()
        p2 = SECEdgarProvider()
        self.assertIs(type(p1)._semaphore, type(p2)._semaphore)
        self.assertIsNotNone(SECEdgarProvider._semaphore)


class TestSECEdgarXMLParsing(unittest.TestCase):
    """Form 4 XML parser unit tests."""

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

    def test_form_without_transaction_code_is_not_called_a_sale(self):
        without_code = FORM4_XML.replace("<transactionCode>S</transactionCode>", "")
        txn = self.provider._parse_form4_xml(without_code, date(2026, 4, 15), "https://x/")[0]
        self.assertEqual((txn.transaction_code, txn.transaction_type), (None, "Unknown"))

    def test_amount_is_rounded_to_the_cent(self):
        """2399 shares at 336.18 gave 806495.8200000001."""
        real = FORM4_XML.replace("<value>50000</value>", "<value>2399</value>").replace(
            "<value>172.45</value>", "<value>336.18</value>"
        )
        txn = self.provider._parse_form4_xml(real, date(2026, 10, 1), "https://x/")[0]
        self.assertEqual(txn.total_value, 806495.82)

    def test_price_written_without_leading_zero_is_read(self):
        """``xs:decimal`` allows ``.01``."""
        cents = FORM4_XML.replace("<value>172.45</value>", "<value>.01</value>")
        txn = self.provider._parse_form4_xml(cents, date(2026, 4, 15), "https://x/")[0]
        self.assertEqual(txn.transaction_code, "S")
        self.assertAlmostEqual(txn.price_per_share, 0.01)
        self.assertAlmostEqual(txn.total_value, 500.0)

    def test_parse_form4_xml_invalid_xml(self):
        """Invalid XML must not raise an exception."""
        txns = self.provider._parse_form4_xml(
            "<invalid>xml<unclosed>", date(2026, 1, 1), "https://test.com/"
        )
        self.assertEqual(txns, [])


class TestSECEdgarCircuitBreakerAndUA(unittest.IsolatedAsyncioTestCase):
    """Circuit breaker and User-Agent tests."""

    def setUp(self):
        self.provider = SECEdgarProvider()

    async def test_consecutive_http_failures_breaks_early(self):
        """If _get returns None (HTTP failure) 3 consecutive times, loop terminates."""
        submissions = {
            "name": "Test Company",
            "filings": {
                "recent": {
                    "form": ["4"] * 10,
                    "filingDate": ["2026-01-01"] * 10,
                    "accessionNumber": [f"0000320193-26-00000{i}" for i in range(10)],
                    "primaryDocument": ["xslF345X06/form4.xml"] * 10,
                }
            },
        }

        async def mock_get(url, **kwargs):
            return None  # Always HTTP failure

        with patch.object(self.provider, "_get", side_effect=mock_get) as mock_get_call:
            txns = await self.provider._form4_transactions("320193", submissions, limit=10)

        self.assertEqual(txns, [])
        # Should stop after exactly 3 consecutive failures instead of attempting 10 filings
        self.assertEqual(mock_get_call.call_count, 3)

    async def test_custom_user_agent_preserved_in_base_provider(self):
        """Specific SEC User-Agent must be preserved by _get without being overwritten."""
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"

        with patch.object(self.provider, "_execute_get", new_callable=AsyncMock, return_value=mock_response) as mock_exec:
            res = await self.provider._get("https://www.sec.gov/test", headers=self.provider._sec_headers())
            self.assertEqual(res, "OK")
            # Verify that the passed User-Agent is the SECEdgarProvider one
            called_headers = mock_exec.call_args[0][1]
            self.assertEqual(called_headers["User-Agent"], "Fonrex contact@fonrex.io")


if __name__ == "__main__":
    unittest.main()


