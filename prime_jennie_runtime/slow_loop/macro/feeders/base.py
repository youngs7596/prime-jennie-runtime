"""Macro Feeder 프로토콜 정의."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from ..schemas import MarketSnapshot


@runtime_checkable
class WsjDigestFeeder(Protocol):
    """WSJ 뉴스레터 → news_digests 테이블 조회."""

    async def fetch(self, as_of: datetime) -> tuple[str, str, str]:
        """Returns (digest_id, macro_summary, headlines_formatted).

        `digest_id == "unavailable"` 가능 (MACRO §8.1 24h 초과 시).
        """
        ...


@runtime_checkable
class MarketSnapshotFeeder(Protocol):
    """KOSPI/KOSDAQ/해외지수/환율/변동성/섹터 스냅샷."""

    async def fetch(self, as_of: datetime) -> MarketSnapshot: ...


@runtime_checkable
class KorMacroNewsFeeder(Protocol):
    """국내 매크로 관련 뉴스 포맷팅 문자열."""

    async def fetch(self, as_of: datetime) -> str: ...
