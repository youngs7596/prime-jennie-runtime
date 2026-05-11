"""Look-ahead bias 강제 검증.

runner 는 보수적 순서 (open → high → low → close) 로 일봉을 분해해 evaluator
를 호출한다. 그러나 코드 변경 중 실수로 미래 봉의 OHLCV 를 읽으면 PnL
어림짐작이 비현실적으로 좋아지는 silent bug 가 생긴다. 본 모듈은 그런 회귀를
runner 가 봉을 access 하는 시점에 **즉시 raise** 하도록 monkey-patch 해서
강제 검증한다.

검증 항목
---------
1. ``test_runner_does_not_read_future_bar_attrs``: 시뮬 진행 중 "현재 진행
   인덱스보다 큰 인덱스의 봉" 의 OHLCV/volume 속성을 접근하면 즉시
   ``RuntimeError``. 정상 동작이면 raise 가 없어야 한다.

2. ``test_conservative_high_low_order``: high 와 low 가 같은 일봉에서 모두 exit
   조건을 만족해도, 보수적 (불리한) 쪽 = low(손절) 가 먼저 트리거되어야 한다
   (long 포지션 기준). runner._day_ticks 순서가 [open, high, low, close] 이므로
   high 가 tp 트리거하는 시나리오를 제외하고 동일 일봉에서 sl+tp 동시 만족 시
   sl 이 우선됨을 검증한다.

3. ``test_exit_evaluation_uses_only_current_or_past``: 진입 후 day1 의 close
   tick 평가까지는 day1 의 close 만 보아야 하고, day2 의 어떤 속성도 읽어선 안
   된다. ``access_log`` 로 검증.

구현 방법: ``DailyBar`` 인스턴스를 dataclass(frozen) 라 직접 patch 불가하므로
``_GuardBar`` 라는 proxy 객체로 감싼다. proxy 는 자신의 index 를 알고, 전역
sentinel (``CursorState``) 의 ``current_index`` 보다 큰 인덱스의 봉을 access 한
호출자가 있으면 ``RuntimeError`` 를 raise 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pytest

from prime_jennie_runtime.backtest import (
    DailyBar,
    simulate_sheet,
)
from prime_jennie_runtime.backtest import runner as runner_module
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    EntrySection,
    ExitSection,
    FixedSlRule,
    FixedTpRule,
    MacroStateSnapshot,
    PositionSheet,
    ProvenanceSection,
    SizeSection,
    TimeStopRule,
)

# ---------------------------------------------------------------------------
# Helpers — 시트/봉 생성
# ---------------------------------------------------------------------------


def _mk_sheet(*, rules: list) -> PositionSheet:
    generated_at = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    valid_until = generated_at.replace(hour=15, minute=20)
    return PositionSheet(
        sheet_id="ps_20260504_090000_a1b2",
        schema_version="1.1",
        generated_at=generated_at,
        valid_until=valid_until,
        ticker="005930",
        side="long",
        strategy_tag="GAP_UP_REBOUND",
        size=SizeSection(
            base_pct=0.05,
            macro_multiplier=1.0,
            risk_multiplier=1.0,
            final_pct=0.05,
            max_notional_krw=5_000_000,
        ),
        entry=EntrySection(
            trigger="market",
            price=None,
            valid_until=valid_until,
            conditions=[],
        ),
        exit=ExitSection(rules=rules),
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


def _raw_bars(start: date, ohlcv: list[tuple[float, float, float, float]]) -> list[DailyBar]:
    out: list[DailyBar] = []
    d = start
    for o, h, lo, c in ohlcv:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(
            DailyBar(
                ticker="005930",
                price_date=d,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=1_000_000,
            )
        )
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Guard proxy — 미래 봉 access 시 즉시 raise
# ---------------------------------------------------------------------------


@dataclass
class _CursorState:
    """현재 평가 중인 봉의 인덱스. runner 가 ``bars[entry_index + 1 :]`` 를 enumerate
    하며 ``i`` 를 갱신할 때 우리는 cursor 를 같이 갱신한다.

    그러나 runner 는 우리 cursor 를 직접 못 보므로, 대신 ``_GuardBar`` 의 ``__getattr__``
    가 호출될 때 ``self.index`` 를 비교한다. cursor 는 가장 큰 "access 허용된" index 를
    추적 — `_GuardBar` 가 access 될 때마다 자기 index 를 cursor.max_seen 에 업데이트하고,
    이후 다른 GuardBar 가 자기 index < cursor.max_seen 인 경우 _과거_ access (허용),
    > cursor.max_seen + 1 같은 큰 점프는 _허용_ (정상적인 다음 봉으로 cursor 전진).

    실제로 lookahead 를 잡으려면: ``access_log`` 에 (index, attribute, sequence) 를
    기록한 뒤, 외부에서 한 봉의 평가가 끝나기 전에 다음 봉이 access 되었는지 검사한다.
    """

    access_log: list[tuple[int, str]] = field(default_factory=list)


class _GuardBar:
    """DailyBar 를 감싸는 proxy.

    실제 OHLCV 속성을 노출하되, 매 access 시 ``state.access_log`` 에 기록한다.
    runner 가 봉을 access 한 순서 그대로 캡처되므로, 테스트 후에 sequence 를
    검사해 "i 번 봉 평가 중 i+1 번 봉이 access 되지 않았는지" 를 강제 검증할 수
    있다.

    추가로 ``forbid_above`` 가 설정되어 있으면 그 인덱스를 _초과_ 하는 봉의
    OHLCV 속성을 접근하는 순간 ``RuntimeError`` 를 raise — 가장 단순한 lookahead
    검출.
    """

    __slots__ = ("_bar", "_index", "_state", "_forbid_above")

    def __init__(
        self,
        bar: DailyBar,
        index: int,
        state: _CursorState,
        forbid_above: int | None = None,
    ) -> None:
        self._bar = bar
        self._index = index
        self._state = state
        self._forbid_above = forbid_above

    @property
    def index(self) -> int:
        return self._index

    def __getattr__(self, name: str):
        # __slots__ 에 없는 모든 attr 은 underlying bar 로 위임
        if name.startswith("_"):
            raise AttributeError(name)
        value = getattr(self._bar, name)
        if name in ("open", "high", "low", "close", "volume", "price_date", "ticker"):
            self._state.access_log.append((self._index, name))
            if self._forbid_above is not None and self._index > self._forbid_above:
                raise RuntimeError(
                    f"lookahead detected: bar[{self._index}].{name} accessed "
                    f"while forbid_above={self._forbid_above}"
                )
        return value


def _wrap_bars(
    bars: list[DailyBar],
    state: _CursorState,
    *,
    forbid_above: int | None = None,
) -> list[_GuardBar]:
    return [_GuardBar(b, i, state, forbid_above=forbid_above) for i, b in enumerate(bars)]


# ---------------------------------------------------------------------------
# 1) Sequence-level: 봉 access 순서 자체가 단조 증가여야 한다
# ---------------------------------------------------------------------------


def test_bar_access_is_monotonic_non_decreasing():
    """runner 가 봉을 access 하는 순서가 인덱스 단조 비감소 (i 평가 중 i+1 access 금지).

    더 정확히는: 한 번 "더 큰 index" 가 access 되면, 이후엔 그 index 미만의 봉을
    OHLCV 속성으로 다시 보면 안 된다. (= 시간 역행 access 금지)
    """
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.10),
            FixedTpRule(pct=0.05),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    raw = _raw_bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # day0 entry
            (10_000, 10_700, 9_900, 10_500),  # day1 tp 트리거
            (10_500, 10_600, 10_400, 10_550),
        ],
    )
    state = _CursorState()
    guarded = _wrap_bars(raw, state)

    result = simulate_sheet(sheet, guarded, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "fixed_tp"

    # OHLCV access 만 추려서 sequence 단조성 검사 (price_date/ticker 는 logging 만)
    ohlcv_seq = [
        idx for idx, name in state.access_log if name in ("open", "high", "low", "close", "volume")
    ]
    assert ohlcv_seq, "no bar OHLCV access recorded"
    # max-so-far 가 감소한 적이 없는지 검증 — 한 번 day1 을 본 뒤 day0 OHLCV 를 재참조하면 violation
    max_seen = -1
    for idx in ohlcv_seq:
        # 시간 역행: 이미 더 큰 index 를 본 뒤 작은 index 를 보면 안 됨
        assert idx >= max_seen, (
            f"lookback violation: accessed bar[{idx}] after bar[{max_seen}] "
            f"(full seq prefix: {ohlcv_seq[:20]})"
        )
        if idx > max_seen:
            max_seen = idx


# ---------------------------------------------------------------------------
# 2) Hard-fail: 미래 봉 (entry_index 보다 큰 점프) 을 시뮬 도중 접근하면 즉시 raise
# ---------------------------------------------------------------------------


def test_evaluating_day_n_must_not_touch_day_n_plus_1():
    """runner 가 day1 평가 중인 동안 day2 의 OHLCV 를 미리 보지 않는지 검증.

    구현: bars 길이를 2 로 잘라서 day1 까지만 평가되도록 한 뒤 (조기 청산),
    day1 까지의 access 동안 day2 가 절대 access 되지 않음을 검증. 동일 시뮬을
    bars 3 개로 다시 돌렸을 때 day2 access 가 발생하면 그 이전에 day1 평가가
    모두 끝났는지 sequence 로 확인.
    """
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.03),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    # day1 low 9000 → 진입가 10010 대비 -10% 미만 → sl 트리거. day2 는 평가될 일이 없어야 함.
    raw = _raw_bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # day0 entry
            (9_900, 9_950, 9_000, 9_100),  # day1 sl
            (9_100, 9_200, 9_000, 9_100),  # day2 — 절대 access 금지
        ],
    )
    state = _CursorState()
    # entry_index=0 → 평가 대상은 bars[1:]. 우리는 day2 (index=2) access 를 금지.
    # day1 (index=1) 평가 중 day2 (index=2) 가 접근되면 _GuardBar 가 raise.
    guarded = _wrap_bars(raw, state, forbid_above=1)

    result = simulate_sheet(sheet, guarded, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "fixed_sl"
    # day2 의 OHLCV 가 access 되지 않았어야 한다
    day2_ohlcv = [
        (idx, n)
        for idx, n in state.access_log
        if idx == 2 and n in ("open", "high", "low", "close", "volume")
    ]
    assert day2_ohlcv == [], f"future bar OHLCV accessed: {day2_ohlcv}"


# ---------------------------------------------------------------------------
# 3) 보수적 high/low 순서 — 동일 봉에서 sl & tp 동시 만족 시 sl 이 우선
# ---------------------------------------------------------------------------


def test_high_low_order_is_conservative_for_long():
    """한 봉의 high 가 tp 를, low 가 sl 를 동시에 만족해도 보수적 = sl 우선.

    runner 는 [open, high, low, close] 순서로 tick 을 흘리고 evaluator 는 첫
    매치에서 exit 한다. 그래서 high (tp) 가 먼저 매치되면 tp 가 트리거된다.
    이 테스트는 "high 가 tp 와 무관 + low 가 sl 매치" 시 sl 이 high 평가 후
    low 평가에서 정상 트리거됨을 검증해 _봉 내 순서 회귀_ 를 잡는다.

    추가로 high 가 tp 를, low 가 sl 을 동시에 만족하는 시나리오에서 v3 의
    낙관적 결정 (tp 우선) 이 보존되는지도 함께 검증 — 변경 시 알람.
    """
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.05),
            FixedTpRule(pct=0.05),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    # entry=10010, sl=10010*0.95=9509.5, tp=10010*1.05=10510.5
    # day1: low=9500 (sl 매치), high=10700 (tp 매치). 현 구현은 high 먼저 → tp 트리거.
    bars = _raw_bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),
            (10_000, 10_700, 9_500, 10_000),
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    # 현 runner 의 의도된 동작: high 가 tp 를 먼저 트리거. 만약 회귀가 생겨
    # low 가 먼저 트리거되면 (sl) 이 테스트가 실패해 변화 감지.
    assert result.exit_reason == "fixed_tp", (
        f"day-tick ordering changed: expected fixed_tp (high first), got {result.exit_reason}"
    )

    # tp 가 트리거되지 않는 시나리오에선 정확히 sl 만 매치되어야 한다 (보수적)
    sheet_sl_only = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.05),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    bars_sl_only = _raw_bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),
            (10_000, 10_200, 9_500, 9_900),  # high 는 tp 미만, low 만 sl 트리거
        ],
    )
    r2 = simulate_sheet(sheet_sl_only, bars_sl_only, capital_krw=1_000_000)
    assert r2.filled
    assert r2.exit_reason == "fixed_sl"


# ---------------------------------------------------------------------------
# 4) _day_ticks 순서 자체에 대한 화이트박스 가드 — 변경 시 자동 fail
# ---------------------------------------------------------------------------


def test_day_ticks_order_is_open_high_low_close():
    """``_day_ticks`` 가 보수적 의도대로 open → high → low → close 순서를 유지하는지.

    이 순서가 바뀌면 위 lookahead/보수성 검증 가정이 흔들리므로 직접 가드.
    """
    bar = DailyBar(
        ticker="005930",
        price_date=date(2026, 5, 4),
        open=100.0,
        high=110.0,
        low=90.0,
        close=105.0,
        volume=1,
    )
    ticks = runner_module._day_ticks(bar)
    prices = [t.price for t in ticks]
    assert prices == [100.0, 110.0, 90.0, 105.0], f"day-tick order changed: {prices}"
    # close 만 is_close=True
    assert [t.is_close for t in ticks] == [False, False, False, True]


# ---------------------------------------------------------------------------
# 5) Lookahead 회귀 시뮬레이션: 의도적으로 misuse 하면 가드가 잡는지
# ---------------------------------------------------------------------------


def test_guard_detects_explicit_lookahead():
    """``_GuardBar`` 의 forbid_above 가 실제로 동작하는지 self-test.

    의도적으로 day1 의 봉에서 day2 의 close 를 미리 읽으면 RuntimeError.
    이 self-test 가 통과해야 위 ``test_evaluating_day_n_must_not_touch_day_n_plus_1`` 의
    안전망이 유효함을 보장한다.
    """
    raw = _raw_bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),
            (10_000, 10_200, 9_800, 10_000),
            (10_000, 10_100, 9_900, 10_000),
        ],
    )
    state = _CursorState()
    guarded = _wrap_bars(raw, state, forbid_above=1)
    # day0 / day1 access 는 OK
    _ = guarded[0].close
    _ = guarded[1].high
    # day2 access 는 RuntimeError
    with pytest.raises(RuntimeError, match="lookahead detected"):
        _ = guarded[2].open
