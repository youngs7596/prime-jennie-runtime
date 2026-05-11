"""check_closed_conditions 결정론 테스트 (MG02)."""

from __future__ import annotations

from datetime import UTC, datetime

from prime_jennie_runtime.slow_loop.macro.closed_conditions import check_closed_conditions
from prime_jennie_runtime.slow_loop.macro.schemas import (
    IndexPoint,
    MarketSnapshot,
    SectorDrop,
)


def _base_snapshot(**overrides: object) -> MarketSnapshot:
    """기본값은 모든 트리거 미충족."""
    ip = IndexPoint(close=2800.0, change_pct=0.005)
    data: dict[str, object] = {
        "as_of": datetime.now(UTC),
        "kospi": ip,
        "kosdaq": ip,
        "sp500": ip,
        "nasdaq": ip,
        "nikkei": ip,
        "hsi": ip,
        "usd_krw": 1350.0,
        "usd_krw_change_pct": 0.002,
        "usd_jpy": 150.0,
        "crude_oil": 80.0,
        "crude_oil_change_pct": 0.0,
        "gold": 2300.0,
        "gold_change_pct": 0.0,
        "vix": 18.0,
        "kospi_20d_vol": 0.15,
        "kospi_60d_vol": 0.14,
        "major_sector_drops": [],
    }
    data.update(overrides)
    return MarketSnapshot(**data)  # type: ignore[arg-type]


def test_no_triggers_returns_empty():
    triggers = check_closed_conditions(_base_snapshot())
    assert triggers == []


def test_mg02_fx_shock_positive():
    """MG02: KRW/USD +3.2% → fx_shock 트리거."""
    snap = _base_snapshot(usd_krw_change_pct=0.032)
    triggers = check_closed_conditions(snap)
    assert "fx_shock" in triggers


def test_mg02_fx_shock_negative():
    """MG02: KRW/USD -3.1% → fx_shock 트리거."""
    snap = _base_snapshot(usd_krw_change_pct=-0.031)
    triggers = check_closed_conditions(snap)
    assert "fx_shock" in triggers


def test_fx_boundary_exactly_3pct():
    """정확히 ±3%는 트리거 (>=)."""
    snap = _base_snapshot(usd_krw_change_pct=0.03)
    assert "fx_shock" in check_closed_conditions(snap)


def test_fx_below_threshold():
    """2.99%는 트리거 아님."""
    snap = _base_snapshot(usd_krw_change_pct=0.0299)
    assert "fx_shock" not in check_closed_conditions(snap)


def test_high_volatility_trigger():
    """KOSPI 20d vol 35% 이상 → high_volatility."""
    snap = _base_snapshot(kospi_20d_vol=0.40)
    triggers = check_closed_conditions(snap)
    assert "high_volatility" in triggers


def test_high_volatility_boundary():
    """정확히 35%는 트리거."""
    snap = _base_snapshot(kospi_20d_vol=0.35)
    assert "high_volatility" in check_closed_conditions(snap)


def test_sector_contagion_three_sectors():
    """주요 섹터 3개 이상 -5% 이상 → sector_contagion."""
    drops = [
        SectorDrop(sector="반도체", change_pct=-0.06),
        SectorDrop(sector="2차전지", change_pct=-0.055),
        SectorDrop(sector="바이오", change_pct=-0.08),
    ]
    snap = _base_snapshot(major_sector_drops=drops)
    triggers = check_closed_conditions(snap)
    assert "sector_contagion" in triggers


def test_sector_contagion_only_two_sectors():
    """2개만 -5%이면 트리거 안 됨."""
    drops = [
        SectorDrop(sector="반도체", change_pct=-0.06),
        SectorDrop(sector="2차전지", change_pct=-0.055),
        SectorDrop(sector="바이오", change_pct=-0.02),  # 트리거 X
    ]
    snap = _base_snapshot(major_sector_drops=drops)
    triggers = check_closed_conditions(snap)
    assert "sector_contagion" not in triggers


def test_multiple_triggers_all_collected():
    """여러 조건 동시 충족 시 모두 반환."""
    drops = [
        SectorDrop(sector="A", change_pct=-0.06),
        SectorDrop(sector="B", change_pct=-0.07),
        SectorDrop(sector="C", change_pct=-0.08),
    ]
    snap = _base_snapshot(
        usd_krw_change_pct=0.035,
        kospi_20d_vol=0.40,
        major_sector_drops=drops,
    )
    triggers = check_closed_conditions(snap)
    assert set(triggers) == {"fx_shock", "high_volatility", "sector_contagion"}


# =====================================================================
# 지정학 — high_impact_geopolitical_count 임계
# =====================================================================


def test_geopolitical_three_events_triggers():
    """24h high-impact geopolitical 3건 → 결정론 closed."""
    snap = _base_snapshot(high_impact_geopolitical_count=3)
    triggers = check_closed_conditions(snap)
    assert "geopolitical" in triggers


def test_geopolitical_two_events_no_trigger():
    """2건 미만이면 LLM 판단에 위임 — 결정론 트리거 안 됨."""
    snap = _base_snapshot(high_impact_geopolitical_count=2)
    triggers = check_closed_conditions(snap)
    assert "geopolitical" not in triggers


def test_geopolitical_zero_baseline():
    """0건은 당연히 트리거 X."""
    snap = _base_snapshot()
    assert "geopolitical" not in check_closed_conditions(snap)


# =====================================================================
# 유동성 — turnover_ratio + spread_ratio AND 결합
# =====================================================================


def test_liquidity_both_conditions_trigger():
    """거래대금 0.4 (절반 이하) AND 스프레드 2.5배 → liquidity_crunch."""
    snap = _base_snapshot(kospi_turnover_ratio=0.4, kospi_spread_ratio=2.5)
    triggers = check_closed_conditions(snap)
    assert "liquidity_crunch" in triggers


def test_liquidity_only_turnover_no_trigger():
    """거래대금 급감 단독으로는 트리거 X (오탐 방지 AND 결합)."""
    snap = _base_snapshot(kospi_turnover_ratio=0.3, kospi_spread_ratio=1.1)
    triggers = check_closed_conditions(snap)
    assert "liquidity_crunch" not in triggers


def test_liquidity_only_spread_no_trigger():
    """스프레드 확대 단독으로도 트리거 X."""
    snap = _base_snapshot(kospi_turnover_ratio=0.9, kospi_spread_ratio=2.5)
    triggers = check_closed_conditions(snap)
    assert "liquidity_crunch" not in triggers


def test_liquidity_missing_data_fail_open():
    """둘 중 하나라도 None 이면 평가 skip — fail-open."""
    snap = _base_snapshot(kospi_turnover_ratio=None, kospi_spread_ratio=2.5)
    assert "liquidity_crunch" not in check_closed_conditions(snap)
    snap = _base_snapshot(kospi_turnover_ratio=0.3, kospi_spread_ratio=None)
    assert "liquidity_crunch" not in check_closed_conditions(snap)


def test_liquidity_boundary_values_trigger():
    """경계값: turnover 정확히 0.5, spread 정확히 2.0 → 둘 다 충족."""
    snap = _base_snapshot(kospi_turnover_ratio=0.5, kospi_spread_ratio=2.0)
    assert "liquidity_crunch" in check_closed_conditions(snap)
