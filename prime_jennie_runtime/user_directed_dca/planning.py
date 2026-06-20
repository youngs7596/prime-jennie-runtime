"""DCA 결정 로직 — 순수 함수(DB·네트워크 무관)라 단위 테스트가 쉽다.

예산 산식·가속 트리거·다음 슬롯 선택을 여기 모은다. cap 불변식의 핵심인 예산
클램프가 전부 compute_slice_budget 한 곳을 지난다.
"""

from __future__ import annotations

from datetime import datetime

from .state import (
    SLOT_ACCEL,
    SLOT_SCHED,
    SLOT_TAIL,
    TRIGGER_ACCELERATION,
    TRIGGER_SCHEDULED,
    TRIGGER_TAIL,
    Campaign,
)


def target_cumulative_for_slice(campaign: Campaign, slice_no: int, *, is_tail: bool) -> int:
    """슬라이스 집행 후 도달해야 할 목표 누적 금액.

    tail 은 cap 전액이 목표, 예정 슬라이스 N 은 base*N. "목표 누적 - 실제 누적" 이
    예산이 되어 부분체결·패스가 자동으로 다음 슬라이스에 이월된다.
    """
    if is_tail:
        return campaign.cap_krw
    return round(campaign.base_per_slice_krw * slice_no)


def compute_slice_budget(
    campaign: Campaign,
    *,
    target_cumulative_krw: int,
    buying_power: int,
    today_filled_krw: int,
) -> int:
    """이 슬라이스에 배정할 예산(원). 모든 안전 클램프가 여기를 지난다.

    1) 목표 누적 - 실제 누적 (roll-over)
    2) 남은 cap (누적 cap 불변식)
    3) 주문가능금액 × clamp_pct (현금부족 거부 방지)
    4) 건별 cost cap
    5) 일별 cost cap - 오늘 이미 체결분
    """
    budget = target_cumulative_krw - campaign.cumulative_filled_krw
    budget = min(budget, campaign.remaining_cap_krw)
    budget = min(budget, int(buying_power * campaign.clamp_pct))
    budget = min(budget, campaign.per_order_cost_cap)
    budget = min(budget, campaign.per_day_cost_cap - today_filled_krw)
    return max(budget, 0)


def accel_should_fire(
    campaign: Campaign,
    *,
    price: int,
    open_price: int,
    change_pct: float,
    now: datetime,
) -> bool:
    """급락 가속 트리거 — 당일 시가대비 또는 전일종가대비 임계 이하.

    가속은 남은 예정 슬라이스가 있을 때만(tail 에는 적용 안 함), 쿨다운을 통과할 때만.
    open_price/price 가 0 이하(거래 전·오류)면 발사하지 않는다.
    """
    if campaign.slices_done >= campaign.total_slices:
        return False
    if campaign.last_accel_at is not None:
        elapsed = (now - campaign.last_accel_at).total_seconds()
        if elapsed < campaign.accel_cooldown_h * 3600:
            return False
    if open_price <= 0 or price <= 0:
        return False
    open_drop = price / open_price - 1.0
    prev_drop = change_pct / 100.0
    return (
        open_drop <= campaign.accel_open_pct / 100.0 or prev_drop <= campaign.accel_prev_pct / 100.0
    )


def next_scheduled_slot(campaign: Campaign) -> tuple[str, int, str] | None:
    """예정 집행(14:30)이 다룰 (slot, slice_no, trigger). 없으면 None.

    남은 예정 슬라이스가 있으면 sched, 다 끝났고 잔액이 남아 꼬리가 필요하면 tail.
    """
    if campaign.slices_done < campaign.total_slices:
        return (SLOT_SCHED, campaign.slices_done + 1, TRIGGER_SCHEDULED)
    if (
        campaign.slices_done == campaign.total_slices
        and not campaign.final_tail_done
        and campaign.remaining_cap_krw > 0
    ):
        return (SLOT_TAIL, campaign.total_slices + 1, TRIGGER_TAIL)
    return None


def accel_slot(campaign: Campaign) -> tuple[str, int, str] | None:
    """가속이 당겨올 (slot, slice_no, trigger). 다음 예정 슬라이스를 accel slot 으로."""
    if campaign.slices_done >= campaign.total_slices:
        return None
    return (SLOT_ACCEL, campaign.slices_done + 1, TRIGGER_ACCELERATION)


__all__ = [
    "accel_should_fire",
    "accel_slot",
    "compute_slice_budget",
    "next_scheduled_slot",
    "target_cumulative_for_slice",
]
