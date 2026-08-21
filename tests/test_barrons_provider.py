import unittest

from selectolax.parser import HTMLParser

from financials.providers.Barrons_provider import BarronsProvider


class BarronsProviderTest(unittest.TestCase):
    def setUp(self):
        self.provider = BarronsProvider()

    def test_parse_rating_sentiment_heuristics(self):
        html = """
        <div>
            <span>Analyst Rating</span>
            <span class="value">Overweight</span>
        </div>
        <div>
            <span>News Sentiment</span>
            <span class="value">Bullish</span>
        </div>
        """
        parser = HTMLParser(html)
        rating, sentiment = self.provider._parse_rating_sentiment(parser)

        self.assertEqual(rating, "Overweight")
        self.assertEqual(sentiment, "Bullish")

    def test_parse_rating_sentiment_classes(self):
        html = """
        <div>
            <div class="analyst-rating">Buy</div>
            <div class="sentiment-value">Bearish</div>
        </div>
        """
        parser = HTMLParser(html)
        rating, sentiment = self.provider._parse_rating_sentiment(parser)

        self.assertEqual(rating, "Buy")
        self.assertEqual(sentiment, "Bearish")

    def test_select_symbol_prefers_matching_french_isin_over_us_otc(self):
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
                    "exchange": "France: Euronext Paris",
                    "exchangeIsoCode": "XPAR",
                    "isin": "FR0000045072",
                    "ticker": "FR:ACA",
                    "type": "Stock",
                },
            ]
        }

        symbol = BarronsProvider._select_symbol(data, "FR0000045072")
        result = BarronsProvider._search_result_from_symbol(symbol, "FR0000045072")

        self.assertEqual(result["ticker"], "FR:ACA")
        self.assertEqual(result["url_ticker"], "ACA")
        self.assertEqual(result["country"], "FR")
        self.assertEqual(result["exchange"], "XPAR")
        self.assertEqual(result["charting_symbol"], "STOCK/FR/XPAR/ACA")
        self.assertEqual(
            result["provider_url"], "https://www.barrons.com/market-data/stocks/aca?countrycode=fr"
        )

    def test_normalize_query_supports_urls_suffixes_and_prefixed_tickers(self):
        self.assertEqual(
            BarronsProvider._normalize_query(
                "https://www.barrons.com/market-data/stocks/aca?countrycode=fr"
            ),
            ("FR:ACA", "FR"),
        )
        self.assertEqual(
            BarronsProvider._normalize_query(
                "https://www.barrons.com/market-data/stocks/sap?countrycode=de"
            ),
            ("XE:SAP", "XE"),
        )
        self.assertEqual(
            BarronsProvider._normalize_query("https://www.barrons.com/market-data/stocks/aapl"),
            ("AAPL", "US"),
        )
        self.assertEqual(BarronsProvider._normalize_query("ACA.PA"), ("FR:ACA", "FR"))
        self.assertEqual(BarronsProvider._normalize_query("DE:SAP"), ("XE:SAP", "XE"))
        self.assertEqual(BarronsProvider._normalize_query("   "), ("", None))


if __name__ == "__main__":
    unittest.main()
