"""Scout 공급 — Track B의 ``NewsEventFeeder`` Protocol 실 구현.

2026-04-25 재설계: 과거 ``sentiment_score`` 평균 기반 ``NewsScoreEntry`` 에서
Qwen3 메타데이터 기반 ``NewsEventEntry`` 로 교체. ticker 별로 impact × event_type
분포를 그대로 노출하고, ``positive_events`` / ``risk_events`` 편의 리스트를 함께
제공해서 Scout 코드가 규칙 조합으로 판단할 수 있도록 함.

설계 명세 (SCOUT_CODE_GENERATION §2.4):
- staleness_hours > 48 이면 Scout 코드가 뉴스 시그널 제외 권장
- 기사 0건 ticker 도 명시적 0건 entry 로 포함
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from prime_jennie_runtime.slow_loop.scout.schemas import (
    POSITIVE_EVENT_TYPES,
    RISK_EVENT_TYPES,
    NewsEventEntry,
)

from .storage import EventRepo, lookback_since


@dataclass
class NewsPipelineScoutFeeder:
    """EventRepo 기반 NewsEventFeeder."""

    event_repo: EventRepo
    lookback_hours: float = 24.0

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, NewsEventEntry]:
        now = self._now_for(as_of)
        since = lookback_since(now, self.lookback_hours)

        out: dict[str, NewsEventEntry] = {}
        for ticker in universe:
            events = await self.event_repo.recent_for_ticker(ticker, since=since, now=now)
            if not events:
                out[ticker] = NewsEventEntry(
                    article_count=0,
                    latest_at=None,
                    staleness_hours=self.lookback_hours,
                )
                continue
            latest = max(e.analyzed_at for e in events)
            staleness = max(0.0, (now - latest).total_seconds() / 3600.0)
            events_by_impact: dict[str, dict[str, int]] = {}
            for e in events:
                bucket = events_by_impact.setdefault(e.impact_level, {})
                bucket[e.event_type] = bucket.get(e.event_type, 0) + 1
            high_types = set(events_by_impact.get("high", {}).keys())
            out[ticker] = NewsEventEntry(
                article_count=len(events),
                latest_at=latest,
                staleness_hours=staleness,
                events_by_impact=events_by_impact,
                positive_events=sorted(high_types & POSITIVE_EVENT_TYPES),
                risk_events=sorted(high_types & RISK_EVENT_TYPES),
            )
        return out

    def _now_for(self, as_of: date) -> datetime:
        return datetime.combine(as_of, time(23, 59, 59))
