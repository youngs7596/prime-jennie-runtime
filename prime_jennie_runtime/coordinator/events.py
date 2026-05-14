"""Coordinator Event Bus 의 이벤트 schema (Pydantic).

design: .ai/designs/2026-05-14-agent-coordinator.md §3 Layer C (Event Bus)
저장 path: event_log 테이블 (migrations/017_coordinator_logs.sql)

이벤트는 Coordinator 가 받아서 event_log + (해당 시) decision_log 에 기록한다.
Stage 1 (현재) 에선 read-only 기록만, agents 의 기존 path 침해 X.

이벤트 lifecycle (한 시트의 happy path):
    sheet_published  (slow_loop pipeline → Coordinator)
        ↓
    entry_queued     (fast_loop consumer → Coordinator)
        ↓
    entry_decided    (fast_loop tick_loop → Coordinator, conditions 통과 시점)
        ↓
    entry_filled     (entry_executor → Coordinator)
        또는 entry_rejected (KIS reject / not_filled 시)
        ↓
    exit_decided     (fast_loop tick_loop → Coordinator, exit rule 매칭 시점)
        ↓
    exit_filled      (exit_executor → Coordinator)

다른 event:
    policy_changed   (macro / risk_throttle 변경 시)
    external_event   (사용자 수동 거래 감지, KIS sync mismatch 등)

correlation_id 는 한 시트의 모든 이벤트를 묶는다 (보통 sheet_id 와 동일).
policy/external 은 별도 correlation_id (예: policy_change_id, external_event_id).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────


class _BaseEvent(BaseModel):
    """모든 Coordinator 이벤트의 공통 필드.

    Stage 1 read-only 단계에선 occurred_at 만 신뢰. recorded_at 은 Coordinator
    listener 가 받은 시각 (event_log.recorded_at). decision_id 는 Coordinator
    의사결정 결과 이벤트일 때만 채워짐.
    """

    occurred_at: datetime = Field(description="이벤트 발생 시각 (agent 가 채움)")
    correlation_id: str = Field(description="시트 lifecycle 묶는 id. 보통 sheet_id")


# ─────────────────────────────────────────────────────────────────────
# Lifecycle events (한 시트의 happy path)
# ─────────────────────────────────────────────────────────────────────


class SheetPublishedEvent(_BaseEvent):
    """Strategy Engine 이 시트를 발행하여 stream 에 publish 한 직후.

    audit B1/B2 의 핵심 진입점. 같은 ticker 가 매시간 반복 발행되는 패턴이
    이 이벤트의 ticker + correlation_id 통계로 정량화됨.
    """

    event_kind: Literal["sheet_published"] = "sheet_published"

    sheet_id: str
    ticker: str
    strategy_tag: str
    conviction: float | None = None
    final_pct: float
    macro_run_id: str | None = None
    scout_run_id: str | None = None
    # 발행 시점 컨텍스트 (Phase 0 multi-dim 분석용)
    macro_gate: Literal["closed", "open"] | None = None
    macro_size_mult: float | None = None
    risk_level: str | None = None
    sheet_generated_at: datetime


class EntryQueuedEvent(_BaseEvent):
    """fast_loop consumer 가 시트를 pending_entry queue 에 적재한 직후.

    sheet_published 후 dedup 가드 통과 여부를 추적한다. consumer 가 reject
    한 경우엔 entry_rejected (reason=already_holding/ticker_pending) 로 별도 발행.
    """

    event_kind: Literal["entry_queued"] = "entry_queued"

    sheet_id: str
    ticker: str
    queue_size_after: int = Field(description="적재 후 큐 전체 크기")


class EntryDecidedEvent(_BaseEvent):
    """tick_loop 가 entry conditions 평가 결과 terminal 분기에 도달한 시점.

    실제 매수 호출 직전이며 Stage 2 (advisory) 부터 Coordinator 가 query
    응답을 보낼 시점. Stage 1 에선 평가 결과만 기록.

    **발행 정책 (Stage 1)** — terminal 분기만 발행하여 매 tick 폭주 회피:
      - ``passed_reason="all_passed"`` — 조건 통과 + 사이저 >0 → 매수 호출
      - ``passed_reason="entry_valid_until_expired"`` — 큐에서 제거되는 시한 만료
      - ``passed_reason="sizer_zero"`` — 통과했지만 수량 0 (skip)

    Non-terminal "conditions_not_yet_met" (이번 tick 미통과, 다음 tick 재평가)
    은 같은 sheet 가 수천 tick × 수십 큐 시트 만큼 발행되어 폭주하므로 미발행.
    Stage 2 에서 sheet_id+reason 기반 dedup cache 도입 후 관찰력 확장 검토.
    """

    event_kind: Literal["entry_decided"] = "entry_decided"

    sheet_id: str
    ticker: str
    conditions_passed: bool
    passed_reason: str = Field(description="all_passed / no_conditions / ...")
    intended_quantity: int = Field(description="sizer 가 산출한 수량 (0 이면 skip)")
    decision_id: int | None = Field(
        default=None, description="Coordinator decision_log 의 id (Stage 2+)"
    )


class EntryFilledEvent(_BaseEvent):
    """entry_executor 가 매수 체결 성공.

    filled_qty < intended_quantity 면 partial. audit A2/D2 (부분체결 잔량
    drift) 의 검출 단서.
    """

    event_kind: Literal["entry_filled"] = "entry_filled"

    sheet_id: str
    ticker: str
    filled_qty: int
    filled_price: float
    intended_quantity: int
    is_partial: bool
    order_no: str | None = None


class EntryRejectedEvent(_BaseEvent):
    """entry 가 거부됨 — consumer dedup / sizer 0 / KIS reject / not_filled / DRY_RUN.

    audit A1 (무한 retry) 진단을 위해 reason 별 빈도 추적. 같은 sheet_id 의
    rejected 누적이 임계 도달 시 Stage 3 에서 차단.
    """

    event_kind: Literal["entry_rejected"] = "entry_rejected"

    sheet_id: str
    ticker: str
    reason: str = Field(
        description=(
            "already_holding | ticker_pending | sheet_id_in_queue | sizer_zero | "
            "conditions_not_met_terminal | kis_reject | not_filled | dryrun | error"
        )
    )
    details: str | None = None


class ExitDecidedEvent(_BaseEvent):
    """tick_loop 가 exit rule 매칭 → exit_executor 호출 직전.

    Stage 1: rule_type + 매칭 사유 기록.
    Stage 2+: Coordinator 가 검증 (forced_liquidation 우회 등).
    """

    event_kind: Literal["exit_decided"] = "exit_decided"

    sheet_id: str
    ticker: str
    rule_type: str = Field(description="fixed_sl | trailing_tp | breakeven | ...")
    reason: str
    portion: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    decision_id: int | None = None


class ExitFilledEvent(_BaseEvent):
    """exit_executor 가 매도 체결 성공.

    Phase 0 #1 (conviction × pnl_pct) + #2 (손절 % 진단) 의 outcome 원천.
    pnl_pct = (filled_price / entry_price - 1) * 100. entry_price 는 PositionState
    의 평균단가 (분할 매수 시 가중평균).
    """

    event_kind: Literal["exit_filled"] = "exit_filled"

    sheet_id: str
    ticker: str
    filled_qty: int
    filled_price: float
    entry_price: float
    reason: str = Field(description="fixed_sl | trailing_tp | ...")
    fully_closed: bool
    pnl_amount: float | None = None
    pnl_pct: float | None = None


# ─────────────────────────────────────────────────────────────────────
# 정책 / 시스템 이벤트
# ─────────────────────────────────────────────────────────────────────


class PolicyChangedEvent(_BaseEvent):
    """macro gate / risk_throttle level / control state 등 정책 변경.

    Phase 0 #3 (Macro calibration) 에서 정책 전환 시점과 그 후 portfolio
    outcome 결합 분석.
    """

    event_kind: Literal["policy_changed"] = "policy_changed"

    policy_name: str = Field(
        description="macro_gate | risk_level | control_stop | control_pause | ..."
    )
    prev_value: str | None = None
    new_value: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalEvent(_BaseEvent):
    """사용자 수동 거래 감지 / KIS sync mismatch / WS reconnect 등 외부 사건.

    audit D1 (장중 외부 매수 미감지) 의 검출 path. Coordinator 가 정기
    sync_check 에서 mismatch 감지 시 발행.
    """

    event_kind: Literal["external_event"] = "external_event"

    event_subtype: str = Field(
        description="kis_state_mismatch | manual_trade_detected | ws_reconnect | task_died | ..."
    )
    ticker: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Discriminated Union
# ─────────────────────────────────────────────────────────────────────


CoordinatorEvent = Annotated[
    SheetPublishedEvent
    | EntryQueuedEvent
    | EntryDecidedEvent
    | EntryFilledEvent
    | EntryRejectedEvent
    | ExitDecidedEvent
    | ExitFilledEvent
    | PolicyChangedEvent
    | ExternalEvent,
    Field(discriminator="event_kind"),
]


__all__ = [
    "CoordinatorEvent",
    "EntryDecidedEvent",
    "EntryFilledEvent",
    "EntryQueuedEvent",
    "EntryRejectedEvent",
    "ExitDecidedEvent",
    "ExitFilledEvent",
    "ExternalEvent",
    "PolicyChangedEvent",
    "SheetPublishedEvent",
]
