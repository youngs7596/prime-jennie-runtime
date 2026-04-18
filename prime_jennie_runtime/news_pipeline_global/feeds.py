"""RSS 피드 카탈로그.

env `GLOBAL_NEWS_FEEDS_JSON` 으로 완전 override 가능. 기본값은 공용 RSS 중
무료·인증 불필요·안정적 페이드 3종:

- WSJ Markets/World (공식 RSS, paywall 은 본문에만 걸림. title/description 은 공개)
- Bloomberg Markets (공식 RSS)
- Reuters 는 2020 에 공식 RSS 폐지 → Google News RSS 경유 (`site:reuters.com`)

키워드 필터는 현재 없음 (매크로 카테고리 RSS 만 선택해서 덜 필요). 필요해지면
feed 당 `keywords` 추가.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RssFeed:
    source: str  # 'wsj' | 'bloomberg' | 'reuters' | 'google_news'
    feed_name: str  # 세부 이름 (로그/DB 보기 편하게)
    url: str


DEFAULT_FEEDS: tuple[RssFeed, ...] = (
    # WSJ 공식 RSS (feeds.a.dj.com) 는 2025-01-27 이후 업데이트 중단됨 — retired.
    # 실전 검증 (2026-04-18): Google News site-filter 가 최근 24h 헤드라인을 안정 공급.
    RssFeed(
        "wsj",
        "wsj-via-gnews",
        "https://news.google.com/rss/search?q=site:wsj.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    ),
    RssFeed("bloomberg", "bloomberg-markets", "https://feeds.bloomberg.com/markets/news.rss"),
    RssFeed("bloomberg", "bloomberg-economics", "https://feeds.bloomberg.com/economics/news.rss"),
    # Reuters 공식 RSS 는 폐지 → Google News 검색 RSS 로 대체. 24h 창.
    RssFeed(
        "reuters",
        "reuters-via-gnews",
        "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    ),
)


def load_feeds() -> tuple[RssFeed, ...]:
    """env override 지원. `GLOBAL_NEWS_FEEDS_JSON` 은 list[{source,feed_name,url}]."""
    raw = os.environ.get("GLOBAL_NEWS_FEEDS_JSON")
    if not raw:
        return DEFAULT_FEEDS
    try:
        items = json.loads(raw)
        return tuple(
            RssFeed(source=i["source"], feed_name=i["feed_name"], url=i["url"]) for i in items
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        # env 가 깨졌을 때 조용히 기본값으로 폴백하면 디버깅이 어려워진다.
        # 호출자에서 예외 처리하도록 그대로 올림.
        raise
