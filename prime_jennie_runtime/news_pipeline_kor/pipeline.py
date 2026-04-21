"""News Pipeline — v2 collector/analyzer 3-thread 모델 + v3 metadata 추출.

2026-04-21 설계 전환:
- 기존 ``SentimentAnalyzer`` + ``Embedder`` 2 stage 소비 → ``EventExtractor`` 단일 stage
- Qdrant/kure-v1 제거 — 벡터 DB 는 국내 금융 뉴스 semantic duplicate 특성상 효용 낮음
- EXAONE 이 structured JSON 으로 event_type/impact/keywords 등 메타데이터 추출 →
  ``news_events`` RDB 테이블 upsert

흐름:
    collect_and_publish(universe)  : crawl → dedup → XADD v3:news:raw
    extract_stream_once()          : XREADGROUP → EXAONE extract → PG news_events → XACK

각 메서드는 호출 1 회의 stats 를 반환. app.py 의 2 async loop 가 각자 호출.

``redis_client`` 는 sync ``redis.Redis``. blocking I/O 가 event loop 을 멈추지 않도록
모든 호출은 ``asyncio.to_thread`` 로 감싼다. LLM httpx 호출은 main event loop 에서
그대로 await — 별도 loop 생성 금지.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .crawler import NewsCrawler
from .dedup import NewsDeduplicator
from .models import NewsArticle, NewsEvent
from .sentiment import EventExtractor
from .storage import EventRepo
from .stream import (
    BLOCK_MS,
    EXTRACTOR_BATCH,
    EXTRACTOR_CONSUMER,
    EXTRACTOR_GROUP,
    NEWS_STREAM,
    NEWS_STREAM_MAXLEN,
    deserialize_article,
    serialize_article,
)

logger = logging.getLogger(__name__)


@dataclass
class CycleStats:
    """단일 호출 결과 통계."""

    crawled: int = 0
    deduped: int = 0  # stream 에 발행된 신규 기사 수
    extracted: int = 0  # news_events 로 upsert 성공 건수
    errors: list[str] = field(default_factory=list)


async def _ensure_group_async(redis_client: Any, stream: str, group: str) -> None:
    """Consumer group 보장. 이미 있으면 BUSYGROUP 무시."""

    def _call() -> None:
        try:
            redis_client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    await asyncio.to_thread(_call)


@dataclass
class NewsPipeline:
    """collect + extract 2 stage. app.py 가 2 async loop 로 호출."""

    crawler: NewsCrawler
    deduplicator: NewsDeduplicator
    extractor: EventExtractor
    event_repo: EventRepo
    _groups_ensured: bool = False

    async def ensure_streams(self, redis_client: Any) -> None:
        """Extractor consumer group 1 회 보장 — app 시작 시."""
        await _ensure_group_async(redis_client, NEWS_STREAM, EXTRACTOR_GROUP)
        self._groups_ensured = True

    # ------------------------------------------------------------------
    # Stage 1 — Collector
    # ------------------------------------------------------------------

    async def collect_and_publish(self, universe: list[str], redis_client: Any) -> CycleStats:
        """crawl → dedup → stream 에 XADD. ``redis_client`` 는 sync ``redis.Redis``.

        ticker 단위 즉시 발행. stream 이 얇게 유지돼 extractor 가 cycle 시작 몇 초
        내에 소비 시작 → GPU 부하가 cycle 전체에 평탄 분산.
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
    # Stage 2 — Extractor
    # ------------------------------------------------------------------

    async def extract_stream_once(self, redis_client: Any) -> CycleStats:
        """Stream → EXAONE 메타데이터 추출 → PG news_events upsert → XACK.

        최대 ``EXTRACTOR_BATCH`` 건/호출.
        """
        stats = CycleStats()
        if not self._groups_ensured:
            await self.ensure_streams(redis_client)

        pending = await _read_group(
            redis_client,
            EXTRACTOR_GROUP,
            EXTRACTOR_CONSUMER,
            last_id="0",
            count=EXTRACTOR_BATCH,
        )
        entries = pending or await _read_group(
            redis_client,
            EXTRACTOR_GROUP,
            EXTRACTOR_CONSUMER,
            last_id=">",
            count=EXTRACTOR_BATCH,
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
                logger.warning("extractor parse failed id=%s: %s", msg_id, e)
                stats.errors.append(f"parse:{msg_id!r}:{e}")
                parse_failed_ids.append(msg_id)
                continue
            parsed.append((msg_id, article))

        if parse_failed_ids:
            await _xack(redis_client, EXTRACTOR_GROUP, parse_failed_ids)

        if not parsed:
            return stats

        results: list[NewsEvent | BaseException] = await asyncio.gather(
            *(self._extract_one(a) for _, a in parsed),
            return_exceptions=True,
        )

        ack_ids: list[Any] = []
        for (msg_id, article), result in zip(parsed, results, strict=True):
            if isinstance(result, BaseException):
                stats.errors.append(f"extract:{article.article_id}:{result}")
            else:
                try:
                    await self.event_repo.upsert(result, article=article)
                    stats.extracted += 1
                except Exception as e:
                    stats.errors.append(f"upsert:{article.article_id}:{type(e).__name__}:{e}")
            # v2 동일 at-most-once — 저장 실패해도 ACK.
            ack_ids.append(msg_id)

        if ack_ids:
            await _xack(redis_client, EXTRACTOR_GROUP, ack_ids)

        return stats

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    async def _extract_one(self, article: NewsArticle) -> NewsEvent:
        return await self.extractor.extract(article)


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

    sync redis blocking xreadgroup 을 ``asyncio.to_thread`` 로 위임.
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
    """가변 인자 XACK batch."""
    if not msg_ids:
        return
    await asyncio.to_thread(redis_client.xack, NEWS_STREAM, group, *msg_ids)
