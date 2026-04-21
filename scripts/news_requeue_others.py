"""one-off: event_type='other' news_events 의 원본 기사를 stream 에 재 XADD.

news_events row 는 건드리지 않는다 — extractor 가 ON CONFLICT (article_id) UPSERT
이므로 재처리 결과로 덮어쓰여짐. dedup SET 에도 손대지 않는다 (extractor 는
dedup 조회 없이 stream 을 소비한다).

배치 단위로 XADD 하고 sleep 하여 GPU burst 를 평탄화한다.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from typing import Any

import asyncpg
import redis as sync_redis

from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle
from prime_jennie_runtime.news_pipeline_kor.stream import (
    NEWS_STREAM,
    NEWS_STREAM_MAXLEN,
    serialize_article,
)

BATCH = int(os.environ.get("REQUEUE_BATCH", "50"))
SLEEP_SEC = float(os.environ.get("REQUEUE_SLEEP", "5.0"))
LIMIT = int(os.environ.get("REQUEUE_LIMIT", "0"))  # 0 = 전체
STREAM_CAP = int(os.environ.get("REQUEUE_STREAM_CAP", "8000"))  # 추가 XADD 상한


def _pg_url() -> str:
    return (
        f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    )


def _redis_url() -> str:
    pw = os.environ.get("REDIS_PASSWORD", "")
    host = os.environ["REDIS_HOST"]
    port = os.environ["REDIS_PORT"]
    if pw:
        return f"redis://:{pw}@{host}:{port}/0"
    return f"redis://{host}:{port}/0"


def _row_to_article(row: dict[str, Any]) -> NewsArticle:
    return NewsArticle(
        article_id=row["article_id"],
        ticker=row["ticker"],
        title=row["title"],
        body=row["body"] or "",
        published_at=row["published_at"],
        source_url=str(row["source_url"]),
        source_name=row["source_name"] or "",
        fetched_at=row.get("fetched_at") or datetime.now(),
    )


async def main() -> int:
    sql = (
        "SELECT na.article_id, na.ticker, na.title, na.body, na.published_at, "
        "       na.source_url, na.source_name, na.fetched_at "
        "FROM news_events ne "
        "JOIN news_articles na ON na.article_id = ne.article_id "
        "WHERE ne.event_type = 'other' "
        "ORDER BY ne.analyzed_at DESC"
    )
    if LIMIT > 0:
        sql += f" LIMIT {LIMIT}"

    conn = await asyncpg.connect(_pg_url())
    rows = await conn.fetch(sql)
    await conn.close()

    total = len(rows)
    print(f"[requeue] other rows fetched: {total}  batch={BATCH} sleep={SLEEP_SEC}s")
    if total == 0:
        return 0

    r = sync_redis.Redis.from_url(_redis_url(), decode_responses=False)

    pushed = 0
    skipped_cap = 0
    t0 = time.time()
    for i, row in enumerate(rows, start=1):
        # Stream 크기 확인 — CAP 이상이면 잠시 대기
        while True:
            xlen = int(r.xlen(NEWS_STREAM) or 0)
            if xlen < STREAM_CAP:
                break
            skipped_cap += 1
            print(f"[requeue] stream full xlen={xlen} >= {STREAM_CAP}, waiting {SLEEP_SEC * 2}s")
            time.sleep(SLEEP_SEC * 2)

        try:
            article = _row_to_article(dict(row))
        except Exception as exc:
            print(f"[requeue] skip article_id={row['article_id']}: {exc}")
            continue

        r.xadd(NEWS_STREAM, serialize_article(article), maxlen=NEWS_STREAM_MAXLEN, approximate=True)
        pushed += 1

        if i % BATCH == 0:
            elapsed = time.time() - t0
            rate = pushed / elapsed if elapsed else 0
            xlen = int(r.xlen(NEWS_STREAM) or 0)
            print(
                f"[requeue] {i}/{total} pushed={pushed} xlen={xlen} "
                f"rate={rate:.1f}/s elapsed={elapsed:.0f}s"
            )
            time.sleep(SLEEP_SEC)

    elapsed = time.time() - t0
    print(
        f"[requeue] DONE pushed={pushed}/{total} skipped_cap={skipped_cap} elapsed={elapsed:.0f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
