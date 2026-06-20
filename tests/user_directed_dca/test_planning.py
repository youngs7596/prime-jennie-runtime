"""DCA 순수 결정 로직 — 예산 산식·가속 트리거·슬롯 선택."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prime_jennie_runtime.user_directed_dca.planning import (
    accel_should_fire,
    accel_slot,
    compute_slice_budget,
    next_scheduled_slot,
    target_cumulative_for_slice,
)
from prime_jennie_runtime.user_directed_dca.state import (
    SLOT_SCHED,
    SLOT_TAIL,
    Campaign,
)

KST = timezone(timedelta(hours=9))


def make_campaign(**over) -> Campaign:
    base = dict(
        id="dca:005930:20260622",
        ticker="005930",
        status="active",
        preset="production",
        cap_krw=118_000_000,
        total_slices=10,
        execute_at_kst="14:30",
        accel_open_pct=-4.0,
        accel_prev_pct=-5.0,
        accel_cooldown_h=24,
        clamp_pct=0.995,
        per_order_cost_cap=40_000_000,
        per_day_cost_cap=60_000_000,
        dry_run=False,
        cumulative_filled_krw=0,
        cumulative_filled_qty=0,
        slices_done=0,
        last_accel_at=None,
        final_tail_done=False,
    )
    base.update(over)
    return Campaign(**base)


def test_budget_basic_first_slice():
    c = make_campaign()
    target = target_cumulative_for_slice(c, 1, is_tail=False)
    assert target == 11_800_000
    budget = compute_slice_budget(
        c, target_cumulative_krw=target, buying_power=300_000_000, today_filled_krw=0
    )
    assert budget == 11_800_000


def test_budget_rolls_over_deficit():
    # 슬라이스 1 에서 900만만 체결 → 슬라이스 2 예산이 이월분만큼 늘어난다.
    c = make_campaign(cumulative_filled_krw=9_000_000, slices_done=1)
    target = target_cumulative_for_slice(c, 2, is_tail=False)  # round(11.8M*2)=23.6M
    budget = compute_slice_budget(
        c, target_cumulative_krw=target, buying_power=300_000_000, today_filled_krw=0
    )
    assert budget == 23_600_000 - 9_000_000  # 14.6M (이월 2.8M 흡수)


def test_budget_clamped_by_remaining_cap():
    c = make_campaign(cumulative_filled_krw=117_000_000, slices_done=9)
    target = target_cumulative_for_slice(c, 10, is_tail=False)  # 118M
    budget = compute_slice_budget(
        c, target_cumulative_krw=target, buying_power=300_000_000, today_filled_krw=0
    )
    assert budget == 1_000_000  # cap-cumulative
    assert budget <= c.remaining_cap_krw


def test_budget_clamped_by_buying_power():
    c = make_campaign()
    budget = compute_slice_budget(
        c, target_cumulative_krw=11_800_000, buying_power=5_000_000, today_filled_krw=0
    )
    assert budget == int(5_000_000 * 0.995)


def test_budget_clamped_by_per_day():
    c = make_campaign()
    budget = compute_slice_budget(
        c,
        target_cumulative_krw=11_800_000,
        buying_power=300_000_000,
        today_filled_krw=55_000_000,  # per_day_cost_cap=60M → 5M 남음
    )
    assert budget == 5_000_000


def test_budget_never_negative():
    c = make_campaign(cumulative_filled_krw=118_000_000, slices_done=10)
    budget = compute_slice_budget(
        c, target_cumulative_krw=118_000_000, buying_power=300_000_000, today_filled_krw=0
    )
    assert budget == 0


def test_accel_fires_on_open_drop():
    c = make_campaign()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=KST)
    # 시가 80000 대비 -4% = 76800 이하
    assert accel_should_fire(c, price=76_800, open_price=80_000, change_pct=-1.0, now=now)
    assert not accel_should_fire(c, price=77_000, open_price=80_000, change_pct=-1.0, now=now)


def test_accel_fires_on_prev_close_drop():
    c = make_campaign()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=KST)
    assert accel_should_fire(c, price=80_000, open_price=80_000, change_pct=-5.0, now=now)
    assert not accel_should_fire(c, price=80_000, open_price=80_000, change_pct=-4.5, now=now)


def test_accel_blocked_by_cooldown():
    last = datetime(2026, 6, 22, 10, 0, tzinfo=KST)
    c = make_campaign(last_accel_at=last)
    within = datetime(2026, 6, 23, 9, 0, tzinfo=KST)  # 23시간 후
    assert not accel_should_fire(c, price=70_000, open_price=80_000, change_pct=-10.0, now=within)
    after = datetime(2026, 6, 23, 11, 0, tzinfo=KST)  # 25시간 후
    assert accel_should_fire(c, price=70_000, open_price=80_000, change_pct=-10.0, now=after)


def test_accel_blocked_when_open_price_zero():
    c = make_campaign()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=KST)
    assert not accel_should_fire(c, price=0, open_price=0, change_pct=-10.0, now=now)


def test_accel_blocked_when_all_slices_done():
    c = make_campaign(slices_done=10)
    now = datetime(2026, 6, 22, 10, 0, tzinfo=KST)
    assert not accel_should_fire(c, price=70_000, open_price=80_000, change_pct=-10.0, now=now)
    assert accel_slot(c) is None


def test_next_slot_scheduled_then_tail_then_none():
    c = make_campaign(slices_done=3)
    assert next_scheduled_slot(c) == (SLOT_SCHED, 4, "scheduled")

    # 10슬라이스 다 했고 잔액 있으면 tail
    c2 = make_campaign(slices_done=10, cumulative_filled_krw=117_000_000)
    slot, no, trig = next_scheduled_slot(c2)
    assert slot == SLOT_TAIL and no == 11 and trig == "tail"

    # 다 했고 잔액 없으면 None
    c3 = make_campaign(slices_done=10, cumulative_filled_krw=118_000_000)
    assert next_scheduled_slot(c3) is None

    # tail 까지 끝났으면 None
    c4 = make_campaign(slices_done=10, cumulative_filled_krw=117_000_000, final_tail_done=True)
    assert next_scheduled_slot(c4) is None
