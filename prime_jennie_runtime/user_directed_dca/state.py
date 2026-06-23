"""DCA 캠페인·슬라이스 상태 모델 — dca_campaigns / dca_slice_executions 행 매핑.

CampaignStatus 는 종목별 트랙(한 row)의 생애, SliceStatus 는 한 슬라이스 시도의 생애.
terminal 슬라이스(filled/partial/passed/expired_accel/skipped_cap)는 "그 슬라이스가
끝났다"를, non-terminal(pending/submitted/rejected_retry)은 "아직 진행 중"을 뜻한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

# 1 won 미만 의미 없는 주문 방지용 하한. 실제 1주 미달은 qty=budget//price 가 0 으로 잡는다.
MIN_ORDER_KRW = 1000

# slice_key slot — 같은 영업일에 예정(sched)·가속(accel)·꼬리(tail)를 구분.
SLOT_SCHED = "sched"
SLOT_ACCEL = "accel"
SLOT_TAIL = "tail"

# trigger_kind — 어떤 경로로 집행됐는지 감사용.
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_ACCELERATION = "acceleration"
TRIGGER_TAIL = "tail"


class CampaignStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CAP_REACHED = "cap_reached"
    PAUSED = "paused"  # 일시중지 — 슬라이스 집행만 멈추고 진행상태 보존, resume 으로 복귀
    HALTED = "halted"  # 영구 중단(취소) — 이어가려면 다시 무장


class SliceStatus(StrEnum):
    PENDING = "pending"  # 슬롯 선점(주문 전). reconcile 이 정리하는 표적.
    SUBMITTED = "submitted"  # 주문 발행 후 confirm 전. crash 복구 표적.
    FILLED = "filled"  # 전량 체결 (terminal)
    PARTIAL = "partial"  # 부분 체결 + 잔량 취소 (terminal)
    REJECTED_RETRY = "rejected_retry"  # 거부 → retry_at 후 1회 재시도 예약
    PASSED = "passed"  # 재시도도 실패/예산부족 → 슬라이스 패스, 예산 이월 (terminal)
    SKIPPED_CAP = "skipped_cap"  # 집행 직전 cap 도달로 스킵 (terminal)


# 그 슬라이스가 더는 움직이지 않는 상태.
TERMINAL_SLICE_STATUSES: frozenset[str] = frozenset(
    {
        SliceStatus.FILLED.value,
        SliceStatus.PARTIAL.value,
        SliceStatus.PASSED.value,
        SliceStatus.SKIPPED_CAP.value,
    }
)


@dataclass
class Campaign:
    """dca_campaigns 한 row = 종목별 독립 트랙."""

    id: str
    ticker: str
    status: str
    preset: str
    cap_krw: int
    total_slices: int
    execute_at_kst: str
    accel_open_pct: float
    accel_prev_pct: float
    accel_cooldown_h: int
    clamp_pct: float
    per_order_cost_cap: int
    per_day_cost_cap: int
    dry_run: bool
    cumulative_filled_krw: int
    cumulative_filled_qty: int
    slices_done: int
    last_accel_at: datetime | None
    final_tail_done: bool

    @property
    def remaining_cap_krw(self) -> int:
        return max(self.cap_krw - self.cumulative_filled_krw, 0)

    @property
    def base_per_slice_krw(self) -> float:
        return self.cap_krw / self.total_slices if self.total_slices else 0.0


@dataclass
class SliceExecution:
    """dca_slice_executions 한 row = 슬라이스 한 시도."""

    id: int
    slice_key: str
    campaign_id: str
    ticker: str
    slice_no: int
    trigger_kind: str
    trade_date: date
    status: str
    budget_krw: int
    intended_qty: int | None
    order_no: str | None
    filled_qty: int
    filled_krw: int
    avg_price: float
    retry_at: datetime | None
    retry_count: int
    reject_reason: str | None


__all__ = [
    "MIN_ORDER_KRW",
    "SLOT_ACCEL",
    "SLOT_SCHED",
    "SLOT_TAIL",
    "TERMINAL_SLICE_STATUSES",
    "TRIGGER_ACCELERATION",
    "TRIGGER_SCHEDULED",
    "TRIGGER_TAIL",
    "Campaign",
    "CampaignStatus",
    "SliceExecution",
    "SliceStatus",
]
