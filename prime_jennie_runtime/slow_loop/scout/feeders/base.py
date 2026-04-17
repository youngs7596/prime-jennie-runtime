"""Scout Feeder 프로토콜 정의.

Phase 1: stub 구현만 제공. Track E가 real feeder로 교체.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from ..schemas import MarketSummary, NewsScoreEntry


@runtime_checkable
class UniverseFeeder(Protocol):
    """KOSPI 200 + KOSDAQ 150 universe를 제공."""

    async def fetch(self, as_of: date) -> list[str]: ...


@runtime_checkable
class NewsScoreFeeder(Protocol):
    """ticker별 감성 점수 스냅샷. Track E의 news_pipeline_kor가 실체."""

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, NewsScoreEntry]: ...


@runtime_checkable
class SectorMomentumFeeder(Protocol):
    """섹터별 20일 모멘텀."""

    async def fetch(self, as_of: date) -> dict[str, float]: ...


@runtime_checkable
class MarketSummaryFeeder(Protocol):
    """KOSPI/KOSDAQ 지수 요약."""

    async def fetch(self, as_of: date) -> MarketSummary: ...
