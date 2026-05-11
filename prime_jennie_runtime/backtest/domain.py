"""v3 백테스트 도메인 타입.

fast_loop.exit_evaluator 를 재사용하는 얇은 시뮬레이터가 사용하는 입력/결과
객체만 정의한다. v2 engine 의 MarketRegime/TradeTier/SignalType 같은 v2 전용
enum 은 전부 제거 — v3 는 PositionSheet + PositionState 로 충분하다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal


@dataclass(frozen=True)
class DailyBar:
    """일봉 하나."""

    ticker: str
    price_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass(frozen=True)
class Trade:
    """백테스트 체결 1건 — entry 또는 exit. scale_out 시 동일 sheet 에 exit 여러 개."""

    sheet_id: str
    ticker: str
    side: Literal["buy", "sell"]
    qty: int
    price: float  # 슬리피지 반영 체결가
    fee: float
    ts: datetime
    # entry: "market"|"limit"|"limit_unfilled"; exit: ExitDecision.reason | "data_end"
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SheetBacktestResult:
    """단일 PositionSheet 시뮬레이션 결과."""

    sheet_id: str
    ticker: str
    strategy_tag: str
    filled: bool  # 진입 체결 여부
    trades: list[Trade]
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None  # 마지막 exit. 부분 청산이면 마지막 부분 청산 시각
    entry_price: float = 0.0  # 슬리피지 반영
    total_qty: int = 0  # 진입 당시 최대 보유 수량
    gross_pnl_krw: float = 0.0  # 수수료 제외 (exit 가액 − entry 가액)
    fees_krw: float = 0.0  # 매수 수수료 + 매도 수수료 합
    net_pnl_krw: float = 0.0  # gross_pnl − fees
    pnl_pct: float = 0.0  # net_pnl / (entry_price × total_qty) × 100
    exit_reason: str | None = None  # 최종 full-close Trade 의 reason. unfilled 면 entry 실패 사유


@dataclass
class BacktestConfig:
    """시뮬레이션 파라미터. 시트별로 공유.

    Volume-based slippage:
    - ``volume_based_slippage_enabled=False`` (기본) — 모든 체결에 일정한
      ``slippage_pct`` 적용. 기존 백테스트 결과와 호환.
    - True 면 체결 notional (price × qty) 을 일봉 volume × price 와 비교한
      참여율 ``participation`` 에 비례하여 추가 슬리피지를 더한다:
        extra_pct = slippage_pct × participation_slippage_multiplier × participation
      예) participation 5% × multiplier 4 = base slippage 의 +20% (=0.02% 가산).
      participation 은 ``max_participation_clip`` 로 상한 클립 (기본 50%).
    """

    buy_fee_pct: float = 0.015  # 매수 수수료 % (0.015 = 0.015%)
    sell_fee_pct: float = 0.195  # 매도 수수료+세금 %
    slippage_pct: float = 0.1  # 체결 가격 슬리피지 % (매수: +, 매도: −)
    # ---- volume-based slippage (Phase 2.10 정밀화) ----
    volume_based_slippage_enabled: bool = False
    participation_slippage_multiplier: float = 4.0
    max_participation_clip: float = 0.5  # participation > 50% 는 50% 로 클립
