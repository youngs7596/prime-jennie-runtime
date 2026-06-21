"""DCA tick — 매 영업일 장중 매 분 도는 결정론 상태기계.

예정 집행(14:30)·급락 가속·VI 재시도를 한 경로로 합쳐 상호배제를 단순화한다. 한 tick
한 종목 최대 한 집행. 가속과 14:30 의 이중집행은 (1) 가드 우선순위 return, (2) "오늘
처리됨" 검사, (3) slices_done 카운터 세 겹으로 막는다.

핸들러는 절대 예외로 빠져나가지 않는다 — apscheduler 가 예외 시 30초 후 재호출하는데
같은 tick 두 번 집행을 막으려면 정상 리턴해야 한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time

import redis.asyncio as aioredis

from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.notifier import Notifier

from .executor import SliceExecutor
from .planning import (
    accel_should_fire,
    accel_slot,
    next_scheduled_slot,
    target_cumulative_for_slice,
)
from .repository import DcaRepository
from .state import (
    MIN_ORDER_KRW,
    TRIGGER_TAIL,
    Campaign,
    CampaignStatus,
)

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
# 캠페인의 execute_at_kst 를 못 읽을 때만 쓰는 기본 집행 시각. 14:00 은 2026-06-20
# 백테스트(.ai/analyses/2026-06-20-dca-entry-time.md) 결정 — 14:30 보다 두 종목 모두
# 일관되게 조금 쌌고, 종가/늦은 오후는 14:30 보다 안 좋았다.
_DEFAULT_SCHED_TIME = dt_time(14, 0)
_MARKET_OPEN = dt_time(9, 0)
_NEW_ORDER_CUTOFF = dt_time(15, 20)  # 이후엔 새 주문 시작 안 함


def _sched_time(campaign: Campaign) -> dt_time:
    """캠페인 집행 시각(execute_at_kst, 'HH:MM') 파싱. 불량값이면 기본값."""
    try:
        h, m = campaign.execute_at_kst.split(":")
        return dt_time(int(h), int(m))
    except (ValueError, AttributeError):
        return _DEFAULT_SCHED_TIME


async def run_dca_tick(
    *,
    repo: DcaRepository,
    kis: KisClient,
    redis_client: aioredis.Redis,
    now: datetime | None = None,
    notifier: object | None = None,
) -> None:
    """한 tick. 활성 캠페인을 순차 처리하며, 어떤 예외도 밖으로 던지지 않는다."""
    try:
        now = now or datetime.now(KST)
        snap = await SystemState(redis_client).snapshot()
        # STOP(긴급정지)만 전면 차단한다. PAUSE 는 통과 — DCA 는 운영자가 /dca arm 으로
        # 직접 지시한 매수라 승인 매수(approved_buy)와 같은 부류다. 시스템 PAUSE 는 무확인
        # 자동 진입만 막고 지시·승인 매수는 흘려보낸다(consumer.py 의 manual_buy 만 차단).
        # 사람-승인 매매 체제의 상시 PAUSE(exit_only_human_approved) 아래서도 DCA 는 돈다.
        if snap.stopped:
            logger.info("dca_tick skip: system stopped")
            return
        executor = SliceExecutor(repo, kis, notifier=notifier or Notifier(redis_client))
        for camp in await repo.fetch_active_campaigns():
            try:
                # crash 복구를 먼저 — 새 주문 전에 in-flight 정리. 이후 상태 재조회.
                await executor.reconcile_inflight(camp, now)
                fresh = await repo.fetch_campaign(camp.id)
                if fresh is None or fresh.status != CampaignStatus.ACTIVE.value:
                    continue
                await _process_campaign(repo, executor, kis, fresh, now, force_dry=snap.dryrun)
            except Exception:
                logger.exception("dca_tick campaign failed id=%s", camp.id)
    except Exception:
        logger.exception("dca_tick failed")


async def _process_campaign(
    repo: DcaRepository,
    executor: SliceExecutor,
    kis: KisClient,
    camp: Campaign,
    now: datetime,
    *,
    force_dry: bool,
) -> None:
    trade_date = now.date()

    # G0. 누적 cap 도달 → 종료
    if camp.remaining_cap_krw <= MIN_ORDER_KRW:
        await repo.set_status(camp.id, CampaignStatus.CAP_REACHED.value)
        return

    # G1. VI 재시도 due — 가장 시간민감하므로 가속·예정보다 먼저
    retry = await repo.fetch_due_retry(camp.id, now)
    if retry is not None:
        await executor.execute_slice(camp, retry, now=now, force_dry=force_dry)
        return

    # G2. 오늘 이미 처리됨(가속/14:30/꼬리/재시도대기) → 완료조건만 확인하고 끝
    if await repo.today_settled(camp.id, trade_date):
        await _maybe_complete(repo, camp)
        return

    # G3. 급락 가속 — 장중이면 다음 예정 슬라이스를 당겨 집행
    if _MARKET_OPEN <= now.time() <= _NEW_ORDER_CUTOFF and await _accel_fires(kis, camp, now):
        acc = accel_slot(camp)
        if acc is not None:
            slot, slice_no, trigger = acc
            await _fire(repo, executor, camp, now, slot, slice_no, trigger, force_dry=force_dry)
            return

    # G4. 예정 집행 시각(캠페인 execute_at_kst ~ 15:20)
    if _sched_time(camp) <= now.time() <= _NEW_ORDER_CUTOFF:
        slot_info = next_scheduled_slot(camp)
        if slot_info is None:
            await _maybe_complete(repo, camp)
            return
        slot, slice_no, trigger = slot_info
        await _fire(repo, executor, camp, now, slot, slice_no, trigger, force_dry=force_dry)
        return
    # else: 14:30 전 + 가속 없음 → no-op (다음 tick)


async def _accel_fires(kis: KisClient, camp: Campaign, now: datetime) -> bool:
    try:
        snap = await kis.get_snapshot(camp.ticker)
    except Exception:
        logger.exception("dca accel snapshot failed campaign=%s", camp.id)
        return False
    return accel_should_fire(
        camp,
        price=snap.price,
        open_price=snap.open_price,
        change_pct=snap.change_pct,
        now=now,
    )


async def _fire(
    repo: DcaRepository,
    executor: SliceExecutor,
    camp: Campaign,
    now: datetime,
    slot: str,
    slice_no: int,
    trigger: str,
    *,
    force_dry: bool,
) -> None:
    is_tail = trigger == TRIGGER_TAIL
    target = target_cumulative_for_slice(camp, slice_no, is_tail=is_tail)
    provisional = max(min(target - camp.cumulative_filled_krw, camp.remaining_cap_krw), 0)
    slice_key = f"{camp.id}:{now.date().isoformat()}:{slot}"
    row = await repo.acquire_slice(
        slice_key=slice_key,
        campaign_id=camp.id,
        ticker=camp.ticker,
        slice_no=slice_no,
        trigger_kind=trigger,
        trade_date=now.date(),
        budget_krw=provisional,
    )
    if row is None:
        return  # 이미 누가 잡았다(재배달/재시작) → 멱등 스킵
    await executor.execute_slice(camp, row, now=now, force_dry=force_dry)


async def _maybe_complete(repo: DcaRepository, camp: Campaign) -> None:
    """예정 슬라이스 + 꼬리까지 끝났으면 completed 로."""
    if camp.slices_done < camp.total_slices:
        return
    negligible = camp.final_tail_done or camp.remaining_cap_krw <= MIN_ORDER_KRW
    if negligible and camp.status != CampaignStatus.COMPLETED.value:
        await repo.set_status(camp.id, CampaignStatus.COMPLETED.value)


__all__ = ["run_dca_tick"]
