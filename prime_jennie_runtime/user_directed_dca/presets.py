"""DCA 캠페인 프리셋 — 무장 시 echo 로 되읽는 고정 파라미터.

검증 사다리와 1:1 대응한다. dryrun(실주문 없이 로직 검증) → smoke(저가 종목 소액 실거래)
→ production(본자금). production/dryrun 의 파라미터는 명세 고정값이라 오타 여지가 없고,
smoke 만 운영자가 종목을 골라 붙인다(`/dca arm smoke <종목코드>`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class PresetLeg:
    ticker: str
    cap_krw: int


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    legs: tuple[PresetLeg, ...]
    total_slices: int
    dry_run: bool
    per_order_cost_cap: int
    per_day_cost_cap: int
    needs_ticker: bool = False
    smoke_cap_krw: int = 0
    accel_open_pct: float = -4.0
    accel_prev_pct: float = -5.0
    accel_cooldown_h: int = 24
    clamp_pct: float = 0.995
    # 14:00 — 2026-06-20 백테스트 결정(.ai/analyses/2026-06-20-dca-entry-time.md).
    execute_at_kst: str = "14:00"


# 본자금: 삼성전자·SK하이닉스 각 1.18억 (총 2.36억), 10슬라이스.
_PRODUCTION_LEGS = (PresetLeg("005930", 118_000_000), PresetLeg("000660", 118_000_000))

PRESETS: dict[str, Preset] = {
    "production": Preset(
        name="production",
        label="본자금 — 삼성전자·SK하이닉스 50:50",
        legs=_PRODUCTION_LEGS,
        total_slices=10,
        dry_run=False,
        per_order_cost_cap=40_000_000,
        per_day_cost_cap=60_000_000,
    ),
    "dryrun": Preset(
        name="dryrun",
        label="모의 검증 — production 파라미터, 실주문 없음",
        legs=_PRODUCTION_LEGS,
        total_slices=10,
        dry_run=True,
        per_order_cost_cap=40_000_000,
        per_day_cost_cap=60_000_000,
    ),
    "smoke": Preset(
        name="smoke",
        label="소액 실거래 스모크 — 종목 1개",
        legs=(),
        needs_ticker=True,
        smoke_cap_krw=100_000,
        total_slices=2,
        dry_run=False,
        per_order_cost_cap=100_000,
        per_day_cost_cap=200_000,
    ),
}


def resolve_legs(preset: Preset, ticker: str | None) -> list[PresetLeg]:
    """프리셋의 종목 구성. smoke 는 운영자가 준 종목 1개로 구성한다."""
    if preset.needs_ticker:
        if not ticker:
            raise ValueError("smoke 프리셋은 종목코드가 필요합니다 (/dca arm smoke <종목코드>)")
        return [PresetLeg(ticker, preset.smoke_cap_krw)]
    return list(preset.legs)


def campaign_id(ticker: str, arm_date: date) -> str:
    return f"dca:{ticker}:{arm_date:%Y%m%d}"


def build_campaign_rows(
    preset: Preset, *, arm_date: date, ticker: str | None = None
) -> list[dict[str, Any]]:
    """무장 시 INSERT 할 캠페인 행들(종목별 1개)."""
    rows: list[dict[str, Any]] = []
    for leg in resolve_legs(preset, ticker):
        rows.append(
            {
                "id": campaign_id(leg.ticker, arm_date),
                "ticker": leg.ticker,
                "preset": preset.name,
                "cap_krw": leg.cap_krw,
                "total_slices": preset.total_slices,
                "execute_at_kst": preset.execute_at_kst,
                "accel_open_pct": preset.accel_open_pct,
                "accel_prev_pct": preset.accel_prev_pct,
                "accel_cooldown_h": preset.accel_cooldown_h,
                "clamp_pct": preset.clamp_pct,
                "per_order_cost_cap": preset.per_order_cost_cap,
                "per_day_cost_cap": preset.per_day_cost_cap,
                "dry_run": preset.dry_run,
            }
        )
    return rows


__all__ = [
    "PRESETS",
    "Preset",
    "PresetLeg",
    "build_campaign_rows",
    "campaign_id",
    "resolve_legs",
]
