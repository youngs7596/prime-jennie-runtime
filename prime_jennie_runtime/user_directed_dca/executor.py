"""슬라이스 집행기 — 한 슬라이스 시도를 cap강제·부분체결·VI재시도·crash복구까지.

주문은 일반 시장가(ORD_DVSN "01"). 부분체결 시 잔량을 취소하고 재조회로 terminal
체결량을 확정하는 entry_executor.py(2026-05-21 사고 학습) 패턴을 그대로 따른다.
누적 가산은 finalize_slice 한 트랜잭션에서만 일어나 cap 불변식을 유지한다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Protocol

from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.schemas import GenericAlertNotification
from prime_jennie_runtime.kis_gateway.schemas import OrderRequest, OrderStatusResult

from .planning import compute_slice_budget, target_cumulative_for_slice
from .repository import DcaRepository
from .state import (
    MIN_ORDER_KRW,
    TRIGGER_ACCELERATION,
    TRIGGER_TAIL,
    Campaign,
    CampaignStatus,
    SliceExecution,
    SliceStatus,
)

logger = logging.getLogger(__name__)

# VI 재시도(5분 후)를 예약하기엔 너무 늦은 시각 — 이후엔 바로 패스(장 마감 임박).
_VI_RETRY_CUTOFF = dt_time(15, 10)
_VI_RETRY_DELAY_SEC = 300
_CONFIRM_RETRIES = 5
_CONFIRM_INTERVAL = 3.0


class _Emitter(Protocol):
    async def emit(self, notification: object) -> object: ...


class SliceExecutor:
    def __init__(
        self, repo: DcaRepository, kis: KisClient, *, notifier: _Emitter | None = None
    ) -> None:
        self._repo = repo
        self._kis = kis
        self._notifier = notifier

    async def _notify(self, *, title: str, body: str, severity: str, ts: datetime | None) -> None:
        """텔레그램(v3:notifications) 알림 — 실패해도 매매를 막지 않는다(best-effort)."""
        if self._notifier is None:
            return
        try:
            await self._notifier.emit(
                GenericAlertNotification(
                    kind="alert",
                    severity=severity,  # type: ignore[arg-type]
                    title=title,
                    body=body,
                    ts=ts or datetime.now(UTC),
                )
            )
        except Exception:
            logger.exception("dca notify failed")

    # ----- 공개 진입점 -----

    async def execute_slice(
        self,
        campaign: Campaign,
        slice_row: SliceExecution,
        *,
        now: datetime,
        force_dry: bool,
    ) -> None:
        """선점된 슬라이스(pending) 또는 재시도(rejected_retry) 한 건을 집행."""
        is_tail = slice_row.trigger_kind == TRIGGER_TAIL

        # 1. cap 재확인 (집행 직전 마지막 방어)
        if campaign.remaining_cap_krw <= MIN_ORDER_KRW:
            await self._repo.finalize_slice(
                slice_id=slice_row.id,
                campaign_id=campaign.id,
                status=SliceStatus.SKIPPED_CAP.value,
                filled_qty=0,
                filled_krw=0,
                avg_price=0.0,
                advance_slices_done=True,
                new_campaign_status=CampaignStatus.CAP_REACHED.value,
            )
            return

        # 2. 목표 누적 → 예산 (roll-over·cap·현금·건별·일별 클램프)
        target = target_cumulative_for_slice(campaign, slice_row.slice_no, is_tail=is_tail)
        try:
            buying_power = await self._kis.get_cash()
        except Exception:
            # 일시적 조회 실패 — 상태 그대로 두면 다음 tick 이 재시도(주문 전이라 안전).
            logger.exception("dca get_cash failed campaign=%s", campaign.id)
            return
        today_filled = await self._repo.today_filled_krw(campaign.id, slice_row.trade_date)
        budget = compute_slice_budget(
            campaign,
            target_cumulative_krw=target,
            buying_power=buying_power,
            today_filled_krw=today_filled,
        )
        if budget <= MIN_ORDER_KRW:
            await self._pass(campaign, slice_row, reason="budget_too_small", is_tail=is_tail)
            return

        # 3. 수량 (시장가 → 보수적 내림)
        try:
            snap = await self._kis.get_snapshot(campaign.ticker)
        except Exception:
            logger.exception("dca get_snapshot failed campaign=%s", campaign.id)
            return
        price = snap.price
        if price <= 0:
            await self._pass(campaign, slice_row, reason="no_price", is_tail=is_tail)
            return
        qty = budget // price
        if qty <= 0:
            await self._pass(campaign, slice_row, reason="qty_zero", is_tail=is_tail)
            return
        await self._repo.update_slice(slice_row.id, intended_qty=qty)

        # 4. dry-run — 실주문 없이 시뮬 체결을 기록(이월·cap 로직 검증)
        if force_dry or campaign.dry_run:
            await self._settle(
                campaign,
                slice_row,
                now=now,
                status=SliceStatus.FILLED.value,
                filled_qty=qty,
                filled_krw=qty * price,
                avg_price=float(price),
                is_tail=is_tail,
                dry=True,
            )
            return

        # 5. submitted 영속 후 주문 (crash 복구 표적)
        await self._repo.update_slice(slice_row.id, status=SliceStatus.SUBMITTED.value)
        logger.info(
            "dca order submit campaign=%s ticker=%s qty=%d budget=%d trigger=%s",
            campaign.id,
            campaign.ticker,
            qty,
            budget,
            slice_row.trigger_kind,
        )
        try:
            result = await self._kis.buy(
                OrderRequest(stock_code=campaign.ticker, quantity=qty, order_type="market")
            )
        except Exception as exc:
            logger.exception("dca buy failed campaign=%s", campaign.id)
            await self._vi_retry_or_pass(campaign, slice_row, now, reason=f"buy_error:{exc!r}")
            return
        if not result.success or not result.order_no:
            await self._vi_retry_or_pass(
                campaign, slice_row, now, reason=result.message or "rejected"
            )
            return
        await self._repo.update_slice(slice_row.id, order_no=result.order_no)

        # 6. confirm → 부분체결 잔량취소 → terminal 확정
        status = await self._confirm_terminal(result.order_no, qty)
        if status is None or status.filled_qty <= 0:
            await self._vi_retry_or_pass(campaign, slice_row, now, reason="not_filled")
            return
        filled_qty = status.filled_qty
        avg = status.avg_price
        await self._settle(
            campaign,
            slice_row,
            now=now,
            status=(SliceStatus.FILLED.value if filled_qty >= qty else SliceStatus.PARTIAL.value),
            filled_qty=filled_qty,
            filled_krw=round(filled_qty * avg),
            avg_price=avg,
            is_tail=is_tail,
        )

    async def reconcile_inflight(self, campaign: Campaign, now: datetime) -> None:
        """매 tick 시작 시 in-flight 정리 — 새 주문 전에.

        pending(주문 전)은 삭제, submitted(주문 후 crash)는 order_no 로 재confirm 하거나
        KIS 원장과 대조해 멱등 복구. 이중 매수를 만들지 않는다.
        """
        await self._repo.delete_stale_pending(campaign.id)
        for s in await self._repo.fetch_submitted(campaign.id):
            try:
                if s.order_no:
                    await self._resume_submitted(campaign, s)
                else:
                    await self._reconcile_via_ledger(campaign, s, now)
            except Exception:
                logger.exception("dca reconcile failed campaign=%s slice=%s", campaign.id, s.id)

    # ----- 내부 -----

    async def _confirm_terminal(self, order_no: str, qty: int) -> OrderStatusResult | None:
        status = await self._kis.confirm_order(
            order_no, max_retries=_CONFIRM_RETRIES, interval=_CONFIRM_INTERVAL
        )
        if status is None or status.filled_qty <= 0:
            await self._safe_cancel(order_no)
            final = await self._kis.check_order_status(order_no)
            return final if final and final.filled_qty > 0 else None
        if status.filled_qty < qty:
            await self._safe_cancel(order_no)
            final = await self._kis.check_order_status(order_no)
            if final and final.filled_qty >= status.filled_qty:
                return final
        return status

    async def _safe_cancel(self, order_no: str) -> None:
        try:
            await self._kis.cancel_order(order_no)
        except Exception:
            logger.exception("dca cancel_order failed order=%s", order_no)

    async def _resume_submitted(self, campaign: Campaign, s: SliceExecution) -> None:
        status = await self._confirm_terminal(s.order_no or "", s.intended_qty or 0)
        if status is None or status.filled_qty <= 0:
            await self._pass(campaign, s, reason="reconcile_not_filled", is_tail=_is_tail(s))
            return
        await self._settle(
            campaign,
            s,
            now=None,
            status=(
                SliceStatus.FILLED.value
                if (s.intended_qty and status.filled_qty >= s.intended_qty)
                else SliceStatus.PARTIAL.value
            ),
            filled_qty=status.filled_qty,
            filled_krw=round(status.filled_qty * status.avg_price),
            avg_price=status.avg_price,
            is_tail=_is_tail(s),
        )

    async def _reconcile_via_ledger(
        self, campaign: Campaign, s: SliceExecution, now: datetime
    ) -> None:
        day = s.trade_date.strftime("%Y%m%d")
        try:
            execs = await self._kis.get_daily_executions(
                start_date=day, end_date=day, stock_code=campaign.ticker
            )
        except Exception:
            logger.exception("dca ledger reconcile fetch failed campaign=%s", campaign.id)
            return  # submitted 로 남겨 다음 tick 재시도
        recorded = await self._repo.recorded_order_nos(campaign.id, s.trade_date)
        candidates = [
            e
            for e in execs
            if e.side == "buy"
            and not e.cancelled
            and e.filled_qty > 0
            and e.order_no not in recorded
        ]
        if not candidates:
            logger.warning(
                "dca reconcile: no ledger order for submitted slice=%s — 패스 처리", s.id
            )
            await self._pass(campaign, s, reason="reconcile_no_order", is_tail=_is_tail(s))
        elif len(candidates) == 1:
            e = candidates[0]
            await self._repo.update_slice(s.id, order_no=e.order_no)
            logger.warning("dca reconcile: 원장 주문 %s 채택 slice=%s", e.order_no, s.id)
            await self._settle(
                campaign,
                s,
                now=now,
                status=(
                    SliceStatus.FILLED.value
                    if (s.intended_qty and e.filled_qty >= s.intended_qty)
                    else SliceStatus.PARTIAL.value
                ),
                filled_qty=e.filled_qty,
                filled_krw=e.filled_amount or round(e.filled_qty * e.avg_price),
                avg_price=e.avg_price,
                is_tail=_is_tail(s),
            )
        else:
            logger.error(
                "dca reconcile ambiguous: %d 미기록 매수 원장 — 캠페인 %s 중단(수동 확인)",
                len(candidates),
                campaign.id,
            )
            await self._repo.set_status(campaign.id, CampaignStatus.HALTED.value)

    async def _settle(
        self,
        campaign: Campaign,
        slice_row: SliceExecution,
        *,
        now: datetime | None,
        status: str,
        filled_qty: int,
        filled_krw: int,
        avg_price: float,
        is_tail: bool,
        dry: bool = False,
    ) -> None:
        new_cumulative = campaign.cumulative_filled_krw + filled_krw
        cap_reached = new_cumulative >= campaign.cap_krw
        new_status: str | None = None
        if cap_reached:
            new_status = CampaignStatus.CAP_REACHED.value
        elif is_tail:
            new_status = CampaignStatus.COMPLETED.value
        set_last_accel = (
            now if (now is not None and slice_row.trigger_kind == TRIGGER_ACCELERATION) else None
        )
        await self._repo.finalize_slice(
            slice_id=slice_row.id,
            campaign_id=campaign.id,
            status=status,
            filled_qty=filled_qty,
            filled_krw=filled_krw,
            avg_price=avg_price,
            advance_slices_done=True,
            set_last_accel=set_last_accel,
            set_final_tail_done=is_tail,
            new_campaign_status=new_status,
        )
        prefix = "(모의) " if dry else ""
        word = "부분체결" if status == SliceStatus.PARTIAL.value else "체결"
        await self._notify(
            title=f"{prefix}DCA {word}",
            body=(
                f"{campaign.ticker} {filled_qty}주 @{round(avg_price):,}원\n"
                f"슬라이스 {slice_row.slice_no}/{campaign.total_slices} · "
                f"누적 {new_cumulative:,}/{campaign.cap_krw:,}원"
            ),
            severity="info",
            ts=now,
        )
        if new_status is not None:
            await self._notify(
                title="DCA 캠페인 종료",
                body=f"{campaign.ticker} — {new_status} · 누적 {new_cumulative:,}원",
                severity="info",
                ts=now,
            )

    async def _pass(
        self, campaign: Campaign, slice_row: SliceExecution, *, reason: str, is_tail: bool
    ) -> None:
        """슬라이스 패스 — 체결 0, 슬라이스 소진, 예산은 다음으로 이월(누적 불변)."""
        await self._repo.update_slice(slice_row.id, reject_reason=reason)
        await self._repo.finalize_slice(
            slice_id=slice_row.id,
            campaign_id=campaign.id,
            status=SliceStatus.PASSED.value,
            filled_qty=0,
            filled_krw=0,
            avg_price=0.0,
            advance_slices_done=True,
            set_final_tail_done=is_tail,
            new_campaign_status=CampaignStatus.COMPLETED.value if is_tail else None,
        )
        await self._notify(
            title="DCA 슬라이스 패스",
            body=f"{campaign.ticker} — {reason} · 예산 다음 슬라이스로 이월",
            severity="warning",
            ts=None,
        )

    async def _vi_retry_or_pass(
        self, campaign: Campaign, slice_row: SliceExecution, now: datetime, *, reason: str
    ) -> None:
        if slice_row.retry_count == 0 and now.time() < _VI_RETRY_CUTOFF:
            retry_at = now + timedelta(seconds=_VI_RETRY_DELAY_SEC)
            await self._repo.update_slice(
                slice_row.id,
                status=SliceStatus.REJECTED_RETRY.value,
                retry_at=retry_at,
                retry_count=1,
                reject_reason=reason,
            )
            logger.warning(
                "dca order rejected campaign=%s reason=%s — 5분 후 1회 재시도", campaign.id, reason
            )
            await self._notify(
                title="DCA 주문 거부",
                body=f"{campaign.ticker} — {reason} · 5분 후 1회 재시도",
                severity="warning",
                ts=now,
            )
        else:
            logger.warning(
                "dca order rejected (재시도 소진/마감임박) campaign=%s reason=%s — 패스",
                campaign.id,
                reason,
            )
            await self._pass(
                campaign, slice_row, reason=f"{reason}|final", is_tail=_is_tail(slice_row)
            )


def _is_tail(s: SliceExecution) -> bool:
    return s.trigger_kind == TRIGGER_TAIL


__all__ = ["SliceExecutor"]
