"""Scout 공급 — Track B의 ``NewsScoreFeeder`` Protocol 실 구현.

Track B의 ``StubNewsScoreFeeder``를 대체. SentimentRepo에서 ticker별 최근 N시간
SentimentScore를 읽어 score 평균 + timestamp(최신) + article_count + staleness를 계산.

설계 명세 (SCOUT_CODE_GENERATION §2.4):
- staleness_hours > 48이면 Scout 코드가 제외 권장 (Feeder는 그대로 전달, 판단은 Scout)
- 빈 ticker (기사 0건)는 dict에 0건 entry 포함 — score=0.0, article_count=0, staleness=무한대 표지
  - Scout가 missing보다는 명시적 0건을 더 잘 다룬다는 가정
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time

from prime_jennie_runtime.slow_loop.scout.schemas import NewsScoreEntry

from .storage import SentimentRepo, lookback_since


@dataclass
class NewsPipelineScoutFeeder:
    """SentimentRepo 기반 NewsScoreFeeder. Track B Protocol에 conform.

    `lookback_hours`: ticker별 평균을 계산할 윈도우 (기본 24h).
    `now_supplier`: 현재 시각 주입 (테스트에서 freezegun 대신 명시적 주입 가능).
    """

    sentiment_repo: SentimentRepo
    lookback_hours: float = 24.0

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, NewsScoreEntry]:
        now = self._now_for(as_of)
        since = lookback_since(now, self.lookback_hours)

        out: dict[str, NewsScoreEntry] = {}
        for ticker in universe:
            scores = await self.sentiment_repo.recent_for_ticker(ticker, since=since, now=now)
            if not scores:
                out[ticker] = NewsScoreEntry(
                    score=0.0,
                    timestamp=now,
                    article_count=0,
                    staleness_hours=self.lookback_hours,
                )
                continue
            avg = sum(s.score for s in scores) / len(scores)
            latest = max(s.analyzed_at for s in scores)
            staleness = max(0.0, (now - latest).total_seconds() / 3600.0)
            out[ticker] = NewsScoreEntry(
                score=_clamp(avg, -1.0, 1.0),
                timestamp=latest,
                article_count=len(scores),
                staleness_hours=staleness,
            )
        return out

    def _now_for(self, as_of: date) -> datetime:
        """as_of date의 23:59:59를 'now'로 사용. Scout 호출 시점이 분석 기준일의
        장 마감 후라는 가정 (SCOUT §1.3 — 기본 주기는 다음 날 08:30 KST지만
        as_of는 전일이므로 전일 종가 시각을 안전하게 잡는다).
        """
        return datetime.combine(as_of, time(23, 59, 59))


def _clamp(x: float, lo: float, hi: float) -> float:
    if math.isnan(x):
        return 0.0
    return max(lo, min(hi, x))
