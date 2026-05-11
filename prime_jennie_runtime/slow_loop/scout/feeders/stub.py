"""Scout feeder stubs — Phase 1 고정값 반환.

Track E가 real feeder를 붙일 때까지 pipeline/테스트용. 어떤 입력도 무해해야 함.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ..schemas import ConsensusEntry, MarketSummary, NewsEventEntry


@dataclass(frozen=True)
class StubUniverseFeeder:
    tickers: tuple[str, ...] = (
        "005930",  # 삼성전자
        "000660",  # SK하이닉스
        "035420",  # NAVER
        "207940",  # 삼성바이오로직스
        "005380",  # 현대차
    )

    async def fetch(self, as_of: date) -> list[str]:
        return list(self.tickers)


@dataclass(frozen=True)
class StubNewsEventFeeder:
    base_timestamp: datetime = field(default_factory=datetime.now)

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, NewsEventEntry]:
        return {
            t: NewsEventEntry(
                article_count=3,
                latest_at=self.base_timestamp,
                staleness_hours=2.0,
                events_by_impact={"medium": {"other": 3}},
                positive_events=[],
                risk_events=[],
            )
            for t in universe
        }


@dataclass(frozen=True)
class StubSectorMomentumFeeder:
    defaults: dict[str, float] = field(
        default_factory=lambda: {
            "반도체": 0.04,
            "2차전지": 0.03,
            "바이오": -0.01,
            "자동차": 0.02,
            "인터넷": 0.01,
        }
    )

    async def fetch(self, as_of: date) -> dict[str, float]:
        return dict(self.defaults)


@dataclass(frozen=True)
class StubMarketSummaryFeeder:
    async def fetch(self, as_of: date) -> MarketSummary:
        return MarketSummary(
            kospi_close=2800.0,
            kospi_change_pct=0.005,
            kosdaq_close=880.0,
            kosdaq_change_pct=0.003,
            up_count=520,
            down_count=310,
        )


@dataclass(frozen=True)
class EmptyConsensusFeeder:
    """Placeholder feeder — 모든 ticker 에 None 값 ConsensusEntry 반환.

    v3 DB 에 forward EPS/PER consensus 적재 파이프라인이 들어오면
    ``RealConsensusFeeder`` 로 교체. 그 전까지 Scout 코드는 ``consensus_data[t]``
    의 모든 필드가 None 임을 인지하고 다른 팩터로만 판단한다.
    """

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, ConsensusEntry]:
        return {t: ConsensusEntry() for t in universe}
