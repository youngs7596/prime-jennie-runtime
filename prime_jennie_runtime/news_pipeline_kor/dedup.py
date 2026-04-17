"""뉴스 중복 제거 — v2 dedup.py 포팅.

v2 원본: prime_jennie/services/news/dedup.py — Redis SET 기반 3일 TTL.
v3 변경: Protocol 추상화 + InMemory(테스트) / Redis(운영) 두 backend.

알고리즘:
- URL 또는 (제목+본문) hash → 12-char md5
- 날짜별 SET (`dedup:news:YYYYMMDD`)에 hash 저장
- 중복 체크는 최근 3일 키 모두 확인 → 날짜 경계 회피
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

DEDUP_TTL_SECONDS = 3 * 86400  # v2와 동일
DEDUP_WINDOW_DAYS = 3


def article_fingerprint(url_or_text: str) -> str:
    """v2와 동일한 fingerprint 함수: trim + lowercase + md5[:12]."""
    return hashlib.md5(url_or_text.strip().lower().encode()).hexdigest()[:12]


def _key_for(d: date) -> str:
    return f"dedup:news:{d.strftime('%Y%m%d')}"


def _recent_keys(today: date, window_days: int = DEDUP_WINDOW_DAYS) -> list[str]:
    return [_key_for(today - timedelta(days=d)) for d in range(window_days)]


@runtime_checkable
class NewsDeduplicator(Protocol):
    """중복 체크 + 마킹의 단일 책임."""

    def is_new(self, url_or_text: str, *, now: datetime | None = None) -> bool:
        """is_duplicate=False && mark_seen → True 반환."""
        ...


# ---------------------------------------------------------------------
# In-memory (테스트 / dev)
# ---------------------------------------------------------------------


@dataclass
class InMemoryDeduplicator:
    """v2와 동일 로직을 dict[date_key, set[hash]]로 구현."""

    window_days: int = DEDUP_WINDOW_DAYS
    _store: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def is_new(self, url_or_text: str, *, now: datetime | None = None) -> bool:
        if self._is_duplicate(url_or_text, now=now):
            return False
        self._mark_seen(url_or_text, now=now)
        return True

    def _is_duplicate(self, url_or_text: str, *, now: datetime | None = None) -> bool:
        h = article_fingerprint(url_or_text)
        today = (now or datetime.now()).date()
        return any(h in self._store.get(k, set()) for k in _recent_keys(today, self.window_days))

    def _mark_seen(self, url_or_text: str, *, now: datetime | None = None) -> None:
        h = article_fingerprint(url_or_text)
        today = (now or datetime.now()).date()
        self._store[_key_for(today)].add(h)

    # 테스트 보조
    def size(self) -> int:
        return sum(len(s) for s in self._store.values())


# ---------------------------------------------------------------------
# Redis backend — v2 호환
# ---------------------------------------------------------------------


@dataclass
class RedisDeduplicator:
    """v2 동일 키 스킴. ``redis.Redis`` 또는 ``redis.asyncio.Redis``를 받지만 본 클래스는
    sync Redis만 지원 (Phase 1 news pipeline은 sync thread 모델을 그대로 답습)."""

    redis_client: object  # `redis.Redis`. 외부 import 회피 위해 object 타입.
    window_days: int = DEDUP_WINDOW_DAYS

    def is_new(self, url_or_text: str, *, now: datetime | None = None) -> bool:
        h = article_fingerprint(url_or_text)
        today = (now or datetime.now()).date()
        keys = _recent_keys(today, self.window_days)

        pipe = self.redis_client.pipeline()  # type: ignore[attr-defined]
        for key in keys:
            pipe.sismember(key, h)
        results = pipe.execute()
        if any(results):
            return False

        today_key = _key_for(today)
        write_pipe = self.redis_client.pipeline()  # type: ignore[attr-defined]
        write_pipe.sadd(today_key, h)
        write_pipe.expire(today_key, DEDUP_TTL_SECONDS)
        write_pipe.execute()
        return True
