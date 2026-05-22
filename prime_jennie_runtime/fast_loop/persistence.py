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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import asyncpg

from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)

# 거래비용 (실증, 뱅키스 기준) — outcomes 의 net pnl 계산용.
# 매수: 위탁수수료. 매도: 위탁수수료 + 거래세·농특세 0.20%.
_BUY_FEE_RATE = 0.000140527
_SELL_FEE_RATE = 0.000140527 + 0.0020


@dataclass(frozen=True)
class OutcomeRow:
    """sheet 한 건의 매매 결과 — ``outcomes`` 테이블 1행에 대응."""

    entry_price: float
    exit_price: float
    holding_period_s: int
    pnl_krw: float
    pnl_pct: float
    exit_reason: str | None
    closed_at: datetime
    gross_pnl_krw: float
    fees_krw: float
    matched_qty: int


def _extract_exit_reason(meta: Any) -> str | None:
    """executions.metadata_json 에서 exit_reason 키를 꺼낸다.

    asyncpg 는 jsonb 를 기본적으로 str 로 돌려주므로 str/dict 둘 다 수용.
    """
    if meta is None:
        return None
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return None
    if isinstance(meta, Mapping):
        value = meta.get("exit_reason")
        return str(value) if value is not None else None
    return None


def compute_outcome(exec_rows: Iterable[Mapping[str, Any]]) -> OutcomeRow | None:
    """한 sheet 의 executions 행들로부터 ``outcomes`` 행을 계산.

    ``exec_rows`` 는 side / price / qty / executed_at / metadata_json 키를 갖는
    매핑들 (asyncpg.Record 호환). 매수 또는 매도가 하나도 없으면 ``None``.

    entry/exit_price 는 수량 가중평균, pnl 은 거래비용 차감 후 net.
    """
    buys = [r for r in exec_rows if r["side"] == "buy"]
    sells = [r for r in exec_rows if r["side"] == "sell"]
    if not buys or not sells:
        return None
    buy_qty = sum(int(r["qty"]) for r in buys)
    sell_qty = sum(int(r["qty"]) for r in sells)
    if buy_qty <= 0 or sell_qty <= 0:
        return None
    entry_price = sum(float(r["price"]) * int(r["qty"]) for r in buys) / buy_qty
    exit_price = sum(float(r["price"]) * int(r["qty"]) for r in sells) / sell_qty
    matched_qty = min(buy_qty, sell_qty)
    gross = (exit_price - entry_price) * matched_qty
    fees = entry_price * matched_qty * _BUY_FEE_RATE + exit_price * matched_qty * _SELL_FEE_RATE
    pnl_krw = gross - fees
    entry_notional = entry_price * matched_qty
    pnl_pct = (pnl_krw / entry_notional * 100.0) if entry_notional > 0 else 0.0
    opened_at = min(r["executed_at"] for r in buys)
    last_sell = max(sells, key=lambda r: r["executed_at"])
    closed_at = last_sell["executed_at"]
    holding_period_s = max(0, int((closed_at - opened_at).total_seconds()))
    return OutcomeRow(
        entry_price=entry_price,
        exit_price=exit_price,
        holding_period_s=holding_period_s,
        pnl_krw=pnl_krw,
        pnl_pct=pnl_pct,
        exit_reason=_extract_exit_reason(last_sell.get("metadata_json")),
        closed_at=closed_at,
        gross_pnl_krw=gross,
        fees_krw=fees,
        matched_qty=matched_qty,
    )


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
        fully_closed: bool = False,
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
        fully_closed: bool = False,
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
        fully_closed: bool = False,
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
            return

        # 완전 청산이면 매매 결과를 outcomes 에 기록 (별도 트랜잭션 — best-effort).
        # executions 가 이미 커밋된 뒤라 같은 sheet 의 전 체결을 다시 읽어 집계한다.
        # 실패해도 executions/positions 기록은 보존된다.
        if fully_closed:
            await self._record_outcome(sheet.sheet_id)

    async def _record_outcome(self, sheet_id: str) -> None:
        """완전 청산 sheet 의 결과를 ``outcomes`` 에 UPSERT.

        같은 sheet 의 executions 전 행을 읽어 ``compute_outcome`` 으로 집계.
        매수/매도 행이 부족하면 (entry 데이터 결락 등) skip.
        """
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT side, price, qty, executed_at, metadata_json
                    FROM executions
                    WHERE sheet_id = $1
                    ORDER BY executed_at
                    """,
                    sheet_id,
                )
                outcome = compute_outcome(rows)
                if outcome is None:
                    logger.warning(
                        "record_outcome: sheet=%s — buy/sell 체결 부족, outcomes skip",
                        sheet_id,
                    )
                    return
                meta = {
                    "source": "fast_loop_recorder",
                    "gross_pnl_krw": round(outcome.gross_pnl_krw, 2),
                    "fees_krw": round(outcome.fees_krw, 2),
                    "matched_qty": outcome.matched_qty,
                }
                await conn.execute(
                    """
                    INSERT INTO outcomes
                        (sheet_id, entry_price, exit_price, holding_period_s,
                         pnl_krw, pnl_pct, exit_reason, closed_at, metadata_json)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (sheet_id) DO UPDATE SET
                        entry_price = EXCLUDED.entry_price,
                        exit_price = EXCLUDED.exit_price,
                        holding_period_s = EXCLUDED.holding_period_s,
                        pnl_krw = EXCLUDED.pnl_krw,
                        pnl_pct = EXCLUDED.pnl_pct,
                        exit_reason = EXCLUDED.exit_reason,
                        closed_at = EXCLUDED.closed_at,
                        metadata_json = EXCLUDED.metadata_json
                    """,
                    sheet_id,
                    outcome.entry_price,
                    outcome.exit_price,
                    outcome.holding_period_s,
                    outcome.pnl_krw,
                    outcome.pnl_pct,
                    outcome.exit_reason,
                    outcome.closed_at,
                    json.dumps(meta, ensure_ascii=False, default=str),
                )
        except Exception:
            logger.exception(
                "record_outcome persist failed sheet=%s — outcome will be missing in PG",
                sheet_id,
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
    "OutcomeRow",
    "compute_outcome",
]
