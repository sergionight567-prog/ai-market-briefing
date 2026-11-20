import os

from briefing import pipeline

FEED = os.path.join(os.path.dirname(__file__), 'fixture_feed.xml')


def test_rss_items_are_normalised():
    items = pipeline.fetch_rss('Fixture', FEED)
    assert len(items) == 2
    it = items[0]
    assert it['source'] == 'Fixture' and it['url'].startswith('https://')
    assert '<' not in it['excerpt'] and it['published'] > 1_700_000_000


def test_slack_message_shape():
    item = {
        'title': 'Battery prices fall',
        'url': 'https://example.com/a',
        'summary': 'Prices fell 10%.',
        'why': 'Cheaper cells.',
        'category': 'Market & prices',
        'source': 'Fixture',
        'relevance': 8,
    }
    blocks = pipeline.slack_blocks([item])['blocks']
    assert blocks[0]['type'] == 'header' and 'Battery prices fall' in blocks[1]['text']['text']


def test_email_renders():
    item = {
        'title': 'Battery prices fall',
        'url': 'https://example.com/a',
        'summary': 'Prices fell 10%.',
        'why': 'Cheaper cells.',
        'category': 'Market & prices',
        'source': 'Fixture',
        'relevance': 8,
    }
    html = pipeline.render_email([item], 'test-run')
    assert 'Battery prices fall' in html and 'Why it matters' in html
