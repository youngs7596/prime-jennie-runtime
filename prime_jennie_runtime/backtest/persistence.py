"""Backtest 결과 → position_sheets / executions / outcomes 영속화.

운영 시트와 namespace 를 분리하기 위해:

* ``sheet_id`` 는 원본 ``ps_…`` 를 ``bt_…`` 로 prefix 치환 (`SHEET_ID_RE` 가 두 prefix 모두 허용).
* ``provenance_json`` 에 ``is_backtest=true`` + ``backtest_run_id`` 마커를 박는다.
  이렇게 두면 dashboard / briefing 의 운영 SELECT 가 ``provenance_json->>'is_backtest' IS NULL``
  으로 손쉽게 필터링할 수 있다 (별도 컬럼/마이그레이션 불필요).

ON CONFLICT DO NOTHING — 동일 backtest_run_id 로 두 번 영속화해도 무해.

meta evaluation pipeline (prime-jennie-meta) 의 평가 path 가 이 테이블 행을
읽어 PR 후보 성과를 채점한다.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from prime_jennie_runtime.position_sheet.schema import PositionSheet

from .domain import SheetBacktestResult

logger = logging.getLogger(__name__)


# `ps_` 또는 `bt_` 접두 → `bt_` 로 강제. 이미 `bt_` 면 그대로.
_SHEET_PREFIX_RE = re.compile(r"^(?:ps|bt)_")


def to_backtest_sheet_id(original_sheet_id: str) -> str:
    """운영 ``ps_…`` sheet_id 를 백테스트 namespace 인 ``bt_…`` 로 변환.

    Backtest 시뮬 결과를 운영 row 와 PK 충돌 없이 영속화하기 위한 변환.
    이미 ``bt_`` 면 그대로 반환.
    """
    if not _SHEET_PREFIX_RE.match(original_sheet_id):
        raise ValueError(f"unsupported sheet_id prefix: {original_sheet_id}")
    return _SHEET_PREFIX_RE.sub("bt_", original_sheet_id, count=1)


_UPSERT_SHEET_SQL = text(
    """
    INSERT INTO position_sheets (
        sheet_id, schema_version, generated_at, valid_until,
        ticker, strategy_tag, sheet_json, provenance_json
    ) VALUES (
        :sid, :sv, :ga, :vu,
        :tk, :st, CAST(:sj AS JSONB), CAST(:pj AS JSONB)
    )
    ON CONFLICT (sheet_id) DO NOTHING
    """
)

# SQLite 폴백 (테스트용) — JSONB 캐스팅 없이 TEXT.
_UPSERT_SHEET_SQL_SQLITE = text(
    """
    INSERT OR IGNORE INTO position_sheets (
        sheet_id, schema_version, generated_at, valid_until,
        ticker, strategy_tag, sheet_json, provenance_json
    ) VALUES (
        :sid, :sv, :ga, :vu,
        :tk, :st, :sj, :pj
    )
    """
)

_INSERT_EXECUTION_SQL = text(
    """
    INSERT INTO executions (
        sheet_id, side, price, qty, executed_at, slippage_bps, metadata_json
    ) VALUES (
        :sid, :side, :price, :qty, :at, :slip, CAST(:meta AS JSONB)
    )
    """
)

_INSERT_EXECUTION_SQL_SQLITE = text(
    """
    INSERT INTO executions (
        sheet_id, side, price, qty, executed_at, slippage_bps, metadata_json
    ) VALUES (
        :sid, :side, :price, :qty, :at, :slip, :meta
    )
    """
)

_UPSERT_OUTCOME_SQL = text(
    """
    INSERT INTO outcomes (
        sheet_id, entry_price, exit_price, holding_period_s,
        pnl_krw, pnl_pct, exit_reason, closed_at, metadata_json
    ) VALUES (
        :sid, :ep, :xp, :hp, :pk, :pp, :er, :ca, CAST(:meta AS JSONB)
    )
    ON CONFLICT (sheet_id) DO UPDATE SET
        exit_price = EXCLUDED.exit_price,
        holding_period_s = EXCLUDED.holding_period_s,
        pnl_krw = EXCLUDED.pnl_krw,
        pnl_pct = EXCLUDED.pnl_pct,
        exit_reason = EXCLUDED.exit_reason,
        closed_at = EXCLUDED.closed_at,
        metadata_json = EXCLUDED.metadata_json
    """
)

_UPSERT_OUTCOME_SQL_SQLITE = text(
    """
    INSERT OR REPLACE INTO outcomes (
        sheet_id, entry_price, exit_price, holding_period_s,
        pnl_krw, pnl_pct, exit_reason, closed_at, metadata_json
    ) VALUES (
        :sid, :ep, :xp, :hp, :pk, :pp, :er, :ca, :meta
    )
    """
)


def _is_sqlite(engine: AsyncEngine) -> bool:
    return engine.dialect.name == "sqlite"


@dataclass
class BacktestPersistenceResult:
    """``BacktestPersistence.persist_one`` 반환값."""

    sheet_id: str  # 영속화에 쓰인 (bt_-prefixed) sheet_id
    persisted: bool  # outcomes 가 기록되었는지 (filled=True 일 때만 True)
    n_executions: int  # 영속화된 executions 행 수


def _make_provenance_marker(
    original: dict[str, Any],
    *,
    backtest_run_id: str,
    original_sheet_id: str,
) -> dict[str, Any]:
    """원본 provenance 에 backtest 마커를 추가한 dict 를 반환."""
    enriched = dict(original)
    enriched["is_backtest"] = True
    enriched["backtest_run_id"] = backtest_run_id
    enriched["backtest_original_sheet_id"] = original_sheet_id
    return enriched


class BacktestPersistence:
    """SheetBacktestResult → DB 영속화.

    호출자는 ``backtest_run_id`` 를 직접 부여하거나 ``new_run_id`` 로 생성.
    동일 run_id 안에서 여러 시트를 영속화하면 후속 meta evaluation 이
    배치 단위로 평가할 수 있다.
    """

    def __init__(self, engine: AsyncEngine, *, backtest_run_id: str | None = None) -> None:
        self._engine = engine
        self._sqlite = _is_sqlite(engine)
        self.backtest_run_id = backtest_run_id or self.new_run_id()

    @staticmethod
    def new_run_id() -> str:
        """``bt_run_<ISO>_<rand>`` 형태의 신규 run_id 생성."""
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"bt_run_{ts}_{uuid.uuid4().hex[:8]}"

    async def persist_one(
        self,
        sheet: PositionSheet,
        result: SheetBacktestResult,
    ) -> BacktestPersistenceResult:
        """단일 시뮬레이션 결과를 ``bt_…`` namespace 로 영속화.

        * position_sheets: sheet_id prefix 만 ``bt_`` 로 바꾸고 provenance 마커 추가.
        * executions: 모든 trade 를 INSERT (buy + sell, scale_out 포함).
        * outcomes: ``result.filled`` 일 때만 INSERT. 미체결 시트는 outcomes 미기록
          (운영 path 와 일관 — 운영도 미체결은 outcomes 없음).
        """
        bt_sheet_id = to_backtest_sheet_id(sheet.sheet_id)

        # 1) provenance 마커 + 시트 JSON 도 sheet_id 만 갈아끼움
        prov_dict = sheet.provenance.model_dump(mode="json")
        prov_dict = _make_provenance_marker(
            prov_dict,
            backtest_run_id=self.backtest_run_id,
            original_sheet_id=sheet.sheet_id,
        )
        sheet_json_dict = json.loads(sheet.model_dump_json())
        sheet_json_dict["sheet_id"] = bt_sheet_id
        sheet_json_dict["provenance"] = prov_dict

        upsert_sheet_sql = _UPSERT_SHEET_SQL_SQLITE if self._sqlite else _UPSERT_SHEET_SQL
        insert_exec_sql = _INSERT_EXECUTION_SQL_SQLITE if self._sqlite else _INSERT_EXECUTION_SQL
        upsert_outcome_sql = _UPSERT_OUTCOME_SQL_SQLITE if self._sqlite else _UPSERT_OUTCOME_SQL

        async with self._engine.begin() as conn:
            await conn.execute(
                upsert_sheet_sql,
                {
                    "sid": bt_sheet_id,
                    "sv": sheet.schema_version,
                    "ga": sheet.generated_at,
                    "vu": sheet.valid_until,
                    "tk": sheet.ticker,
                    "st": sheet.strategy_tag,
                    "sj": json.dumps(sheet_json_dict, ensure_ascii=False, default=str),
                    "pj": json.dumps(prov_dict, ensure_ascii=False, default=str),
                },
            )

            # 2) executions — 모든 trade
            for trade in result.trades:
                meta = dict(trade.metadata)
                meta["backtest_run_id"] = self.backtest_run_id
                meta["reason"] = trade.reason
                await conn.execute(
                    insert_exec_sql,
                    {
                        "sid": bt_sheet_id,
                        "side": trade.side,
                        "price": trade.price,
                        "qty": trade.qty,
                        "at": trade.ts,
                        "slip": None,
                        "meta": json.dumps(meta, ensure_ascii=False, default=str),
                    },
                )

            # 3) outcomes — filled 시에만 (운영 path 일관)
            persisted_outcome = False
            if result.filled and result.entry_price > 0 and result.total_qty > 0:
                exit_price = self._compute_exit_price(result)
                holding_period_s = None
                if result.entry_ts is not None and result.exit_ts is not None:
                    holding_period_s = max(
                        0,
                        int((result.exit_ts - result.entry_ts).total_seconds()),
                    )
                outcome_meta = {
                    "backtest_run_id": self.backtest_run_id,
                    "gross_pnl_krw": result.gross_pnl_krw,
                    "fees_krw": result.fees_krw,
                    "n_trades": len(result.trades),
                }
                await conn.execute(
                    upsert_outcome_sql,
                    {
                        "sid": bt_sheet_id,
                        "ep": result.entry_price,
                        "xp": exit_price,
                        "hp": holding_period_s,
                        "pk": result.net_pnl_krw,
                        "pp": result.pnl_pct,
                        "er": result.exit_reason,
                        "ca": result.exit_ts,
                        "meta": json.dumps(outcome_meta, ensure_ascii=False, default=str),
                    },
                )
                persisted_outcome = True

        return BacktestPersistenceResult(
            sheet_id=bt_sheet_id,
            persisted=persisted_outcome,
            n_executions=len(result.trades),
        )

    @staticmethod
    def _compute_exit_price(result: SheetBacktestResult) -> float | None:
        """outcomes.exit_price = 매도 체결 가중평균 (수량 기준)."""
        sells = [t for t in result.trades if t.side == "sell"]
        if not sells:
            return None
        total_qty = sum(t.qty for t in sells)
        if total_qty <= 0:
            return None
        return sum(t.price * t.qty for t in sells) / total_qty

    async def persist_many(
        self,
        items: list[tuple[PositionSheet, SheetBacktestResult]],
    ) -> list[BacktestPersistenceResult]:
        """편의 메서드 — 다수 (sheet, result) 페어를 순차 영속화."""
        out: list[BacktestPersistenceResult] = []
        for sheet, result in items:
            try:
                out.append(await self.persist_one(sheet, result))
            except Exception:
                logger.exception(
                    "backtest persist failed: sheet=%s run=%s",
                    sheet.sheet_id,
                    self.backtest_run_id,
                )
                raise
        return out


__all__ = [
    "BacktestPersistence",
    "BacktestPersistenceResult",
    "to_backtest_sheet_id",
]
