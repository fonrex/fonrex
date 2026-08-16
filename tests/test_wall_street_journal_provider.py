import unittest

from financials.providers.wallStreetJournal_provider import WallStreetJournalProvider


class WallStreetJournalProviderTest(unittest.TestCase):
    def test_search_result_from_charting_symbol_builds_quote_url(self):
        symbol = {
            "score": 1,
            "chartingSymbol": "STOCK/FR/XPAR/ACA",
            "company": "Credit Agricole S.A.",
            "country": "FR",
            "exchange": "France: Euronext Paris",
            "exchangeIsoCode": "XPAR",
            "isin": "FR0000045072",
            "ticker": "FR:ACA",
            "type": "Stock",
        }

        result = WallStreetJournalProvider._search_result_from_symbol(symbol, "FR0000045072")

        self.assertEqual(result["url_ticker"], "ACA")
        self.assertEqual(result["country"], "FR")
        self.assertEqual(result["exchange"], "XPAR")
        self.assertEqual(result["isin"], "FR0000045072")
        self.assertEqual(result["name"], "Credit Agricole S.A.")
        self.assertEqual(
            result["provider_url"], "https://www.wsj.com/market-data/quotes/FR/XPAR/ACA"
        )

    def test_select_symbol_prefers_matching_french_isin(self):
        data = {
            "symbols": [
                {
                    "score": 0.74,
                    "chartingSymbol": "STOCK/US/OOTC/CRARF",
                    "company": "Credit Agricole S.A.",
                    "country": "US",
                    "exchangeIsoCode": "OOTC",
                    "isin": "FR0000045072",
                    "ticker": "CRARF",
                    "type": "Stock",
                },
                {
                    "score": 1,
                    "chartingSymbol": "STOCK/FR/XPAR/ACA",
                    "company": "Credit Agricole S.A.",
                    "country": "FR",
                    "exchangeIsoCode": "XPAR",
                    "isin": "FR0000045072",
                    "ticker": "FR:ACA",
                    "type": "Stock",
                },
            ]
        }

        symbol = WallStreetJournalProvider._select_symbol(data, "FR0000045072")

        self.assertEqual(symbol["ticker"], "FR:ACA")

    def test_metrics_can_fall_back_to_search_result_when_page_is_blocked(self):
        metrics = WallStreetJournalProvider._metrics_from_search_result(
            {
                "provider_url": "https://www.wsj.com/market-data/quotes/FR/XPAR/ACA",
                "name": "Credit Agricole S.A.",
                "url_ticker": "ACA",
                "isin": "FR0000045072",
                "exchange": "XPAR",
                "exchange_name": "France: Euronext Paris",
                "instrument_type": "Stock",
            },
            "FR0000045072",
        )

        self.assertEqual(metrics.ticker, "ACA")
        self.assertEqual(metrics.isin, "FR0000045072")
        self.assertEqual(metrics.name, "Credit Agricole S.A.")
        self.assertEqual(metrics.exchange, "XPAR")
        self.assertEqual(metrics.provider_url, "https://www.wsj.com/market-data/quotes/FR/XPAR/ACA")


if __name__ == "__main__":
    unittest.main()
