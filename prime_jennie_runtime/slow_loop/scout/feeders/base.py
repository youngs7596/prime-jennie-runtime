"""Scout Feeder 프로토콜 정의.

Phase 1: stub 구현만 제공. Track E가 real feeder로 교체.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from ..schemas import MarketSummary, NewsEventEntry


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
