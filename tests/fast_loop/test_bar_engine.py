"""BarEngine + IndicatorProvider 회귀 테스트.

- tick 누적 → 1분봉 close 시 history append
- intraday_cum_volume 일별 reset
- volume_ma20 / rsi / recent_high warm-up 동안 None, 충족 시 값
- BarEngineIndicatorProvider 어댑터가 BarEngine 값을 그대로 반환
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from prime_jennie_runtime.fast_loop.bar_engine import (
    BarEngine,
    BarEngineIndicatorProvider,
)


def _ts(minute_offset: int, second: int = 0) -> datetime:
    base = datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)
    return base + timedelta(minutes=minute_offset, seconds=second)


def test_update_starts_new_bar_then_closes_on_next_minute():
    eng = BarEngine(bar_interval_sec=60)
    # 첫 tick (분봉 0) — completed=None
    closed = eng.update("005930", price=100.0, volume=50, ts=_ts(0, 5))
    assert closed is None
    # 같은 분봉 두 번째 tick — 여전히 None
    closed = eng.update("005930", price=101.0, volume=30, ts=_ts(0, 30))
    assert closed is None
    # 다음 분봉 — 이전 분봉 close 되어 반환
    closed = eng.update("005930", price=102.0, volume=10, ts=_ts(1, 0))
    assert closed is not None
    assert closed.open == 100.0
    assert closed.high == 101.0
    assert closed.low == 100.0
    assert closed.close == 101.0
    assert closed.volume == 80


def test_intraday_cum_volume_resets_per_day():
    eng = BarEngine()
    eng.update("A", price=100.0, volume=100, ts=_ts(0, 0))
    eng.update("A", price=100.0, volume=200, ts=_ts(1, 0))  # 분봉 0 close + cum += 200
    assert eng.intraday_cum_volume("A", now=_ts(1, 1)) == 300

    # 다음 날 — cum reset
    next_day = _ts(0, 0) + timedelta(days=1)
    eng.update("A", price=100.0, volume=50, ts=next_day)
    assert eng.intraday_cum_volume("A", now=next_day) == 50


def test_volume_ma20_warmup_then_value():
    eng = BarEngine()
    # 19 분봉만 완성 — 부족
    for i in range(20):  # i=0..19 → 0~18번 close 됨 (마지막은 진행 중)
        eng.update("A", price=100.0, volume=1000, ts=_ts(i, 0))
    # 19 개 완성, 1 개 진행 중
    assert eng.bar_count("A") == 19
    assert eng.volume_ma20("A") is None

    # 한 분봉 더 close 시켜 20개 채움
    eng.update("A", price=100.0, volume=1000, ts=_ts(20, 0))
    assert eng.bar_count("A") == 20
    ma = eng.volume_ma20("A")
    assert ma == 1000.0


def test_rsi_warmup_then_value():
    eng = BarEngine()
    # 15개 close 분봉 (15+1 진행) — RSI 14 충족
    prices = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115]
    for i, p in enumerate(prices):
        eng.update("A", price=float(p), volume=100, ts=_ts(i, 0))
    # close 된 분봉이 14 개라 RSI period=14 는 None (15+1 필요)
    rsi = eng.rsi("A", period=14)
    # close 된 bar 수 = len(prices) - 1 = 15. period+1=15 → 충족 → 100 (모두 상승)
    assert rsi == 100.0


def test_recent_high_lookback():
    eng = BarEngine()
    # 분봉 close: high 값이 각각 [100, 105, 102, 108, 103, 110]
    seq = [(100, 100), (105, 105), (102, 102), (108, 108), (103, 103), (110, 110)]
    for i, (p, _h) in enumerate(seq):
        eng.update("A", price=float(p), volume=10, ts=_ts(i, 0))
    # 마지막 진행 중 분봉을 닫기 위해 한 분봉 더
    eng.update("A", price=120.0, volume=10, ts=_ts(len(seq), 0))
    # close 된 bar 들: high = [100, 105, 102, 108, 103, 110]
    assert eng.recent_high("A", lookback=5) == 110
    assert eng.recent_high("A", lookback=3) == 110
    assert eng.recent_high("A", lookback=2) == 110


def test_recent_high_warmup_returns_none():
    eng = BarEngine()
    eng.update("A", price=100.0, volume=10, ts=_ts(0, 0))
    eng.update("A", price=101.0, volume=10, ts=_ts(1, 0))  # 1 closed
    assert eng.recent_high("A", lookback=5) is None


def test_indicator_provider_adapter():
    eng = BarEngine()
    # 20개 완성 분봉 생성
    for i in range(21):
        eng.update("A", price=100.0 + i, volume=1000, ts=_ts(i, 0))

    provider = BarEngineIndicatorProvider(eng)
    assert provider.volume_ma20("A") == 1000.0
    assert provider.intraday_cum_volume("A") == 21000
    assert provider.rsi_1m("A") == 100.0  # 단조 상승
    assert provider.recent_high("A", lookback=5) is not None


def test_bar_engine_max_history_caps_growth():
    eng = BarEngine(max_history=5)
    for i in range(20):
        eng.update("A", price=100.0 + i, volume=10, ts=_ts(i, 0))
    # max 5 만 유지
    assert eng.bar_count("A") == 5
    bars = eng.recent_bars("A", count=10)
    assert len(bars) == 5
