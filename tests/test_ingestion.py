import json
import unittest

from src.ingestion.gamma import normalize_market
from src.ingestion.clob import quote_from_book
from src.ingestion.data_api import normalize_history
from src.ingestion.news import rss_records
from src.common import timestamp


class IngestionTests(unittest.TestCase):
    def test_yes_token_not_first(self):
        raw=dict(id=1,conditionId='c',outcomes='["No","Yes"]',clobTokenIds='["n","y"]',
            events=[{'id':'event'}],question='Question?',description='Rules',endDate='2027-01-01T00:00:00Z',active=True,closed=False)
        market,version=normalize_market(raw,100,'ref')
        self.assertEqual(market['yes_token'],'y')
        self.assertEqual(version['available_at'],100)

    def test_settlement_excluded(self):
        payload={'data':[{'timestamp':10,'price':1,'resolution_seconds':0},
                          {'timestamp':9,'price':.7,'resolution_seconds':60}]}
        result=normalize_history(payload,'m',20,'ref',0,15)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['available_at'],20)

    def test_unsorted_orderbook(self):
        book={'bids':[{'price':'.4','size':'2'},{'price':'.5','size':'3'}],
              'asks':[{'price':'.8','size':'2'},{'price':'.6','size':'3'}]}
        self.assertEqual(quote_from_book(book),(.5,.6,.55))

    def test_one_sided_book(self):
        with self.assertRaises(ValueError):
            quote_from_book({'bids':[],'asks':[{'price':'.6','size':'3'}]})

    def test_rss_is_observed_not_publication_asof(self):
        xml='<rss><channel><item><title>Hello</title><link>https://news.example/a</link><pubDate>Wed, 01 Jan 2025 00:00:00 GMT</pubDate><description>Text</description></item></channel></rss>'
        now=timestamp('2025-01-02T00:00:00Z')
        row=list(rss_records(xml,now,'ref'))[0]
        self.assertEqual(row['available_at'],now)
        self.assertLess(row['published_at'],now)

    @staticmethod
    def feed(link, source=''):
        return ('<rss><channel><item><title>Rubio dismisses reports</title><link>' + link + '</link>'
                '<pubDate>Wed, 01 Jan 2025 00:00:00 GMT</pubDate><description>Text</description>' + source +
                '</item></channel></rss>')

    def sources(self, xml):
        return [r['source'] for r in rss_records(xml, timestamp('2025-01-02T00:00:00Z'), 'ref')]

    def test_google_news_item_is_attributed_to_its_publisher(self):
        xml = self.feed('https://news.google.com/rss/articles/CBMi123',
                        '<source url="https://www.nytimes.com">The New York Times</source>')
        self.assertEqual(self.sources(xml), ['www.nytimes.com'])

    def test_aggregator_item_without_a_usable_source_keeps_the_aggregator_host(self):
        self.assertEqual(self.sources(self.feed('https://news.google.com/rss/articles/CBMi123')), ['news.google.com'])
        for unusable in ('<source>The New York Times</source>', '<source url="javascript:alert(1)">x</source>',
                         '<source url="">x</source>'):
            with self.subTest(source=unusable):
                self.assertEqual(self.sources(self.feed('https://news.google.com/rss/articles/CBMi123', unusable)),
                                 ['news.google.com'])

    def test_ordinary_feed_source_is_unchanged_even_with_a_source_element(self):
        xml = self.feed('https://www.bbc.co.uk/news/a', '<source url="https://other.example">Other</source>')
        self.assertEqual(self.sources(xml), ['www.bbc.co.uk'])

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            timestamp('2025-01-01')

    def test_milliseconds_rejected(self):
        with self.assertRaises(ValueError):
            timestamp(1700000000000)
