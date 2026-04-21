"""News Pipeline — v2 3-stage (collect/analyze/archive) 흐름을 Redis Stream 으로 분리.

v2 원본: ``prime_jennie/services/news/{collector,analyzer,archiver}.py``. v2 는 3 개
스레드가 각자 상시 구동하며 ``stream:news:raw`` 로 consumer group 기반 소비.
v3 초기 포팅 때는 단일 ``run_cycle`` 로 합쳐서 10 분 cron 이 전체 단계를 한번에
돌렸는데, 그로 인해 분석 단계의 LLM 호출이 burst 로 몰려 GPU 팬 굉음의 원인이 됨
(세션 2026-04-20). 그래서 v2 패턴으로 되돌린다.

흐름:
    collect_and_publish(universe)  : crawl → dedup → XADD v3:news:raw
    analyze_stream_once()          : XREADGROUP → 배치 LLM → PG upsert → XACK
    archive_stream_once()          : XREADGROUP → 배치 embed → Qdrant upsert → XACK

각 메서드는 호출 1 회의 stats 를 반환. app.py 의 3 async loop 가 각자 호출.

``redis_client`` 는 sync ``redis.Redis``. blocking I/O 가 event loop 을 멈추지 않도록
모든 호출은 ``asyncio.to_thread`` 로 감싼다. LLM/임베딩 httpx 호출은 main event loop
에서 그대로 await — 별도 loop 생성 금지 (cross-loop httpx 는 "Event loop is closed"
에러 유발).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .crawler import NewsCrawler
from .dedup import NewsDeduplicator
from .models import NewsArticle, SentimentScore
from .sentiment import Embedder, SentimentAnalyzer
from .storage import SentimentRepo, VectorStore
from .stream import (
    ANALYZER_BATCH,
    ANALYZER_CONSUMER,
    ANALYZER_GROUP,
    ARCHIVER_BATCH,
    ARCHIVER_CONSUMER,
    ARCHIVER_GROUP,
    BLOCK_MS,
    NEWS_STREAM,
    NEWS_STREAM_MAXLEN,
    deserialize_article,
    serialize_article,
)

logger = logging.getLogger(__name__)


@dataclass
class CycleStats:
    """단일 호출 결과 통계. v2 상시 구동 모델에선 call 단위 경량 집계."""

    crawled: int = 0
    deduped: int = 0  # stream 에 발행된 신규 기사 수
    analyzed: int = 0
    embedded: int = 0
    errors: list[str] = field(default_factory=list)


async def _ensure_group_async(redis_client: Any, stream: str, group: str) -> None:
    """Consumer group 보장. 이미 있으면 BUSYGROUP 으로 무시."""

    def _call() -> None:
        try:
            redis_client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:  # redis.ResponseError 는 import 회피 위해 catch-all
            if "BUSYGROUP" not in str(exc):
                raise

    await asyncio.to_thread(_call)


@dataclass
class NewsPipeline:
    """필수 컴포넌트 묶음. app.py 가 3 async loop 로 이 객체를 호출."""

    crawler: NewsCrawler
    deduplicator: NewsDeduplicator
    analyzer: SentimentAnalyzer
    sentiment_repo: SentimentRepo
    embedder: Embedder | None = None
    vector_store: VectorStore | None = None
    _groups_ensured: bool = False

    async def ensure_streams(self, redis_client: Any) -> None:
        """Consumer group 생성 — app.py 시작 시 1 회."""
        await _ensure_group_async(redis_client, NEWS_STREAM, ANALYZER_GROUP)
        if self.embedder is not None and self.vector_store is not None:
            await _ensure_group_async(redis_client, NEWS_STREAM, ARCHIVER_GROUP)
        self._groups_ensured = True

    # ------------------------------------------------------------------
    # Stage 1 — Collector
    # ------------------------------------------------------------------

    async def collect_and_publish(self, universe: list[str], redis_client: Any) -> CycleStats:
        """crawl → dedup → stream 에 XADD. ``redis_client`` 는 sync ``redis.Redis``.

        ticker 단위로 즉시 발행 — universe 전체를 모은 뒤 flood 하지 않고 크롤 진행에
        맞춰 얇게 stream 을 채운다. 덕분에 analyzer 가 cycle 시작 몇 초 내에 소비를
        시작하고 GPU 부하가 23 분 cycle 전체에 평탄하게 분산된다.
        """
        stats = CycleStats()
        for ticker in universe:
            try:
                articles = await self.crawler.crawl([ticker])
            except Exception as e:
                logger.warning("crawl ticker=%s failed: %s", ticker, e)
                stats.errors.append(f"crawl:{ticker}:{type(e).__name__}:{e}")
                continue

            stats.crawled += len(articles)
            if not articles:
                continue

            # dedup + XADD 는 sync redis 호출 → event loop 차단 방지 위해 to_thread.
            def _dedup_and_publish(
                items: list[NewsArticle] = articles,
            ) -> int:
                new_items = [
                    a for a in items if self.deduplicator.is_new(a.source_url.unicode_string())
                ]
                if not new_items:
                    return 0
                pipe = redis_client.pipeline()
                for article in new_items:
                    pipe.xadd(NEWS_STREAM, serialize_article(article), maxlen=NEWS_STREAM_MAXLEN)
                pipe.execute()
                return len(new_items)

            published = await asyncio.to_thread(_dedup_and_publish)
            stats.deduped += published

        return stats

    # ------------------------------------------------------------------
    # Stage 2 — Analyzer
    # ------------------------------------------------------------------

    async def analyze_stream_once(self, redis_client: Any) -> CycleStats:
        """Stream → 배치 LLM 감성 분석 → PG upsert → XACK. 최대 ``ANALYZER_BATCH`` 건/호출."""
        stats = CycleStats()
        if not self._groups_ensured:
            await self.ensure_streams(redis_client)

        # Pending 우선 처리: 재시작 후 미ACK 메시지 복구.
        pending = await _read_group(
            redis_client,
            ANALYZER_GROUP,
            ANALYZER_CONSUMER,
            last_id="0",
            count=ANALYZER_BATCH,
        )
        entries = pending or await _read_group(
            redis_client,
            ANALYZER_GROUP,
            ANALYZER_CONSUMER,
            last_id=">",
            count=ANALYZER_BATCH,
            block_ms=BLOCK_MS,
        )
        if not entries:
            return stats

        parsed: list[tuple[bytes, NewsArticle]] = []
        parse_failed_ids: list[Any] = []
        for msg_id, data in entries:
            try:
                article = deserialize_article(data)
            except Exception as e:
                logger.warning("analyzer parse failed id=%s: %s", msg_id, e)
                stats.errors.append(f"parse:{msg_id!r}:{e}")
                parse_failed_ids.append(msg_id)
                continue
            parsed.append((msg_id, article))

        if parse_failed_ids:
            await _xack(redis_client, ANALYZER_GROUP, parse_failed_ids)

        if not parsed:
            return stats

        results: list[SentimentScore | BaseException] = await asyncio.gather(
            *(self._analyze_one(a) for _, a in parsed),
            return_exceptions=True,
        )

        ack_ids: list[Any] = []
        for (msg_id, article), result in zip(parsed, results, strict=True):
            if isinstance(result, BaseException):
                stats.errors.append(f"analyze:{article.article_id}:{result}")
            else:
                try:
                    await self.sentiment_repo.upsert(result, article=article)
                    stats.analyzed += 1
                except Exception as e:
                    stats.errors.append(f"upsert:{article.article_id}:{type(e).__name__}:{e}")
            # v2 와 동일 at-most-once — 저장 실패해도 ACK.
            ack_ids.append(msg_id)

        if ack_ids:
            await _xack(redis_client, ANALYZER_GROUP, ack_ids)

        return stats

    # ------------------------------------------------------------------
    # Stage 3 — Archiver
    # ------------------------------------------------------------------

    async def archive_stream_once(self, redis_client: Any) -> CycleStats:
        """Stream → 배치 embed → Qdrant upsert → XACK. 1 회 호출 = 최대 ``ARCHIVER_BATCH`` 건."""
        stats = CycleStats()
        if self.embedder is None or self.vector_store is None:
            return stats
        if not self._groups_ensured:
            await self.ensure_streams(redis_client)

        pending = await _read_group(
            redis_client,
            ARCHIVER_GROUP,
            ARCHIVER_CONSUMER,
            last_id="0",
            count=ARCHIVER_BATCH,
        )
        entries = pending or await _read_group(
            redis_client,
            ARCHIVER_GROUP,
            ARCHIVER_CONSUMER,
            last_id=">",
            count=ARCHIVER_BATCH,
            block_ms=BLOCK_MS,
        )
        if not entries:
            return stats

        parsed: list[tuple[bytes, NewsArticle]] = []
        parse_failed_ids: list[Any] = []
        for msg_id, data in entries:
            try:
                article = deserialize_article(data)
            except Exception as e:
                logger.warning("archiver parse failed id=%s: %s", msg_id, e)
                stats.errors.append(f"parse:{msg_id!r}:{e}")
                parse_failed_ids.append(msg_id)
                continue
            parsed.append((msg_id, article))

        if parse_failed_ids:
            await _xack(redis_client, ARCHIVER_GROUP, parse_failed_ids)

        if not parsed:
            return stats

        embed_results = await asyncio.gather(
            *(self._embed_one(a) for _, a in parsed),
            return_exceptions=True,
        )
        ack_ids: list[Any] = []
        for (msg_id, article), result in zip(parsed, embed_results, strict=True):
            if isinstance(result, BaseException):
                stats.errors.append(f"embed:{article.article_id}:{result}")
            else:
                stats.embedded += 1
            ack_ids.append(msg_id)

        if ack_ids:
            await _xack(redis_client, ARCHIVER_GROUP, ack_ids)

        return stats

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

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


async def _read_group(
    redis_client: Any,
    group: str,
    consumer: str,
    *,
    last_id: str,
    count: int,
    block_ms: int | None = None,
) -> list[tuple[Any, dict]]:
    """XREADGROUP 얇은 래퍼. ``[(msg_id, data_dict), ...]`` 로 flatten.

    sync redis 의 blocking xreadgroup 을 ``asyncio.to_thread`` 로 위임 — BLOCK ms
    동안 event loop 이 멈추지 않음.
    """
    kwargs: dict[str, Any] = {"count": count}
    if block_ms is not None:
        kwargs["block"] = block_ms

    def _call() -> Any:
        return redis_client.xreadgroup(group, consumer, {NEWS_STREAM: last_id}, **kwargs)

    messages = await asyncio.to_thread(_call)
    if not messages:
        return []
    out: list[tuple[Any, dict]] = []
    for _stream_name, msg_entries in messages:
        out.extend(msg_entries)
    return out


async def _xack(redis_client: Any, group: str, msg_ids: list[Any]) -> None:
    """가변 인자 XACK batch. to_thread 로 blocking 회피."""
    if not msg_ids:
        return
    await asyncio.to_thread(redis_client.xack, NEWS_STREAM, group, *msg_ids)
