"""DCA tick + executor 전체 흐름 — 멱등성·이월·가속·VI재시도·cap불변식·복구.

now 를 명시 주입할 수 있어 freezegun 없이 시각 시나리오를 만든다. PG 대신 InMemoryDcaRepo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prime_jennie_runtime.kis_gateway.schemas import DailyExecution
from prime_jennie_runtime.user_directed_dca.presets import PRESETS, build_campaign_rows
from prime_jennie_runtime.user_directed_dca.state import SliceExecution
from prime_jennie_runtime.user_directed_dca.tick import run_dca_tick

from .fakes import FakeKis, FakeNotifier, InMemoryDcaRepo

KST = timezone(timedelta(hours=9))

# 2026 6~7월 영업일 (월~금).
BIZ_DAYS = [
    (2026, 6, 22),
    (2026, 6, 23),
    (2026, 6, 24),
    (2026, 6, 25),
    (2026, 6, 26),
    (2026, 6, 29),
    (2026, 6, 30),
    (2026, 7, 1),
    (2026, 7, 2),
    (2026, 7, 3),
    (2026, 7, 6),
    (2026, 7, 7),
    (2026, 7, 8),
    (2026, 7, 9),
    (2026, 7, 10),
]


def at(day_idx: int, hour: int, minute: int) -> datetime:
    y, m, d = BIZ_DAYS[day_idx]
    return datetime(y, m, d, hour, minute, tzinfo=KST)


async def seed(repo: InMemoryDcaRepo, preset_name: str, ticker: str = "005930") -> str:
    rows = build_campaign_rows(
        PRESETS[preset_name], arm_date=datetime(2026, 6, 20).date(), ticker=ticker
    )
    rows = [r for r in rows if r["ticker"] == ticker]
    await repo.insert_campaigns(rows)
    return rows[0]["id"]


@pytest.mark.asyncio
async def test_dryrun_records_without_orders(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "dryrun")

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))

    camp = repo.campaigns[cid]
    assert camp.slices_done == 1
    assert camp.cumulative_filled_krw > 0
    assert not kis.buy_calls  # dry-run → 실주문 없음

    # 같은 분 재실행 → 멱등 (오늘 이미 처리됨)
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 31))
    assert repo.campaigns[cid].slices_done == 1


@pytest.mark.asyncio
async def test_scheduled_time_honored(fake_redis):
    # 프리셋 집행시각은 14:00 — 13:55 엔 안 사고, 14:00 에 산다.
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 13, 55))
    assert repo.campaigns[cid].slices_done == 0
    assert not kis.buy_calls

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 0))
    assert repo.campaigns[cid].slices_done == 1


@pytest.mark.asyncio
async def test_live_full_fill(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))

    camp = repo.campaigns[cid]
    assert len(kis.buy_calls) == 1
    qty = kis.buy_calls[0].quantity
    assert kis.buy_calls[0].order_type == "market"
    assert camp.cumulative_filled_krw == qty * 80_000
    assert camp.slices_done == 1


@pytest.mark.asyncio
async def test_fill_emits_telegram_notification(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    notifier = FakeNotifier()
    await seed(repo, "production")

    await run_dca_tick(
        repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 0), notifier=notifier
    )
    assert any(
        getattr(n, "kind", None) == "alert" and "체결" in getattr(n, "title", "")
        for n in notifier.sent
    )


@pytest.mark.asyncio
async def test_idempotent_double_tick_same_minute(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))

    assert len(kis.buy_calls) == 1
    assert repo.campaigns[cid].slices_done == 1


@pytest.mark.asyncio
async def test_rollover_after_partial_fill(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")

    # 슬라이스 1: 부분체결(50주만)
    kis.fill_qty = 50
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))
    camp = repo.campaigns[cid]
    assert camp.cumulative_filled_krw == 50 * 80_000
    first_slice = next(s for s in repo.slices if s.slice_no == 1)
    assert first_slice.status == "partial"

    # 슬라이스 2: 전량체결 — 예산이 이월분만큼 커야 한다(intended_qty 가 첫 슬라이스보다 큼)
    kis.fill_qty = None
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(1, 14, 30))
    second_slice = next(s for s in repo.slices if s.slice_no == 2)
    assert second_slice.intended_qty > (first_slice.intended_qty or 0)
    assert camp.cumulative_filled_krw <= camp.cap_krw


@pytest.mark.asyncio
async def test_acceleration_pulls_forward_and_blocks_scheduled(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=76_000, open_price=80_000, change_pct=-1.0)  # 시가대비 -5%
    cid = await seed(repo, "production")

    # 오전 10:00 가속 발사
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 10, 0))
    camp = repo.campaigns[cid]
    assert camp.slices_done == 1
    assert camp.last_accel_at is not None
    accel_slice = next(s for s in repo.slices if s.trigger_kind == "acceleration")
    assert accel_slice.status == "filled"

    # 같은 날 14:30 → 오늘 이미 처리됨 → 추가 집행 없음
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))
    assert repo.campaigns[cid].slices_done == 1
    assert len(kis.buy_calls) == 1


@pytest.mark.asyncio
async def test_acceleration_cooldown_blocks_next_day(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=70_000, open_price=80_000, change_pct=-12.0)
    await seed(repo, "production")

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 10, 0))
    # 다음 날 09:30 (23.5h 후, 쿨다운 내) — 가속 안 됨. 14:30 예정만 가능.
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(1, 9, 30))
    accel_count = sum(1 for s in repo.slices if s.trigger_kind == "acceleration")
    assert accel_count == 1  # 둘째 날 가속 없음


@pytest.mark.asyncio
async def test_vi_reject_then_retry_succeeds(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")

    # 14:30 거부 → rejected_retry, 슬라이스 미소진
    kis.reject = True
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))
    camp = repo.campaigns[cid]
    assert camp.slices_done == 0
    assert camp.cumulative_filled_krw == 0
    s = repo.slices[0]
    assert s.status == "rejected_retry" and s.retry_at is not None

    # 재시도 아직 안 됨(14:33) → 오늘 처리됨으로 막힘
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 33))
    assert repo.campaigns[cid].slices_done == 0

    # 5분 경과(14:36) + 거부 해제 → 재시도 성공
    kis.reject = False
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 36))
    assert repo.campaigns[cid].slices_done == 1
    assert repo.campaigns[cid].cumulative_filled_krw > 0


@pytest.mark.asyncio
async def test_vi_reject_twice_passes_and_rolls(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")

    kis.reject = True
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))
    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 36))

    camp = repo.campaigns[cid]
    assert camp.slices_done == 1  # 패스로 소진
    assert camp.cumulative_filled_krw == 0  # 체결 없음 → 예산 이월
    s = repo.slices[0]
    assert s.status == "passed"


@pytest.mark.asyncio
async def test_stop_gate_blocks_everything(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")
    await fake_redis.set("control.state:stop", b"1")

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 30))
    assert repo.campaigns[cid].slices_done == 0
    assert not kis.buy_calls


@pytest.mark.asyncio
async def test_pause_does_not_block_dca(fake_redis):
    # PAUSE 는 무확인 자동 진입만 막는다. DCA 는 운영자가 직접 지시한 매수라 승인 매수처럼
    # PAUSE 를 통과한다 (상시 PAUSE = exit_only_human_approved 아래서도 집행돼야 함).
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")
    await fake_redis.set("control.state:pause", b"exit_only_human_approved_v1")

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 0))
    assert repo.campaigns[cid].slices_done == 1
    assert len(kis.buy_calls) == 1


@pytest.mark.asyncio
async def test_cap_invariant_full_run_completes(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "dryrun")  # 실주문 없이 전 일정 검증

    for i in range(len(BIZ_DAYS)):
        await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(i, 14, 30))
        camp = repo.campaigns[cid]
        # 불변식: 누적은 절대 cap 을 넘지 않는다
        assert camp.cumulative_filled_krw <= camp.cap_krw

    camp = repo.campaigns[cid]
    assert camp.status in ("completed", "cap_reached")
    assert camp.slices_done >= 10


@pytest.mark.asyncio
async def test_reconcile_resumes_submitted_with_order_no(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    kis.fill_qty = 100
    cid = await seed(repo, "production")
    # crash 직전: 주문은 나갔고(order_no 있음) terminal 기록 전 submitted 잔류
    repo.slices.append(
        SliceExecution(
            id=999,
            slice_key=f"{cid}:2026-06-22:sched",
            campaign_id=cid,
            ticker="005930",
            slice_no=1,
            trigger_kind="scheduled",
            trade_date=at(0, 14, 30).date(),
            status="submitted",
            budget_krw=11_800_000,
            intended_qty=100,
            order_no="BUY000999",
            filled_qty=0,
            filled_krw=0,
            avg_price=0.0,
            retry_at=None,
            retry_count=0,
            reject_reason=None,
        )
    )
    repo._slice_id = 999

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 40))
    s = next(s for s in repo.slices if s.id == 999)
    assert s.status == "filled"
    assert repo.campaigns[cid].cumulative_filled_krw == 100 * 80_000


@pytest.mark.asyncio
async def test_reconcile_adopts_ledger_order_when_order_no_lost(fake_redis):
    repo = InMemoryDcaRepo()
    kis = FakeKis(price=80_000)
    cid = await seed(repo, "production")
    # crash: buy() 직후 order_no 영속 전 — submitted + order_no None
    repo.slices.append(
        SliceExecution(
            id=999,
            slice_key=f"{cid}:2026-06-22:sched",
            campaign_id=cid,
            ticker="005930",
            slice_no=1,
            trigger_kind="scheduled",
            trade_date=at(0, 14, 30).date(),
            status="submitted",
            budget_krw=11_800_000,
            intended_qty=100,
            order_no=None,
            filled_qty=0,
            filled_krw=0,
            avg_price=0.0,
            retry_at=None,
            retry_count=0,
            reject_reason=None,
        )
    )
    repo._slice_id = 999
    kis.daily_execs = [
        DailyExecution(
            order_date="20260622",
            order_time="143005",
            stock_code="005930",
            stock_name="삼성전자",
            side="buy",
            order_qty=100,
            filled_qty=100,
            avg_price=80_000.0,
            filled_amount=8_000_000,
            order_no="LEDGER0001",
            cancelled=False,
        )
    ]

    await run_dca_tick(repo=repo, kis=kis, redis_client=fake_redis, now=at(0, 14, 40))
    s = next(s for s in repo.slices if s.id == 999)
    assert s.status == "filled"
    assert s.order_no == "LEDGER0001"
    assert repo.campaigns[cid].cumulative_filled_krw == 8_000_000
