"""개별 migrate_*.py 스크립트의 컬럼/SQL/runner 검증.

실 DB 없이도 체크할 것:
- COLUMNS 리스트가 v2 스키마와 일치 (회귀 방지)
- SELECT/INSERT SQL 문자열이 해당 컬럼 셋을 담고 있음
- run_with_sources 가 in-memory source/target 으로도 동작
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from migrations import (
    migrate_daily_quant_scores as quant,
)
from migrations import (
    migrate_signal_logs as signal,
)
from migrations import (
    migrate_trade_logs as trade,
)

from ._fakes import InMemorySource, InMemoryTarget

# ---------- 컬럼 셋 회귀 테스트 ----------


def test_signal_logs_columns_match_v2_schema():
    """v2 signal_logs 컬럼 (17개). 변경 시 002_legacy_namespace.sql 동기화 필요."""
    assert signal.COLUMNS == [
        "id",
        "signal_type",
        "stock_code",
        "stock_name",
        "strategy",
        "price",
        "quantity",
        "hybrid_score",
        "rsi_value",
        "volume_ratio",
        "market_regime",
        "position_multiplier",
        "profit_pct",
        "holding_days",
        "status",
        "suppressed_reason",
        "created_at",
    ]


def test_trade_logs_columns_match_v2_schema():
    assert trade.COLUMNS == [
        "id",
        "stock_code",
        "stock_name",
        "trade_type",
        "quantity",
        "price",
        "total_amount",
        "reason",
        "strategy_signal",
        "market_regime",
        "llm_score",
        "hybrid_score",
        "trade_tier",
        "profit_pct",
        "profit_amount",
        "holding_days",
        "trade_timestamp",
    ]


def test_quant_scores_columns_match_v2_schema():
    assert quant.COLUMNS == [
        "id",
        "score_date",
        "stock_code",
        "stock_name",
        "total_quant_score",
        "momentum_score",
        "quality_score",
        "value_score",
        "technical_score",
        "news_score",
        "supply_demand_score",
        "llm_score",
        "hybrid_score",
        "llm_grade",
        "llm_reason",
        "risk_tag",
        "trade_tier",
        "is_tradable",
        "is_final_selected",
        "run_id",
        "is_active",
    ]


# ---------- SELECT/INSERT SQL 문자열 ----------


def test_signal_select_sql_references_all_columns():
    for col in signal.COLUMNS:
        assert col in signal.SELECT_SQL
    assert f"FROM {signal.TABLE_V2}" in signal.SELECT_SQL


def test_signal_insert_sql_uses_on_conflict_do_nothing():
    assert "ON CONFLICT (id) DO NOTHING" in signal.INSERT_SQL
    assert f"INTO {signal.TABLE_V3}" in signal.INSERT_SQL
    # placeholders 개수 = columns 개수
    for i in range(1, len(signal.COLUMNS) + 1):
        assert f"${i}" in signal.INSERT_SQL


def test_trade_insert_sql_uses_on_conflict_do_nothing():
    assert "ON CONFLICT (id) DO NOTHING" in trade.INSERT_SQL
    assert f"INTO {trade.TABLE_V3}" in trade.INSERT_SQL


def test_quant_insert_sql_uses_on_conflict_do_nothing():
    assert "ON CONFLICT (id) DO NOTHING" in quant.INSERT_SQL
    assert f"INTO {quant.TABLE_V3}" in quant.INSERT_SQL


# ---------- run_with_sources — 실 driver 없이 orchestrator 동작 ----------


@pytest.mark.asyncio
async def test_signal_run_with_sources_copies_batches():
    rows = [
        _signal_row(1, created_at=datetime(2026, 3, 1, 9, 0, 0)),
        _signal_row(2, created_at=datetime(2026, 3, 1, 9, 0, 30)),
        _signal_row(3, created_at=datetime(2026, 3, 1, 10, 0, 0)),
    ]
    source = InMemorySource(rows=rows, timestamp_col="created_at")
    target = InMemoryTarget()
    stats = await signal.run_with_sources(source, target, batch_size=2)
    assert stats.ok
    assert stats.copied == 3
    assert await target.count() == 3


@pytest.mark.asyncio
async def test_trade_run_with_sources_filters_by_since():
    rows = [
        _trade_row(1, trade_timestamp=datetime(2026, 3, 1)),
        _trade_row(2, trade_timestamp=datetime(2026, 4, 1)),
        _trade_row(3, trade_timestamp=datetime(2026, 4, 16)),
    ]
    source = InMemorySource(rows=rows, timestamp_col="trade_timestamp")
    target = InMemoryTarget()
    stats = await trade.run_with_sources(source, target, batch_size=10, since=datetime(2026, 4, 1))
    assert stats.copied == 2


@pytest.mark.asyncio
async def test_quant_run_with_sources_dry_run():
    rows = [_quant_row(1)]
    source = InMemorySource(rows=rows, timestamp_col="score_date")
    target = InMemoryTarget()
    stats = await quant.run_with_sources(source, target, batch_size=10, dry_run=True)
    assert stats.source_total == 1
    assert stats.copied == 0
    assert await target.count() == 0


# ---------- helpers ----------


def _signal_row(i: int, *, created_at: datetime) -> dict:
    return {
        "id": i,
        "signal_type": "BUY",
        "stock_code": f"{i:06d}",
        "stock_name": f"종목{i}",
        "strategy": "HYBRID",
        "price": 10000 * i,
        "quantity": 10,
        "hybrid_score": 0.7,
        "rsi_value": 55.0,
        "volume_ratio": 1.2,
        "market_regime": "BULL",
        "position_multiplier": 1.0,
        "profit_pct": None,
        "holding_days": None,
        "status": "EXECUTED",
        "suppressed_reason": None,
        "created_at": created_at,
    }


def _trade_row(i: int, *, trade_timestamp: datetime) -> dict:
    return {
        "id": i,
        "stock_code": f"{i:06d}",
        "stock_name": f"종목{i}",
        "trade_type": "BUY",
        "quantity": 10,
        "price": 10000 * i,
        "total_amount": 100000 * i,
        "reason": "momentum",
        "strategy_signal": "HYBRID",
        "market_regime": "BULL",
        "llm_score": 0.6,
        "hybrid_score": 0.7,
        "trade_tier": "A",
        "profit_pct": None,
        "profit_amount": None,
        "holding_days": None,
        "trade_timestamp": trade_timestamp,
    }


def _quant_row(i: int) -> dict:
    return {
        "id": i,
        "score_date": date(2026, 4, 1),
        "stock_code": f"{i:06d}",
        "stock_name": f"종목{i}",
        "total_quant_score": 0.8,
        "momentum_score": 0.7,
        "quality_score": 0.6,
        "value_score": 0.5,
        "technical_score": 0.65,
        "news_score": 0.4,
        "supply_demand_score": 0.55,
        "llm_score": 0.75,
        "hybrid_score": 0.7,
        "llm_grade": "B",
        "llm_reason": "reason",
        "risk_tag": None,
        "trade_tier": "A",
        "is_tradable": True,
        "is_final_selected": False,
        "run_id": "r1",
        "is_active": True,
    }
