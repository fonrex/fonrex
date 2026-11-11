import unittest

from selectolax.parser import HTMLParser

from financials.providers.Barrons_provider import BarronsProvider


class BarronsProviderTest(unittest.TestCase):
    def setUp(self):
        self.provider = BarronsProvider()

    def test_select_symbol_prefers_us_exact_stock_match(self):
        data = {
            "symbols": [
                {
                    "score": 0.62,
                    "ticker": "MX:TSLA",
                    "country": "MX",
                    "type": "Stock",
                    "isin": "US88160R1014",
                    "company": "Tesla Inc.",
                },
                {
                    "score": 1,
                    "ticker": "TSLA",
                    "country": "US",
                    "type": "Stock",
                    "isin": "US88160R1014",
                    "company": "Tesla Inc.",
                },
            ]
        }

        symbol = BarronsProvider._select_symbol(data, "TSLA")
        result = BarronsProvider._search_result_from_symbol(symbol, "TSLA")

        self.assertEqual(result["ticker"], "TSLA")
        self.assertEqual(result["url_ticker"], "TSLA")
        self.assertEqual(result["isin"], "US88160R1014")
        self.assertEqual(result["provider_url"], "https://www.barrons.com/market-data/stocks/tsla")

    def test_metrics_can_be_returned_from_search_result_without_page_parse(self):
        metrics = BarronsProvider._metrics_from_search_result(
            {
                "ticker": "TSLA",
                "url_ticker": "TSLA",
                "isin": "US88160R1014",
                "name": "Tesla Inc.",
                "provider_url": "https://www.barrons.com/market-data/stocks/tsla",
            },
            "TSLA",
        )

        self.assertEqual(metrics.name, "Tesla Inc.")
        self.assertEqual(metrics.ticker, "TSLA")
        self.assertEqual(metrics.isin, "US88160R1014")
        self.assertEqual(metrics.provider_url, "https://www.barrons.com/market-data/stocks/tsla")

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

    def test_parse_rating_sentiment_reads_the_label_before_the_value(self):
        html = """
        <section>
            <div><span>Credit Rating</span><span class="value">AA-</span></div>
            <div><span>ESG Rating</span><span class="data">Low risk</span></div>
            <div>
                <p>A container whose text is far longer than a single label</p>
                <span>Analysts' Ratings</span>
                <span class="value">Overweight</span>
            </div>
            <ul><li>Market Sentiment</li><li class="data">Bearish</li></ul>
        </section>
        """
        rating, sentiment = self.provider._parse_rating_sentiment(HTMLParser(html))

        self.assertEqual(rating, "Overweight")
        self.assertEqual(sentiment, "Bearish")

    def test_parse_rating_sentiment_without_label(self):
        html = '<div><span class="value">Overweight</span></div>'
        rating, sentiment = self.provider._parse_rating_sentiment(HTMLParser(html))

        self.assertIsNone(rating)
        self.assertIsNone(sentiment)

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

    def test_parse_average_score_scale_and_next_data_rating(self):
        html = """
        <html>
        <head>
          <style>.emotion-1hddjdp-StyledMarker +span{font-weight:bold;}</style>
        </head>
        <body>
          <div class="analyst-ratings" data-id="AnalystRatings_Wrapper">
            <p>Average Score:</p>
            <div data-id="AnalystRatings_ScaleContainer">
              <div data-id="AnalystRatings_ScaleBlock">
                <svg class="emotion-1l73fvh-StyledMarker"></svg>
                <span data-id="AnalystRatings_ScaleText">Sell</span>
              </div>
              <div data-id="AnalystRatings_ScaleBlock">
                <svg class="emotion-1hddjdp-StyledMarker"></svg>
                <span data-id="AnalystRatings_ScaleText">Hold</span>
              </div>
            </div>
          </div>
        </body>
        </html>
        """
        metrics = self.provider._parse_page(HTMLParser(html), "ACA")
        self.assertEqual(metrics.rating, "Hold")

        next_only_html = """
        <html><body>
          <script id="__NEXT_DATA__" type="application/json">
            {"props":{"pageProps":{"analystRatingsBarCard":{"rating":3,"totalRatings":18}}}}
          </script>
        </body></html>
        """
        metrics_next = self.provider._parse_page(HTMLParser(next_only_html), "ACA")
        self.assertEqual(metrics_next.rating, "Over")
        self.assertEqual(metrics_next.analyst_count, 18)

        no_count_html = next_only_html.replace('"totalRatings":18', '"totalRatings":true')
        metrics_no_count = self.provider._parse_page(HTMLParser(no_count_html), "ACA")
        self.assertIsNone(metrics_no_count.analyst_count)

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
