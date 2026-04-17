"""Macro feeder stubs — Phase 1 고정값."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..schemas import IndexPoint, MarketSnapshot


@dataclass(frozen=True)
class StubWsjDigestFeeder:
    digest_id: str = "wsj_stub_0000"
    summary: str = "평온한 글로벌 시장. 특별한 리스크 이벤트 없음."
    headlines: str = "- (stub) 아무 일 없음\n- (stub) 연준 금리 동결 기조"

    async def fetch(self, as_of: datetime) -> tuple[str, str, str]:
        return (self.digest_id, self.summary, self.headlines)


@dataclass(frozen=True)
class StubKorMacroNewsFeeder:
    async def fetch(self, as_of: datetime) -> str:
        return "- (stub) 국내 매크로 이슈 없음"


@dataclass(frozen=True)
class StubMarketSnapshotFeeder:
    """정상 장 스냅샷. 모든 closed 트리거 미충족."""

    async def fetch(self, as_of: datetime) -> MarketSnapshot:
        ip = lambda close, pct: IndexPoint(close=close, change_pct=pct)  # noqa: E731
        return MarketSnapshot(
            as_of=as_of,
            kospi=ip(2800.0, 0.005),
            kosdaq=ip(880.0, 0.003),
            kospi_200_futures=ip(370.0, 0.004),
            sp500=ip(5200.0, 0.002),
            nasdaq=ip(16500.0, 0.003),
            nikkei=ip(39000.0, 0.001),
            hsi=ip(17000.0, -0.002),
            usd_krw=1350.0,
            usd_krw_change_pct=0.002,
            usd_jpy=150.0,
            crude_oil=80.0,
            crude_oil_change_pct=0.005,
            gold=2300.0,
            gold_change_pct=0.001,
            vix=18.0,
            vix_prev=17.5,
            vkospi=19.0,
            kospi_20d_vol=0.15,
            kospi_60d_vol=0.14,
            major_sector_drops=[],
        )
