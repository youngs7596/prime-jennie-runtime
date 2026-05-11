"""Scout Feeder 프로토콜 정의.

Phase 1: stub 구현만 제공. Track E가 real feeder로 교체.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from ..schemas import ConsensusEntry, MarketSummary, NewsEventEntry


@runtime_checkable
class UniverseFeeder(Protocol):
    """KOSPI 200 + KOSDAQ 150 universe를 제공."""

    async def fetch(self, as_of: date) -> list[str]: ...


@runtime_checkable
class NewsEventFeeder(Protocol):
    """ticker별 뉴스 이벤트 분포 스냅샷. 2026-04-25 재설계: Qwen3 메타데이터 기반."""

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, NewsEventEntry]: ...


@runtime_checkable
class SectorMomentumFeeder(Protocol):
    """섹터별 20일 모멘텀."""

    async def fetch(self, as_of: date) -> dict[str, float]: ...


@runtime_checkable
class MarketSummaryFeeder(Protocol):
    """KOSPI/KOSDAQ 지수 요약."""

    async def fetch(self, as_of: date) -> MarketSummary: ...


@runtime_checkable
class ConsensusFeeder(Protocol):
    """ticker별 Forward 컨센서스 (forward_per, eps_revision_pct, analyst_count 등).

    v2 ``ConsensusInfo`` 와 동일 의미. v3 DB 에 적재 파이프라인이 아직 없어
    초기 구현은 ``EmptyConsensusFeeder`` 가 모든 ticker 에 None 만 채운다 —
    Scout 코드 입장에선 "데이터 미존재" 시그널.
    """

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, ConsensusEntry]: ...
