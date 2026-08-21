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

if __name__ == "__main__":
    unittest.main()
