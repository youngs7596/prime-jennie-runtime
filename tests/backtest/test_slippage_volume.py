"""Volume-based slippage model (BacktestConfig.volume_based_slippage_enabled).

배경: 기본 모드는 고정 0.1% slippage. 거래량 대비 큰 주문은 분봉 흡수가 부족해
실제 체결가 악화 — 백테스트에서도 이를 반영해 일봉 거래량 대비 참여율을
이용해 추가 슬리피지를 더한다.

수식:
- participation = qty / bar.volume (max_participation_clip 으로 클립)
- extra_pct = slippage_pct × participation_slippage_multiplier × participation
- effective_pct = slippage_pct + extra_pct

backward compat — flag 미설정 시 결과 동일.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from prime_jennie_runtime.backtest import (
    BacktestConfig,
    DailyBar,
    simulate_sheet,
)
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    EntrySection,
    ExitSection,
    FixedSlRule,
    MacroStateSnapshot,
    PositionSheet,
    ProvenanceSection,
    SizeSection,
    TimeStopRule,
)


def _sheet(ticker: str = "005930") -> PositionSheet:
    gen = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    return PositionSheet(
        sheet_id="ps_20260504_090000_a1b2",
        schema_version="1.1",
        generated_at=gen,
        valid_until=gen.replace(hour=15, minute=20),
        ticker=ticker,
        side="long",
        strategy_tag="GAP_UP_REBOUND",
        size=SizeSection(
            base_pct=0.05,
            macro_multiplier=1.0,
            risk_multiplier=1.0,
            final_pct=0.05,
            max_notional_krw=10_000_000_000,
        ),
        entry=EntrySection(
            trigger="market", price=None, valid_until=gen.replace(hour=15, minute=20)
        ),
        exit=ExitSection(
            rules=[FixedSlRule(pct=0.10), TimeStopRule(mode="hold_days", value=5)]
        ),
        provenance=ProvenanceSection(
            scout_run_id="scout_test",
            scout_code_hash="sha256:test",
            scout_hypothesis="test",
            macro_state_snapshot=MacroStateSnapshot(
                gate="open", size_multiplier=1.0, gate_run_id="macro_test"
            ),
            strategy_policy_version="test",
            generated_by="test",
        ),
    )


def _bars_with_volume(volume: int, n: int = 6) -> list[DailyBar]:
    bars = []
    d = date(2026, 5, 4)
    # 같은 가격으로 flat — sl/tp 가 안 잡히고 time_stop=5일 후 강제 청산.
    for _ in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        bars.append(
            DailyBar(
                ticker="005930",
                price_date=d,
                open=10_000.0,
                high=10_000.0,
                low=10_000.0,
                close=10_000.0,
                volume=volume,
            )
        )
        d += timedelta(days=1)
    return bars


def test_default_slippage_unchanged_when_flag_off():
    """volume_based_slippage_enabled=False (기본) → 거래량 무관 동일 체결가."""
    cfg = BacktestConfig()  # default = False
    assert cfg.volume_based_slippage_enabled is False

    small_vol = simulate_sheet(
        _sheet(), _bars_with_volume(1_000), capital_krw=1_000_000, config=cfg
    )
    big_vol = simulate_sheet(
        _sheet(), _bars_with_volume(10_000_000), capital_krw=1_000_000, config=cfg
    )
    # 두 경우 entry_price 동일 (10000 × 1.001 = 10010)
    assert small_vol.entry_price == pytest.approx(big_vol.entry_price, abs=0.001)
    assert small_vol.entry_price == pytest.approx(10_010.0, abs=0.01)


def test_volume_based_increases_slippage_for_low_volume_bar():
    """flag on + 분봉 거래량 작으면 진입가가 더 높게 잡힌다 (매수 불리)."""
    cfg = BacktestConfig(volume_based_slippage_enabled=True)
    # capital 1억 / 가격 1만 = qty 약 9900. volume=100_000 → participation ≈ 9.9%
    # extra = 0.1% × 4.0 × 0.099 ≈ 0.04% → effective ≈ 0.14%
    result = simulate_sheet(
        _sheet(), _bars_with_volume(100_000), capital_krw=100_000_000, config=cfg
    )
    assert result.filled
    # base 만 적용한 가격 (10010) 보다 entry_price 가 의미있게 더 높다.
    assert result.entry_price > 10_010.0
    assert result.entry_price < 10_030.0  # 너무 크지도 않음


def test_volume_based_clipped_at_max_participation():
    """qty / volume 이 50% 를 넘어가도 50% 로 클립되어 무한 가속하지 않는다."""
    cfg = BacktestConfig(
        volume_based_slippage_enabled=True,
        max_participation_clip=0.5,
    )
    # 매우 작은 volume — participation 100% 인 상황 (50% 로 클립되어야)
    result_huge = simulate_sheet(
        _sheet(), _bars_with_volume(10), capital_krw=100_000_000, config=cfg
    )
    # max extra = 0.1% × 4 × 0.5 = 0.2% → effective = 0.3% → entry ≈ 10_030
    assert result_huge.filled
    assert result_huge.entry_price == pytest.approx(10_030.0, abs=0.5)


def test_volume_based_zero_volume_falls_back_to_base():
    """bar.volume == 0 인 corner case → base slippage 만 적용."""
    cfg = BacktestConfig(volume_based_slippage_enabled=True)
    bars = _bars_with_volume(0)
    result = simulate_sheet(_sheet(), bars, capital_krw=1_000_000, config=cfg)
    # qty 가 양수면 base 만 적용 → 10_010
    if result.filled:
        assert result.entry_price == pytest.approx(10_010.0, abs=0.01)


def test_exit_slippage_also_volume_aware():
    """exit 가격도 volume 적용 — 매도 시 가격이 더 낮게 (불리하게) 잡힌다."""
    bars = _bars_with_volume(100_000)
    cfg_off = BacktestConfig(volume_based_slippage_enabled=False)
    cfg_on = BacktestConfig(volume_based_slippage_enabled=True)
    res_off = simulate_sheet(_sheet(), bars, capital_krw=100_000_000, config=cfg_off)
    res_on = simulate_sheet(_sheet(), bars, capital_krw=100_000_000, config=cfg_on)
    assert res_off.filled and res_on.filled
    # 동일 시뮬에서 매도가만 보자 — sell trade 가격이 on 에서 더 낮아야.
    sell_off = next(t for t in res_off.trades if t.side == "sell")
    sell_on = next(t for t in res_on.trades if t.side == "sell")
    assert sell_on.price < sell_off.price
