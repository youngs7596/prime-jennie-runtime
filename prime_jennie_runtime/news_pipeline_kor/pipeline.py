"""News Pipeline 단일 사이클 오케스트레이션.

v2 원본은 3개 스레드(collector / analyzer / archiver) + Redis Stream 분리. v3는
일단 단일 함수로 한 사이클을 처리 (테스트 용이 + 스케줄러 분리). 운영시엔 cron
또는 별도 서비스가 `run_cycle`을 주기 호출.

흐름:
  crawl → dedup → analyze → upsert sentiment
                       ↘ embed → upsert vector
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .crawler import NewsCrawler
from .dedup import NewsDeduplicator
from .models import NewsArticle, SentimentScore
from .sentiment import Embedder, SentimentAnalyzer
from .storage import SentimentRepo, VectorStore

logger = logging.getLogger(__name__)


@dataclass
class CycleStats:
    """단일 사이클 결과 통계."""

    crawled: int = 0
    deduped: int = 0  # dedup 통과 (= 신규)한 기사 수
    analyzed: int = 0
    embedded: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class NewsPipeline:
    """필수 컴포넌트 묶음 + run_cycle. 의존 객체는 모두 Protocol."""

    crawler: NewsCrawler
    deduplicator: NewsDeduplicator
    analyzer: SentimentAnalyzer
    sentiment_repo: SentimentRepo
    embedder: Embedder | None = None
    vector_store: VectorStore | None = None

    async def run_cycle(self, universe: list[str]) -> CycleStats:
        """크롤링 → 중복 제거 → 감성 분석 → (선택) 임베딩 → 저장."""
        stats = CycleStats()

        try:
            articles = await self.crawler.crawl(universe)
        except Exception as e:
            logger.exception("crawl failed")
            stats.errors.append(f"crawl:{type(e).__name__}:{e}")
            return stats

        stats.crawled = len(articles)
        new_articles = [
            a for a in articles if self.deduplicator.is_new(a.source_url.unicode_string())
        ]
        stats.deduped = len(new_articles)

        if not new_articles:
            return stats

        # 감성 분석 + 임베딩 동시 — 같은 article을 두 추출에 흘림
        sentiment_results = await asyncio.gather(
            *(self._analyze_one(a) for a in new_articles),
            return_exceptions=True,
        )
        for result, article in zip(sentiment_results, new_articles, strict=True):
            if isinstance(result, BaseException):
                stats.errors.append(f"analyze:{article.article_id}:{result}")
                continue
            await self.sentiment_repo.upsert(result, article=article)
            stats.analyzed += 1

        if self.embedder is not None and self.vector_store is not None:
            embed_results = await asyncio.gather(
                *(self._embed_one(a) for a in new_articles),
                return_exceptions=True,
            )
            for result, article in zip(embed_results, new_articles, strict=True):
                if isinstance(result, BaseException):
                    stats.errors.append(f"embed:{article.article_id}:{result}")
                    continue
                stats.embedded += 1

        return stats

    # ----- internal helpers -----

    async def _analyze_one(self, article: NewsArticle) -> SentimentScore:
        return await self.analyzer.analyze(article)

    async def _embed_one(self, article: NewsArticle) -> bool:
        assert self.embedder is not None
        assert self.vector_store is not None
        from datetime import datetime as _dt

        from .models import NewsEmbedding

        vec = await self.embedder.embed(article)
        emb = NewsEmbedding(
            article_id=article.article_id,
            ticker=article.ticker,
            vector=vec,
            embedded_at=_dt.now(),
            model=getattr(self.embedder, "model_name", "stub-embed"),
        )
        await self.vector_store.upsert(emb)
        return True
