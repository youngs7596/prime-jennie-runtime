"""rss_crawler — feedparser 파싱, dedupe, 단일 피드 실패 격리."""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.news_pipeline_global.feeds import RssFeed
from prime_jennie_runtime.news_pipeline_global.rss_crawler import (
    _article_id,
    crawl_all,
    fetch_one,
)

_WSJ_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>WSJ Markets</title>
<item>
  <title>Fed signals pause on rate hikes</title>
  <link>https://www.wsj.com/articles/fed-signals-pause-1</link>
  <description>Federal Reserve officials suggested a pause is likely next meeting.</description>
  <pubDate>Sat, 18 Apr 2026 08:00:00 GMT</pubDate>
</item>
<item>
  <title>Oil prices drop on OPEC+ signals</title>
  <link>https://www.wsj.com/articles/oil-opec-2</link>
  <description>Crude futures fell after hints of supply increase.</description>
  <pubDate>Sat, 18 Apr 2026 07:30:00 GMT</pubDate>
</item>
</channel></rss>"""

_BLOOMBERG_RSS = b"""<?xml version="1.0"?>
<rss><channel>
<item>
  <title>Yen weakens past 160 per dollar</title>
  <link>https://www.bloomberg.com/news/yen-weakens-1</link>
  <description>Japanese currency touched fresh lows in Tokyo trading.</description>
  <pubDate>Sat, 18 Apr 2026 09:00:00 GMT</pubDate>
</item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_fetch_one_parses_rss_entries():
    feed = RssFeed("wsj", "wsj-markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml")
    with respx.mock(assert_all_called=True) as mock:
        mock.get(feed.url).mock(return_value=httpx.Response(200, content=_WSJ_RSS))
        async with httpx.AsyncClient() as client:
            arts = await fetch_one(client, feed)
    assert len(arts) == 2
    assert arts[0].source == "wsj"
    assert arts[0].feed_name == "wsj-markets"
    assert "Fed" in arts[0].title
    assert arts[0].source_url.startswith("https://www.wsj.com")


@pytest.mark.asyncio
async def test_fetch_one_http_error_returns_empty():
    feed = RssFeed("wsj", "wsj-markets", "https://feeds.a.dj.com/rss/x.xml")
    with respx.mock() as mock:
        mock.get(feed.url).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            arts = await fetch_one(client, feed)
    assert arts == []


@pytest.mark.asyncio
async def test_crawl_all_dedupes_and_isolates_failures():
    good = RssFeed("wsj", "wsj-markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml")
    bad = RssFeed("bloomberg", "bloomberg-markets", "https://feeds.bloomberg.com/markets/news.rss")
    dup = RssFeed("wsj", "wsj-world", "https://feeds.a.dj.com/rss/RSSWorldNews.xml")
    with respx.mock() as mock:
        mock.get(good.url).mock(return_value=httpx.Response(200, content=_WSJ_RSS))
        mock.get(bad.url).mock(return_value=httpx.Response(500))
        mock.get(dup.url).mock(return_value=httpx.Response(200, content=_WSJ_RSS))  # same URLs
        async with httpx.AsyncClient() as client:
            arts = await crawl_all((good, bad, dup), client=client)
    # WSJ 2건, dup 도 같은 URL → dedupe 로 총 2건. bloomberg 실패는 0건 기여.
    assert len(arts) == 2
    ids = {a.article_id for a in arts}
    assert ids == {
        _article_id("https://www.wsj.com/articles/fed-signals-pause-1"),
        _article_id("https://www.wsj.com/articles/oil-opec-2"),
    }


@pytest.mark.asyncio
async def test_crawl_all_merges_sources():
    wsj = RssFeed("wsj", "wsj-markets", "https://wsj.example/rss")
    bbg = RssFeed("bloomberg", "bloomberg-markets", "https://bloomberg.example/rss")
    with respx.mock() as mock:
        mock.get(wsj.url).mock(return_value=httpx.Response(200, content=_WSJ_RSS))
        mock.get(bbg.url).mock(return_value=httpx.Response(200, content=_BLOOMBERG_RSS))
        async with httpx.AsyncClient() as client:
            arts = await crawl_all((wsj, bbg), client=client)
    sources = {a.source for a in arts}
    assert sources == {"wsj", "bloomberg"}
    assert len(arts) == 3
