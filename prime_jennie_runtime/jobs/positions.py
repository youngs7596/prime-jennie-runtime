"""KIS ↔ DB 포지션 동기화 job.

v2 원본: `/jobs/sync-positions` (services/jobs/app.py:2272-2331) +
헬퍼 `compare_positions` (line 2045), `apply_sync` (line 2137),
`_cleanup_redis_position_state` (line 2244), `_resolve_sector` (line 2262).

v3 어댑터:
- KIS 직접 호출 → kis_gateway HTTP `/api/balance` (PortfolioState 응답).
- v2 SQLModel session → asyncpg connection (단일 트랜잭션).
- v2 trade_logs MANUAL_SYNC 감사 INSERT 는 v3 에서 생략.
  legacy_trade_logs 는 v2 frozen snapshot (id=BIGINT PK without DEFAULT) 이고,
  v3 PnL 산출은 outcomes (sheet-level closed PnL) 가 담당. MANUAL_SYNC 흔적은
  반환 actions + 로그로만 남긴다.
- Redis cleanup 은 v2 와 동일 키 (watermark/scale_out/rsi_sold/profit_floor).
- stop_loss_pct 는 v3 AppConfig 에 아직 없어 kwarg 로 주입 (v2 default 6.0%).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import redis.asyncio as aioredis

from .sector_taxonomy import get_sector_group

logger = logging.getLogger(__name__)


DEFAULT_STOP_LOSS_PCT = 6.0  # v2 SellConfig.stop_loss_pct default


def compare_positions(
    kis_positions: list[dict],
    db_positions: list[dict],
) -> dict:
    """v2 `compare_positions` 포팅. 5-way 비교.

    Both inputs as list[dict]. db_positions 는 asyncpg Record → dict 로 변환된 row.

    Returns:
        {
            "only_in_kis": [{...}, ...],
            "only_in_db": [{...}, ...],
            "quantity_mismatch": [{...}, ...],
            "price_mismatch": [{...}, ...],
            "matched": [str, ...],
        }
    """
    kis_map: dict[str, dict] = {p["stock_code"]: p for p in kis_positions}
    db_map: dict[str, dict] = {p["stock_code"]: p for p in db_positions}

    only_in_kis: list[dict] = []
    only_in_db: list[dict] = []
    quantity_mismatch: list[dict] = []
    price_mismatch: list[dict] = []
    matched: list[str] = []

    for code, kis_pos in kis_map.items():
        if code not in db_map:
            only_in_kis.append(kis_pos)

    for code, db_pos in db_map.items():
        if code not in kis_map:
            only_in_db.append(db_pos)

    for code in kis_map:
        if code not in db_map:
            continue
        kis_pos = kis_map[code]
        db_pos = db_map[code]
        kis_qty = int(kis_pos.get("quantity", 0))
        db_qty = int(db_pos.get("quantity", 0))
        kis_avg = int(kis_pos.get("average_buy_price", 0))
        db_avg = int(db_pos.get("average_buy_price", 0))

        if kis_qty != db_qty:
            quantity_mismatch.append(
                {
                    "stock_code": code,
                    "stock_name": kis_pos.get("stock_name", ""),
                    "kis_qty": kis_qty,
                    "db_qty": db_qty,
                }
            )
        elif abs(kis_avg - db_avg) >= 1:
            price_mismatch.append(
                {
                    "stock_code": code,
                    "stock_name": kis_pos.get("stock_name", ""),
                    "kis_avg": kis_avg,
                    "db_avg": db_avg,
                }
            )
        else:
            matched.append(code)

    return {
        "only_in_kis": only_in_kis,
        "only_in_db": only_in_db,
        "quantity_mismatch": quantity_mismatch,
        "price_mismatch": price_mismatch,
        "matched": matched,
    }


async def _cleanup_redis_position_state(
    redis_client: aioredis.Redis, stock_code: str
) -> None:
    """v2 `_cleanup_redis_position_state` 포팅. 키 4종 삭제."""
    try:
        pipe = redis_client.pipeline()
        pipe.delete(f"watermark:{stock_code}")
        pipe.delete(f"scale_out:{stock_code}")
        pipe.delete(f"rsi_sold:{stock_code}")
        pipe.delete(f"profit_floor:{stock_code}")
        await pipe.execute()
        logger.info("[%s] Redis position state cleaned (MANUAL_SYNC)", stock_code)
    except Exception:
        logger.debug("[%s] Redis cleanup failed", stock_code, exc_info=True)


async def _resolve_sector(conn: Any, stock_code: str) -> str | None:
    """v2 `_resolve_sector` 포팅. stock_masters.sector_naver → SectorGroup."""
    row = await conn.fetchrow(
        "SELECT sector_naver FROM stock_masters WHERE stock_code = $1",
        stock_code,
    )
    if row and row["sector_naver"]:
        return get_sector_group(row["sector_naver"], stock_code=stock_code).value
    return None


async def _ensure_stock_master(conn: Any, stock_code: str, stock_name: str) -> None:
    """stock_masters FK 위반 방지. 없으면 KOSPI placeholder 로 생성."""
    existing = await conn.fetchrow(
        "SELECT 1 FROM stock_masters WHERE stock_code = $1", stock_code
    )
    if existing:
        return
    await conn.execute(
        "INSERT INTO stock_masters "
        "(stock_code, stock_name, market, is_active, updated_at) "
        "VALUES ($1, $2, 'KOSPI', TRUE, NOW())",
        stock_code,
        stock_name,
    )
    logger.info("stock_masters 자동 생성: %s %s", stock_code, stock_name)


async def apply_sync(
    conn: Any,
    redis_client: aioredis.Redis | None,
    diff: dict,
    kis_positions: list[dict],
    *,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
) -> list[str]:
    """v2 `apply_sync` 포팅. KIS 기준 덮어쓰기. 단일 트랜잭션 권장."""
    actions: list[str] = []
    kis_map = {p["stock_code"]: p for p in kis_positions}

    # 1) KIS 에만 있는 종목 → INSERT
    for kis_pos in diff["only_in_kis"]:
        code = kis_pos["stock_code"]
        name = kis_pos.get("stock_name", "")
        qty = int(kis_pos.get("quantity", 0))
        avg = int(kis_pos.get("average_buy_price", 0))
        total = int(kis_pos.get("total_buy_amount", 0)) or qty * avg
        cur_price = int(kis_pos.get("current_price", 0)) or avg
        sector = await _resolve_sector(conn, code)
        stop_loss = int(avg * (1 - stop_loss_pct / 100))
        await _ensure_stock_master(conn, code, name)
        if redis_client is not None:
            await _cleanup_redis_position_state(redis_client, code)
        await conn.execute(
            "INSERT INTO positions "
            "(stock_code, stock_name, quantity, average_buy_price, "
            "total_buy_amount, high_watermark, stop_loss_price, sector_group) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            code,
            name,
            qty,
            avg,
            total,
            cur_price,
            stop_loss,
            sector,
        )
        actions.append(f"INSERT {code} {name} qty={qty} avg={avg:,}")

    # 2) DB 에만 있는 종목 → DELETE
    for db_pos in diff["only_in_db"]:
        code = db_pos["stock_code"]
        await conn.execute("DELETE FROM positions WHERE stock_code = $1", code)
        actions.append(f"DELETE {code} {db_pos.get('stock_name', '')}")

    # 3) 공통 종목 → KIS 기준 UPDATE
    common_codes = (
        list(diff.get("matched", []))
        + [m["stock_code"] for m in diff.get("quantity_mismatch", [])]
        + [m["stock_code"] for m in diff.get("price_mismatch", [])]
    )
    for code in common_codes:
        kis_pos = kis_map.get(code)
        if not kis_pos:
            continue
        row = await conn.fetchrow(
            "SELECT quantity, average_buy_price, high_watermark, sector_group "
            "FROM positions WHERE stock_code = $1",
            code,
        )
        if not row:
            continue

        kis_qty = int(kis_pos.get("quantity", 0))
        kis_avg = int(kis_pos.get("average_buy_price", 0))
        kis_total = int(kis_pos.get("total_buy_amount", 0)) or kis_qty * kis_avg
        kis_cur = int(kis_pos.get("current_price", 0))

        new_qty = row["quantity"]
        new_avg = row["average_buy_price"]
        new_stop_loss: int | None = None
        new_hwm = row["high_watermark"]
        new_sector = row["sector_group"]
        changed: list[str] = []

        if row["quantity"] != kis_qty:
            changed.append(f"qty:{row['quantity']}→{kis_qty}")
            new_qty = kis_qty
        if row["average_buy_price"] != kis_avg:
            changed.append(f"avg:{row['average_buy_price']:,}→{kis_avg:,}")
            new_avg = kis_avg
            new_stop_loss = int(kis_avg * (1 - stop_loss_pct / 100))
        if kis_cur and (row["high_watermark"] is None or kis_cur > row["high_watermark"]):
            changed.append(f"hwm:{row['high_watermark']}→{kis_cur}")
            new_hwm = kis_cur
        if row["sector_group"] is None:
            sector = await _resolve_sector(conn, code)
            if sector:
                new_sector = sector
                changed.append(f"sector:→{sector}")

        if not changed:
            continue

        if new_stop_loss is not None:
            await conn.execute(
                "UPDATE positions SET quantity=$1, average_buy_price=$2, "
                "total_buy_amount=$3, high_watermark=$4, sector_group=$5, "
                "stop_loss_price=$6, updated_at=NOW() WHERE stock_code=$7",
                new_qty,
                new_avg,
                kis_total,
                new_hwm,
                new_sector,
                new_stop_loss,
                code,
            )
        else:
            await conn.execute(
                "UPDATE positions SET quantity=$1, average_buy_price=$2, "
                "total_buy_amount=$3, high_watermark=$4, sector_group=$5, "
                "updated_at=NOW() WHERE stock_code=$6",
                new_qty,
                new_avg,
                kis_total,
                new_hwm,
                new_sector,
                code,
            )
        actions.append(f"UPDATE {code} {kis_pos.get('stock_name', '')} {', '.join(changed)}")

    return actions


async def sync_positions(
    pool: Any,
    http: httpx.AsyncClient,
    redis_client: aioredis.Redis,
    gateway_url: str,
    *,
    dry_run: bool = True,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
) -> dict:
    """v2 `/jobs/sync-positions` 포팅.

    1) gateway `/api/balance` → kis_positions
    2) positions 테이블 SELECT → db_positions
    3) compare → diff
    4) dry_run=True: 리포트만, dry_run=False: apply_sync (단일 트랜잭션)

    Returns: {"dry_run": bool, "diff": {...}, "actions": [...], "summary": str}
    """
    resp = await http.get(f"{gateway_url}/api/balance", timeout=15.0)
    resp.raise_for_status()
    balance = resp.json()
    kis_positions = balance.get("positions", []) or []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT stock_code, stock_name, quantity, average_buy_price, "
            "total_buy_amount, high_watermark, sector_group FROM positions"
        )
        db_positions = [dict(r) for r in rows]

        diff = compare_positions(kis_positions, db_positions)
        has_diff = bool(
            diff["only_in_kis"]
            or diff["only_in_db"]
            or diff["quantity_mismatch"]
            or diff["price_mismatch"]
        )

        if not has_diff:
            summary = (
                f"All positions matched. KIS: {len(kis_positions)}종목, "
                f"DB: {len(db_positions)}종목, matched: {len(diff['matched'])}"
            )
            logger.info("sync_positions: %s", summary)
            return {
                "dry_run": dry_run,
                "diff": diff,
                "actions": [],
                "summary": summary,
            }

        if dry_run:
            summary = (
                f"[DRY RUN] KIS: {len(kis_positions)}종목, DB: {len(db_positions)}종목, "
                f"only_in_kis: {len(diff['only_in_kis'])}, "
                f"only_in_db: {len(diff['only_in_db'])}, "
                f"quantity_mismatch: {len(diff['quantity_mismatch'])}, "
                f"price_mismatch: {len(diff['price_mismatch'])}"
            )
            logger.info("sync_positions: %s", summary)
            return {
                "dry_run": True,
                "diff": diff,
                "actions": [],
                "summary": summary,
            }

        async with conn.transaction():
            actions = await apply_sync(
                conn,
                redis_client,
                diff,
                kis_positions,
                stop_loss_pct=stop_loss_pct,
            )
        summary = f"[APPLIED] {len(actions)}건 적용 완료"
        logger.info("sync_positions: %s actions=%s", summary, actions)
        return {
            "dry_run": False,
            "diff": diff,
            "actions": actions,
            "summary": summary,
        }


__all__ = [
    "DEFAULT_STOP_LOSS_PCT",
    "apply_sync",
    "compare_positions",
    "sync_positions",
]
