"""뉴스 크롤러 추상화 (Phase 1).

v2의 NaverFinanceCrawler는 BeautifulSoup + httpx로 네이버 금융 뉴스 페이지를 긁는다.
Phase 1 Track E는 Protocol + Stub만. 실제 네트워크 의존 구현은 Phase 2에서
v2 코드를 그대로 포팅 (재작성 금지 — AGENTS.md "v2 포팅 규칙").
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import NewsArticle


@runtime_checkable
class NewsCrawler(Protocol):
    """ticker 리스트를 받아 새 기사들을 yield (또는 list 반환).

    여러 사이트를 동시에 긁을 수 있으므로 universe 일괄 호출. 실제 네트워크/속도
    제약은 구현체 책임.
    """

    async def crawl(self, universe: list[str]) -> list[NewsArticle]: ...


@dataclass
class StubCrawler:
    """고정된 fixture 기사를 반환. 테스트/dev 전용.

    `add` 메서드로 테스트에서 동적으로 기사 풀을 변경 가능.
    """

    seed_articles: list[NewsArticle] = field(default_factory=list)

    async def crawl(self, universe: list[str]) -> list[NewsArticle]:
        wanted = set(universe)
        return [a for a in self.seed_articles if a.ticker in wanted]

    def add(self, articles: Iterable[NewsArticle]) -> None:
        self.seed_articles.extend(articles)


def make_dummy_article(
    ticker: str,
    *,
    title: str,
    body: str = "",
    url: str | None = None,
    published_at: datetime | None = None,
) -> NewsArticle:
    """테스트 fixture용 헬퍼. URL이 비면 ticker+title 기반 결정론적 URL 생성."""
    from .dedup import article_fingerprint

    pub = published_at or datetime.now()
    fingerprint_seed = url or f"{ticker}|{title}"
    fingerprint = article_fingerprint(fingerprint_seed)
    return NewsArticle(
        article_id=fingerprint,
        ticker=ticker,
        title=title,
        body=body,
        published_at=pub,
        source_url=url or f"https://news.example.com/{ticker}/{fingerprint}",
        source_name="stub",
    )
