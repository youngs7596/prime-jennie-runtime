"""LLM 호출 통계 Redis 쓰기 헬퍼.

Dashboard `/llm/stats` (dashboard/routers/llm_stats.py) 가 읽는 키 포맷과 동기:

    llm:stats:{YYYY-MM-DD}:{service}

Hash 필드: calls / tokens_in / tokens_out / cost_usd (현재 reader 는 cost_usd 는
미사용이지만 향후 확장을 위해 함께 누적). 날짜는 KST 기준 — 장 시작/종료가
KST 인 만큼 UTC 로 기록하면 경계일 집계가 어긋남.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))


def _kst_date_key(ts: datetime | None) -> str:
    if ts is None:
        now = datetime.now(_KST)
    elif ts.tzinfo is None:
        now = ts.replace(tzinfo=_KST)
    else:
        now = ts.astimezone(_KST)
    return now.strftime("%Y-%m-%d")


async def record_llm_call(
    redis_client: Any,
    *,
    service: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float | None = None,
    timestamp: datetime | None = None,
) -> None:
    """LLM 호출 1건을 Redis hash 에 누적.

    redis_client 가 None 이거나 Redis 쓰기 실패해도 조용히 swallow — LLM 호출
    자체는 이미 성공한 상태에서 통계만 놓치는 경우라 paylod 흐름을 막지 않는다.
    """
    if redis_client is None:
        return
    date_key = _kst_date_key(timestamp)
    key = f"llm:stats:{date_key}:{service}"
    try:
        pipe = redis_client.pipeline(transaction=False)
        pipe.hincrby(key, "calls", 1)
        pipe.hincrby(key, "tokens_in", int(input_tokens))
        pipe.hincrby(key, "tokens_out", int(output_tokens))
        if cost_usd is not None:
            pipe.hincrbyfloat(key, "cost_usd", float(cost_usd))
        await pipe.execute()
    except Exception:
        logger.exception("record_llm_call failed: service=%s key=%s", service, key)
