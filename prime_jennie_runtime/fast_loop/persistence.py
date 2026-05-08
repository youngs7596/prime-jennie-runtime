"""Trade persistence — fast-loop 매매 체결을 PG ``executions`` / ``positions``
테이블에 기록.

매수 (``record_buy``):
    - ``executions`` 에 INSERT (side='buy')
    - ``positions`` UPSERT — 신규면 INSERT, 기존이면 quantity/total_buy_amount 가산
      후 average_buy_price 가중평균 갱신

매도 (``record_sell``):
    - ``executions`` 에 INSERT (side='sell', metadata.exit_reason)
    - ``positions`` UPDATE — quantity/total_buy_amount 비례 감소.
      quantity == 0 이면 DELETE. average_buy_price 는 FIFO 가정으로 유지.

stock_name 은 ``stock_masters`` 에서 lookup, 없으면 ticker 자체를 fallback.

테스트 / 비활성 모드를 위해 ``NoopTradeRecorder`` 제공. recorder 인터페이스를
class 가 아닌 두 개의 async callable 로 두어 entry_executor / exit_executor 가
간단히 호출.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Protocol

import asyncpg

from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)


class TradeRecorder(Protocol):
    async def record_buy(
        self,
        sheet: PositionSheet,
        *,
        filled_qty: int,
        filled_price: float,
        executed_at: datetime,
    ) -> None: ...

    async def record_sell(
        self,
        sheet: PositionSheet,
        *,
        filled_qty: int,
        filled_price: float,
        executed_at: datetime,
        reason: str,
    ) -> None: ...


class NoopTradeRecorder:
    """테스트 / persistence 비활성 환경. 모든 호출은 noop."""

    async def record_buy(
        self,
        sheet: PositionSheet,
        *,
        filled_qty: int,
        filled_price: float,
        executed_at: datetime,
    ) -> None:
        return None

    async def record_sell(
        self,
        sheet: PositionSheet,
        *,
        filled_qty: int,
        filled_price: float,
        executed_at: datetime,
        reason: str,
    ) -> None:
        return None


class PostgresTradeRecorder:
    """asyncpg pool 을 사용해 executions + positions 를 갱신."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record_buy(
        self,
        sheet: PositionSheet,
        *,
        filled_qty: int,
        filled_price: float,
        executed_at: datetime,
    ) -> None:
        if filled_qty <= 0:
            return
        metadata = {
            "strategy_tag": sheet.strategy_tag,
            "scout_run_id": sheet.provenance.scout_run_id,
            "macro_run_id": sheet.provenance.macro_run_id,
        }
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    """
                        INSERT INTO executions
                            (sheet_id, side, price, qty, executed_at, slippage_bps, metadata_json)
                        VALUES ($1, 'buy', $2, $3, $4, NULL, $5)
                        """,
                    sheet.sheet_id,
                    filled_price,
                    filled_qty,
                    executed_at,
                    json.dumps(metadata, ensure_ascii=False, default=str),
                )
                await self._upsert_position_buy(
                    conn,
                    sheet.ticker,
                    filled_qty,
                    int(filled_price),
                )
        except Exception:
            # PG persist 실패가 fast-loop 진입 자체를 막지 않도록 catch.
            # 실 매매는 KIS 쪽에서 이미 일어났으므로 retro-fit 으로 회복 가능.
            logger.exception(
                "record_buy persist failed sheet=%s — execution will be missing in PG",
                sheet.sheet_id,
            )

    async def record_sell(
        self,
        sheet: PositionSheet,
        *,
        filled_qty: int,
        filled_price: float,
        executed_at: datetime,
        reason: str,
    ) -> None:
        if filled_qty <= 0:
            return
        metadata = {
            "strategy_tag": sheet.strategy_tag,
            "exit_reason": reason,
            "scout_run_id": sheet.provenance.scout_run_id,
            "macro_run_id": sheet.provenance.macro_run_id,
        }
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    """
                        INSERT INTO executions
                            (sheet_id, side, price, qty, executed_at, slippage_bps, metadata_json)
                        VALUES ($1, 'sell', $2, $3, $4, NULL, $5)
                        """,
                    sheet.sheet_id,
                    filled_price,
                    filled_qty,
                    executed_at,
                    json.dumps(metadata, ensure_ascii=False, default=str),
                )
                await self._update_position_sell(conn, sheet.ticker, filled_qty)
        except Exception:
            logger.exception(
                "record_sell persist failed sheet=%s — execution will be missing in PG",
                sheet.sheet_id,
            )

    # ── 내부 ──

    async def _upsert_position_buy(
        self,
        conn: asyncpg.Connection,
        stock_code: str,
        qty: int,
        price: int,
    ) -> None:
        """매수 후 positions 갱신 — 가중평균으로 average_buy_price 재계산."""
        stock_name = await self._lookup_stock_name(conn, stock_code)
        new_amount = qty * price
        # 기존 row 가 없으면 INSERT, 있으면 가중평균 UPDATE
        await conn.execute(
            """
            INSERT INTO positions
                (stock_code, stock_name, quantity, average_buy_price, total_buy_amount,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, now(), now())
            ON CONFLICT (stock_code) DO UPDATE SET
                quantity = positions.quantity + EXCLUDED.quantity,
                total_buy_amount = positions.total_buy_amount + EXCLUDED.total_buy_amount,
                average_buy_price = CASE
                    WHEN positions.quantity + EXCLUDED.quantity > 0
                    THEN (positions.total_buy_amount + EXCLUDED.total_buy_amount)
                         / (positions.quantity + EXCLUDED.quantity)
                    ELSE EXCLUDED.average_buy_price
                END,
                updated_at = now()
            """,
            stock_code,
            stock_name,
            qty,
            price,
            new_amount,
        )

    async def _update_position_sell(
        self,
        conn: asyncpg.Connection,
        stock_code: str,
        qty: int,
    ) -> None:
        """매도 후 positions 갱신 — FIFO 가정으로 평단 유지, 수량만 감소.

        quantity 가 0 이하가 되면 DELETE.
        """
        row = await conn.fetchrow(
            "SELECT quantity, average_buy_price FROM positions WHERE stock_code = $1",
            stock_code,
        )
        if row is None:
            logger.warning(
                "record_sell: stock_code=%s not in positions (skipping update)",
                stock_code,
            )
            return
        existing_qty = int(row["quantity"])
        avg_price = int(row["average_buy_price"])
        new_qty = max(existing_qty - qty, 0)
        if new_qty == 0:
            await conn.execute("DELETE FROM positions WHERE stock_code = $1", stock_code)
        else:
            new_amount = new_qty * avg_price
            await conn.execute(
                """
                UPDATE positions SET
                    quantity = $2,
                    total_buy_amount = $3,
                    updated_at = now()
                WHERE stock_code = $1
                """,
                stock_code,
                new_qty,
                new_amount,
            )

    async def _lookup_stock_name(self, conn: asyncpg.Connection, stock_code: str) -> str:
        try:
            row = await conn.fetchrow(
                "SELECT stock_name FROM stock_masters WHERE stock_code = $1",
                stock_code,
            )
            if row and row["stock_name"]:
                return str(row["stock_name"])
        except Exception:
            logger.debug("stock_masters lookup failed for %s", stock_code)
        return stock_code  # fallback


class StockNameResolver(Protocol):
    """ticker → 한국어 종목명. 못 찾으면 ticker 자체 반환."""

    async def __call__(self, stock_code: str) -> str: ...


class PostgresStockNameResolver:
    """``stock_masters`` 에서 종목명을 조회. 결과 메모리 캐싱 (장중 변동 없음)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._cache: dict[str, str] = {}

    async def __call__(self, stock_code: str) -> str:
        if stock_code in self._cache:
            return self._cache[stock_code]
        try:
            row = await self._pool.fetchrow(
                "SELECT stock_name FROM stock_masters WHERE stock_code = $1",
                stock_code,
            )
            if row and row["stock_name"]:
                name = str(row["stock_name"])
                self._cache[stock_code] = name
                return name
        except Exception:
            logger.debug("stock_masters lookup failed for %s", stock_code)
        return stock_code  # fallback: ticker 그대로


__all__ = [
    "TradeRecorder",
    "NoopTradeRecorder",
    "PostgresTradeRecorder",
    "StockNameResolver",
    "PostgresStockNameResolver",
]
