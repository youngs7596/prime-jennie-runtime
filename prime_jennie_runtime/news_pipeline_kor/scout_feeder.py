"""Scout 공급 — Track B의 ``NewsScoreFeeder`` Protocol 실 구현.

2026-04-21 이후: ``EventRepo`` 기반. 신규 쓰기 경로가 news_events 이므로 ticker 별
최근 N시간 ``sentiment_score`` 평균으로 Scout 호환 NewsScoreEntry 를 계산.
추후 Scout 프롬프트가 event_type/impact 를 직접 활용하도록 확장할 수 있다.

설계 명세 (SCOUT_CODE_GENERATION §2.4):
- staleness_hours > 48 이면 Scout 코드가 제외 권장 (판단은 Scout)
- 기사 0건 ticker 도 명시적 0건 entry 로 포함
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time

from prime_jennie_runtime.slow_loop.scout.schemas import NewsScoreEntry

from .storage import EventRepo, lookback_since


@dataclass
class NewsPipelineScoutFeeder:
    """EventRepo 기반 NewsScoreFeeder."""

    event_repo: EventRepo
    lookback_hours: float = 24.0

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, NewsScoreEntry]:
        now = self._now_for(as_of)
        since = lookback_since(now, self.lookback_hours)

        out: dict[str, NewsScoreEntry] = {}
        for ticker in universe:
            events = await self.event_repo.recent_for_ticker(ticker, since=since, now=now)
            if not events:
                out[ticker] = NewsScoreEntry(
                    score=0.0,
                    timestamp=now,
                    article_count=0,
                    staleness_hours=self.lookback_hours,
                )
                continue
            avg = sum(e.sentiment_score for e in events) / len(events)
            latest = max(e.analyzed_at for e in events)
            staleness = max(0.0, (now - latest).total_seconds() / 3600.0)
            out[ticker] = NewsScoreEntry(
                score=_clamp(avg, -1.0, 1.0),
                timestamp=latest,
                article_count=len(events),
                staleness_hours=staleness,
            )
        return out

    def _now_for(self, as_of: date) -> datetime:
        return datetime.combine(as_of, time(23, 59, 59))


def _clamp(x: float, lo: float, hi: float) -> float:
    if math.isnan(x):
        return 0.0
    return max(lo, min(hi, x))
