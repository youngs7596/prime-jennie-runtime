"""DCA API — 사용자 지정 종목 buy-only 시차 분할매수 진행률.

`user_directed_dca` 모듈이 매 영업일 정해진 시각 시장가로 분할매수하는 캠페인의
진행 상황을 read-only 로 노출한다. 데이터 출처는 dca_* 테이블(migration 026):
- `dca_campaigns` — 종목별 캠페인 1행 (상한·슬라이스·누적 체결).
- `dca_slice_executions` — 슬라이스 시도 1건 1행 (체결·패스·거부 이력).

DCA 보유분은 v3 추적(시트·트래커)에 안 들어가 포트폴리오 화면엔 보유로만 뜨고
진행률(슬라이스 n/총, 누적/상한, 평균단가)은 여기서만 본다.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_session

router = APIRouter(prefix="/dca", tags=["dca"])

logger = logging.getLogger(__name__)

# 캠페인당 카드에 실어 보낼 최근 슬라이스 수.
_RECENT_SLICE_LIMIT = 12


class DcaSlice(BaseModel):
    trade_date: date
    slice_no: int
    trigger_kind: str  # scheduled | acceleration | tail
    status: str  # filled | partial | passed | rejected_retry | skipped_cap ...
    filled_qty: int
    filled_krw: int
    avg_price: float
    reject_reason: str | None = None


class DcaCampaign(BaseModel):
    id: str
    ticker: str
    stock_name: str | None = None
    status: str  # active | paused | completed | cap_reached | halted
    preset: str
    cap_krw: int
    total_slices: int
    slices_done: int
    cumulative_filled_krw: int
    cumulative_filled_qty: int
    avg_price: float | None  # 누적 평균 진입가 (체결 0이면 None)
    progress_pct: float  # 누적 체결액 / 상한 × 100
    execute_at_kst: str
    dry_run: bool
    recent_slices: list[DcaSlice]


class DcaState(BaseModel):
    campaigns: list[DcaCampaign]
    timestamp: datetime


@router.get("/campaigns")
async def get_campaigns(
    session: AsyncSession = Depends(get_session),
) -> DcaState:
    """전체 DCA 캠페인 + 캠페인별 최근 슬라이스. active·paused 를 먼저 정렬."""
    camp_rows = (
        (
            await session.execute(
                text(
                    "SELECT c.id, c.ticker, sm.stock_name, c.status, c.preset, c.cap_krw, "
                    "c.total_slices, c.slices_done, c.cumulative_filled_krw, "
                    "c.cumulative_filled_qty, c.execute_at_kst, c.dry_run "
                    "FROM dca_campaigns c "
                    "LEFT JOIN stock_masters sm ON sm.stock_code = c.ticker "
                    "ORDER BY CASE WHEN c.status IN ('active','paused') THEN 0 ELSE 1 END, c.id"
                )
            )
        )
        .mappings()
        .all()
    )

    slice_rows = (
        (
            await session.execute(
                text(
                    "SELECT campaign_id, trade_date, slice_no, trigger_kind, status, "
                    "filled_qty, filled_krw, avg_price, reject_reason "
                    "FROM dca_slice_executions "
                    "ORDER BY trade_date DESC, id DESC"
                )
            )
        )
        .mappings()
        .all()
    )

    slices_by_campaign: dict[str, list[DcaSlice]] = {}
    for s in slice_rows:
        bucket = slices_by_campaign.setdefault(s["campaign_id"], [])
        if len(bucket) >= _RECENT_SLICE_LIMIT:
            continue
        bucket.append(
            DcaSlice(
                trade_date=s["trade_date"],
                slice_no=s["slice_no"],
                trigger_kind=s["trigger_kind"],
                status=s["status"],
                filled_qty=int(s["filled_qty"]),
                filled_krw=int(s["filled_krw"]),
                avg_price=float(s["avg_price"]),
                reject_reason=s["reject_reason"],
            )
        )

    campaigns: list[DcaCampaign] = []
    for c in camp_rows:
        filled_krw = int(c["cumulative_filled_krw"])
        filled_qty = int(c["cumulative_filled_qty"])
        cap = int(c["cap_krw"])
        campaigns.append(
            DcaCampaign(
                id=c["id"],
                ticker=c["ticker"],
                stock_name=c["stock_name"],
                status=c["status"],
                preset=c["preset"],
                cap_krw=cap,
                total_slices=int(c["total_slices"]),
                slices_done=int(c["slices_done"]),
                cumulative_filled_krw=filled_krw,
                cumulative_filled_qty=filled_qty,
                avg_price=(filled_krw / filled_qty) if filled_qty > 0 else None,
                progress_pct=round(filled_krw / cap * 100, 1) if cap > 0 else 0.0,
                execute_at_kst=c["execute_at_kst"],
                dry_run=bool(c["dry_run"]),
                recent_slices=slices_by_campaign.get(c["id"], []),
            )
        )

    return DcaState(campaigns=campaigns, timestamp=datetime.now(UTC))
