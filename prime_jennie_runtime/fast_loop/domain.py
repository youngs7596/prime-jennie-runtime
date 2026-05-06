"""Fast Loop 내부 도메인 타입.

PositionSheet를 실행 중에 유지하는 런타임 상태 + exit decision 표현.
Redis Hash `position_state:{sheet_id}`에 직렬화되어 재시작 시 복구.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class PositionState:
    """단일 포지션의 장중 상태.

    시트 entry 체결 직후 생성 → exit까지 유지. Redis Hash로 백업.
    """

    sheet_id: str
    ticker: str
    entry_price: float
    quantity: int
    entered_at: datetime

    # 고점 추적 (trailing_tp, profit_floor용)
    high_watermark: float = 0.0  # 체결 후 관측된 최고가
    peak_return_pct: float = 0.0  # high_watermark / entry_price - 1.0

    # activate 플래그 (trailing_tp, profit_floor, breakeven)
    trailing_active: bool = False
    profit_floor_active: bool = False
    breakeven_active: bool = False

    # breakeven 발동 시 SL 상향된 가격 (None이면 미적용)
    breakeven_sl_price: float | None = None

    # scale_out 레벨별 실행 여부 (levels index set)
    scale_out_executed: set[int] = field(default_factory=set)

    # death_cross는 일봉 기반 — 하루 한 번만 평가
    death_cross_checked_at: datetime | None = None

    # overextension — 1분봉 RSI 과열 청산 (1회만)
    overextension_triggered: bool = False

    def update_high(self, price: float) -> None:
        """새 고가 반영."""
        if price > self.high_watermark:
            self.high_watermark = price
            if self.entry_price > 0:
                self.peak_return_pct = price / self.entry_price - 1.0

    def current_return_pct(self, price: float) -> float:
        """현재가 기준 수익률."""
        if self.entry_price <= 0:
            return 0.0
        return price / self.entry_price - 1.0


@dataclass(frozen=True)
class ExitDecision:
    """exit_evaluator가 반환하는 판정.

    청산할지 / 어떤 rule이 트리거했는지 / 얼마나 청산할지.
    breakeven은 청산 아닌 SL 상향 보조 rule — should_close=False이지만
    new_sl_price가 설정됨.
    """

    should_close: bool
    reason: str  # "trailing_tp" | "fixed_sl" | ... | "breakeven_sl_raise"
    portion: float = 1.0  # 0.0~1.0. scale_out은 부분 청산
    new_sl_price: float | None = None  # breakeven 발동 시 사용
    matched_rule_type: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TickData:
    """1틱 가격 이벤트.

    exit rule 평가에 필요한 최소 정보. 1분봉 지표(rsi_1m)는 상위에서 미리 계산.
    bid/ask 는 entry condition `spread_under_bps` 평가용 (KIS H0STCNT0 호가1).
    """

    ticker: str
    price: float
    ts: datetime
    rsi_1m: float | None = None  # overextension_exit 평가용
    volume: float | None = None
    bid: float | None = None
    ask: float | None = None


@dataclass
class FillEvent:
    """Entry 또는 Exit 체결 이벤트. notifier가 포맷하여 발행."""

    sheet_id: str
    ticker: str
    side: Literal["buy", "sell"]
    quantity: int
    price: float
    ts: datetime
    order_no: str
    is_partial: bool = False
    reason: str = ""  # exit 경우 어떤 rule이 트리거했는지
