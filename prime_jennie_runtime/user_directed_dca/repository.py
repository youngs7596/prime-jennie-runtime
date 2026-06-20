"""DCA 영속 계층 — asyncpg 로 dca_campaigns / dca_slice_executions 접근.

모든 런타임 상태가 PG 에 있어 컨테이너 재기동 후 이어갈 수 있다(Redis 미사용).
멱등성의 핵심은 acquire_slice 의 ON CONFLICT(slice_key) DO NOTHING RETURNING —
같은 슬라이스를 두 번 잡지 못한다. finalize_slice 는 슬라이스 terminal 전이와 누적
가산을 한 트랜잭션으로 묶어 cap 불변식을 깨지 않는다.

tick(executor) 은 이 클래스의 메서드만 부른다. 테스트는 같은 시그니처의 in-memory
fake 로 대체한다(덕 타이핑).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import asyncpg

from .state import Campaign, SliceExecution, SliceStatus

_CAMPAIGN_COLUMNS = (
    "id, ticker, status, preset, cap_krw, total_slices, execute_at_kst, "
    "accel_open_pct, accel_prev_pct, accel_cooldown_h, clamp_pct, "
    "per_order_cost_cap, per_day_cost_cap, dry_run, "
    "cumulative_filled_krw, cumulative_filled_qty, slices_done, "
    "last_accel_at, final_tail_done"
)

_SLICE_COLUMNS = (
    "id, slice_key, campaign_id, ticker, slice_no, trigger_kind, trade_date, status, "
    "budget_krw, intended_qty, order_no, filled_qty, filled_krw, avg_price, "
    "retry_at, retry_count, reject_reason"
)

_SLICE_UPDATABLE = frozenset(
    {
        "status",
        "intended_qty",
        "order_no",
        "filled_qty",
        "filled_krw",
        "avg_price",
        "retry_at",
        "retry_count",
        "reject_reason",
    }
)


def _campaign(row: asyncpg.Record) -> Campaign:
    return Campaign(
        id=row["id"],
        ticker=row["ticker"],
        status=row["status"],
        preset=row["preset"],
        cap_krw=int(row["cap_krw"]),
        total_slices=int(row["total_slices"]),
        execute_at_kst=row["execute_at_kst"],
        accel_open_pct=float(row["accel_open_pct"]),
        accel_prev_pct=float(row["accel_prev_pct"]),
        accel_cooldown_h=int(row["accel_cooldown_h"]),
        clamp_pct=float(row["clamp_pct"]),
        per_order_cost_cap=int(row["per_order_cost_cap"]),
        per_day_cost_cap=int(row["per_day_cost_cap"]),
        dry_run=bool(row["dry_run"]),
        cumulative_filled_krw=int(row["cumulative_filled_krw"]),
        cumulative_filled_qty=int(row["cumulative_filled_qty"]),
        slices_done=int(row["slices_done"]),
        last_accel_at=row["last_accel_at"],
        final_tail_done=bool(row["final_tail_done"]),
    )


def _slice(row: asyncpg.Record) -> SliceExecution:
    return SliceExecution(
        id=int(row["id"]),
        slice_key=row["slice_key"],
        campaign_id=row["campaign_id"],
        ticker=row["ticker"],
        slice_no=int(row["slice_no"]),
        trigger_kind=row["trigger_kind"],
        trade_date=row["trade_date"],
        status=row["status"],
        budget_krw=int(row["budget_krw"]),
        intended_qty=row["intended_qty"],
        order_no=row["order_no"],
        filled_qty=int(row["filled_qty"]),
        filled_krw=int(row["filled_krw"]),
        avg_price=float(row["avg_price"]),
        retry_at=row["retry_at"],
        retry_count=int(row["retry_count"]),
        reject_reason=row["reject_reason"],
    )


class DcaRepository:
    """asyncpg pool 위의 DCA 영속 메서드."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ----- 캠페인 -----

    async def insert_campaigns(self, rows: list[dict[str, Any]]) -> int:
        """무장된 캠페인들을 멱등 INSERT. 새로 삽입된 개수 반환."""
        inserted = 0
        async with self._pool.acquire() as conn:
            for r in rows:
                result = await conn.execute(
                    "INSERT INTO dca_campaigns "
                    "(id, ticker, status, preset, cap_krw, total_slices, execute_at_kst, "
                    " accel_open_pct, accel_prev_pct, accel_cooldown_h, clamp_pct, "
                    " per_order_cost_cap, per_day_cost_cap, dry_run) "
                    "VALUES ($1,$2,'active',$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) "
                    "ON CONFLICT (id) DO NOTHING",
                    r["id"],
                    r["ticker"],
                    r["preset"],
                    r["cap_krw"],
                    r["total_slices"],
                    r["execute_at_kst"],
                    r["accel_open_pct"],
                    r["accel_prev_pct"],
                    r["accel_cooldown_h"],
                    r["clamp_pct"],
                    r["per_order_cost_cap"],
                    r["per_day_cost_cap"],
                    r["dry_run"],
                )
                if result.endswith(" 1"):
                    inserted += 1
        return inserted

    async def fetch_active_campaigns(self) -> list[Campaign]:
        rows = await self._pool.fetch(
            f"SELECT {_CAMPAIGN_COLUMNS} FROM dca_campaigns WHERE status='active' ORDER BY id"
        )
        return [_campaign(r) for r in rows]

    async def fetch_campaigns(self, statuses: list[str] | None = None) -> list[Campaign]:
        if statuses:
            rows = await self._pool.fetch(
                f"SELECT {_CAMPAIGN_COLUMNS} FROM dca_campaigns "
                "WHERE status = ANY($1::text[]) ORDER BY id",
                statuses,
            )
        else:
            rows = await self._pool.fetch(
                f"SELECT {_CAMPAIGN_COLUMNS} FROM dca_campaigns ORDER BY id"
            )
        return [_campaign(r) for r in rows]

    async def fetch_campaign(self, campaign_id: str) -> Campaign | None:
        row = await self._pool.fetchrow(
            f"SELECT {_CAMPAIGN_COLUMNS} FROM dca_campaigns WHERE id=$1", campaign_id
        )
        return _campaign(row) if row else None

    async def count_active_for_ticker(self, ticker: str) -> int:
        return int(
            await self._pool.fetchval(
                "SELECT COUNT(*) FROM dca_campaigns WHERE ticker=$1 AND status='active'",
                ticker,
            )
        )

    async def set_status(self, campaign_id: str, status: str) -> None:
        await self._pool.execute(
            "UPDATE dca_campaigns SET status=$2, updated_at=NOW() WHERE id=$1",
            campaign_id,
            status,
        )

    # ----- 슬라이스 조회 -----

    async def today_settled(self, campaign_id: str, trade_date: date) -> bool:
        """오늘 이미 처리된(예정·가속·꼬리) 슬라이스가 있는가. pending 은 제외."""
        val = await self._pool.fetchval(
            "SELECT 1 FROM dca_slice_executions "
            "WHERE campaign_id=$1 AND trade_date=$2 AND status <> 'pending' LIMIT 1",
            campaign_id,
            trade_date,
        )
        return val is not None

    async def today_filled_krw(self, campaign_id: str, trade_date: date) -> int:
        return int(
            await self._pool.fetchval(
                "SELECT COALESCE(SUM(filled_krw),0) FROM dca_slice_executions "
                "WHERE campaign_id=$1 AND trade_date=$2",
                campaign_id,
                trade_date,
            )
        )

    async def fetch_due_retry(self, campaign_id: str, now: datetime) -> SliceExecution | None:
        row = await self._pool.fetchrow(
            f"SELECT {_SLICE_COLUMNS} FROM dca_slice_executions "
            "WHERE campaign_id=$1 AND status='rejected_retry' AND retry_at <= $2 "
            "ORDER BY retry_at LIMIT 1",
            campaign_id,
            now,
        )
        return _slice(row) if row else None

    async def fetch_submitted(self, campaign_id: str) -> list[SliceExecution]:
        rows = await self._pool.fetch(
            f"SELECT {_SLICE_COLUMNS} FROM dca_slice_executions "
            "WHERE campaign_id=$1 AND status='submitted' ORDER BY id",
            campaign_id,
        )
        return [_slice(r) for r in rows]

    async def delete_stale_pending(self, campaign_id: str) -> int:
        """주문 전에 멈춘 pending 슬롯 정리 — 실제 주문이 안 나갔으므로 삭제 안전."""
        result = await self._pool.execute(
            "DELETE FROM dca_slice_executions WHERE campaign_id=$1 AND status='pending'",
            campaign_id,
        )
        # "DELETE N"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def recorded_order_nos(self, campaign_id: str, trade_date: date) -> set[str]:
        rows = await self._pool.fetch(
            "SELECT order_no FROM dca_slice_executions "
            "WHERE campaign_id=$1 AND trade_date=$2 AND order_no IS NOT NULL",
            campaign_id,
            trade_date,
        )
        return {r["order_no"] for r in rows}

    # ----- 슬라이스 쓰기 -----

    async def acquire_slice(
        self,
        *,
        slice_key: str,
        campaign_id: str,
        ticker: str,
        slice_no: int,
        trigger_kind: str,
        trade_date: date,
        budget_krw: int,
    ) -> SliceExecution | None:
        """pending 슬롯 선점. 이미 같은 slice_key 가 있으면(누가 잡았으면) None."""
        row = await self._pool.fetchrow(
            "INSERT INTO dca_slice_executions "
            "(slice_key, campaign_id, ticker, slice_no, trigger_kind, trade_date, "
            " status, budget_krw) "
            "VALUES ($1,$2,$3,$4,$5,$6,'pending',$7) "
            "ON CONFLICT (slice_key) DO NOTHING "
            f"RETURNING {_SLICE_COLUMNS}",
            slice_key,
            campaign_id,
            ticker,
            slice_no,
            trigger_kind,
            trade_date,
            budget_krw,
        )
        return _slice(row) if row else None

    async def update_slice(self, slice_id: int, **fields: Any) -> None:
        cols = [k for k in fields if k in _SLICE_UPDATABLE]
        if not cols:
            return
        sets = ", ".join(f"{c}=${i + 2}" for i, c in enumerate(cols))
        await self._pool.execute(
            f"UPDATE dca_slice_executions SET {sets}, updated_at=NOW() WHERE id=$1",
            slice_id,
            *[fields[c] for c in cols],
        )

    async def finalize_slice(
        self,
        *,
        slice_id: int,
        campaign_id: str,
        status: str,
        filled_qty: int,
        filled_krw: int,
        avg_price: float,
        advance_slices_done: bool,
        set_last_accel: datetime | None = None,
        set_final_tail_done: bool = False,
        new_campaign_status: str | None = None,
    ) -> None:
        """슬라이스 terminal 전이 + 누적 가산을 한 트랜잭션으로.

        filled_krw>0 일 때만 누적이 늘어 cap 불변식(cumulative ≤ cap)을 유지한다.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE dca_slice_executions "
                "SET status=$2, filled_qty=$3, filled_krw=$4, avg_price=$5, updated_at=NOW() "
                "WHERE id=$1",
                slice_id,
                status,
                filled_qty,
                filled_krw,
                avg_price,
            )
            await conn.execute(
                "UPDATE dca_campaigns SET "
                "cumulative_filled_krw = cumulative_filled_krw + $2, "
                "cumulative_filled_qty = cumulative_filled_qty + $3, "
                "slices_done = slices_done + $4, "
                "last_accel_at = COALESCE($5, last_accel_at), "
                "final_tail_done = final_tail_done OR $6, "
                "status = COALESCE($7, status), "
                "updated_at = NOW() "
                "WHERE id=$1",
                campaign_id,
                filled_krw,
                filled_qty,
                1 if advance_slices_done else 0,
                set_last_accel,
                set_final_tail_done,
                new_campaign_status,
            )


__all__ = ["DcaRepository", "SliceStatus"]
