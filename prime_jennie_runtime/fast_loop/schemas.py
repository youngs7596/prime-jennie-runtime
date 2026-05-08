"""Fast Loop ↔ 외부 시스템 Pydantic 계약.

`v3:notifications` 스트림의 메시지 포맷과 executions/outcomes 레코드.
Telegram bot이 notification을 읽어 포맷, DB writer가 executions를 읽어 INSERT.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# =====================================================================
# 알림 envelope (v3:notifications)
# =====================================================================


class TradeNotification(BaseModel):
    """entry 체결 / exit 체결 알림.

    텔레그램 표기 보강을 위한 선택 필드:
    - ``stock_name``: ticker 외 한국어 종목명. 없으면 "" → formatter 가 ticker 만 표기.
    - ``entry_avg_price``: 청산 시 PnL 계산 기준이 된 진입 평균가 (DRY_RUN 등 미정 시 None).
    - ``pnl_amount`` / ``pnl_pct``: 청산 시 손익 (원, %). entry_avg_price 가 없거나
      DRY_RUN(price=0) 이면 None — formatter 는 None 인 줄을 생략.
    """

    kind: Literal["entry_filled", "exit_filled"]
    sheet_id: str
    ticker: str
    side: Literal["buy", "sell"]
    quantity: int
    price: float
    ts: datetime
    reason: str = ""  # exit 경우 rule type
    is_partial: bool = False
    stock_name: str = ""
    entry_avg_price: float | None = None
    pnl_amount: float | None = None
    pnl_pct: float | None = None


class RiskLevelChangeNotification(BaseModel):
    """Intraday Risk Throttle 단계 변화 알림."""

    kind: Literal["risk_level_change"] = "risk_level_change"
    prev_level: str
    new_level: str
    prev_multiplier: float
    new_multiplier: float
    kospi_change_pct: float | None = None
    vix: float | None = None
    sox_change_pct: float | None = None
    ts: datetime


class GenericAlertNotification(BaseModel):
    """기타 알림 (auto_override, gate_closed, hallucination, stale 등)."""

    kind: Literal["alert"] = "alert"
    severity: Literal["info", "warning", "critical"]
    title: str
    body: str
    ts: datetime
    metadata: dict[str, object] = Field(default_factory=dict)


# =====================================================================
# 실행 기록 (executions 테이블)
# =====================================================================


class ExecutionRecord(BaseModel):
    """체결 이력. executions 테이블에 그대로 INSERT (PostgresTradeRecorder 직쓰기)."""

    sheet_id: str
    ticker: str
    side: Literal["buy", "sell"]
    order_no: str
    requested_qty: int
    filled_qty: int
    requested_price: float | None = None
    filled_price: float | None = None
    order_type: Literal["limit", "market"]
    status: Literal["pending", "filled", "partial", "cancelled", "failed"]
    submitted_at: datetime
    filled_at: datetime | None = None
    cancel_reason: str | None = None
    raw_response: dict[str, object] = Field(default_factory=dict)
