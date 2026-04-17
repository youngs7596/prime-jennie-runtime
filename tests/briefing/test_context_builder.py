"""collect_briefing_data — fake AsyncEngine 으로 shape 검증.

실 Postgres 는 CI 가용성 이슈로 scope 밖 (news_pipeline_kor/test_pg_sentiment_repo.py 와 동일 패턴).
각 섹션 helper 가 독립적으로 비어있는 결과에 대응하는지를 본다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest

from prime_jennie_runtime.briefing.context_builder import collect_briefing_data


@dataclass
class _FakeResult:
    _rows: list[dict]

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict]:
        return list(self._rows)

    def first(self) -> dict | None:
        return self._rows[0] if self._rows else None


@dataclass
class _FakeConn:
    rows_by_prefix: dict[str, list[dict]] = field(default_factory=dict)
    raise_on_prefix: set[str] = field(default_factory=set)
    executed: list[str] = field(default_factory=list)

    async def execute(self, stmt, params=None) -> _FakeResult:
        sql = str(stmt).strip()
        self.executed.append(sql)
        for prefix in self.raise_on_prefix:
            if prefix in sql:
                raise RuntimeError(f"simulated failure for {prefix}")
        for prefix, rows in self.rows_by_prefix.items():
            if prefix in sql:
                return _FakeResult(rows)
        return _FakeResult([])

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@dataclass
class _FakeEngine:
    conn: _FakeConn

    def connect(self) -> _FakeConn:
        return self.conn


@pytest.mark.asyncio
async def test_collect_briefing_data_empty_db_returns_defaults():
    engine = _FakeEngine(_FakeConn())
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 17))

    assert data["date"] == "2026-04-17"
    assert data["positions"] == []
    assert data["trades"] == []
    assert data["watchlist"] == []
    assert data["news"] == []
    assert data["macro"] is None
    assert data["assets"] is None
    assert data["trade_summary"]["buy_count"] == 0
    assert data["trade_summary"]["sell_count"] == 0


@pytest.mark.asyncio
async def test_collect_briefing_data_maps_trades_and_positions():
    conn = _FakeConn(
        rows_by_prefix={
            "FROM executions e": [
                {
                    "side": "buy",
                    "price": 100,
                    "qty": 5,
                    "executed_at": None,
                    "stock_code": "005930",
                    "strategy_tag": "momentum",
                    "pnl_krw": None,
                    "pnl_pct": None,
                    "exit_reason": None,
                },
                {
                    "side": "sell",
                    "price": 110,
                    "qty": 3,
                    "executed_at": None,
                    "stock_code": "005930",
                    "strategy_tag": "momentum",
                    "pnl_krw": 30,
                    "pnl_pct": 10.0,
                    "exit_reason": "tp",
                },
            ],
            "FROM position_sheets ps\n        JOIN executions e": [
                {"stock_code": "005930", "quantity": 2, "avg_price": 100.0},
            ],
        }
    )
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 17))

    assert len(data["trades"]) == 2
    assert data["trades"][0]["trade_type"] == "BUY"
    assert data["trades"][0]["reason"] == "momentum"
    assert data["trades"][1]["trade_type"] == "SELL"
    assert data["trades"][1]["reason"] == "tp"
    assert data["trades"][1]["profit_pct"] == 10.0

    assert data["trade_summary"]["buy_count"] == 1
    assert data["trade_summary"]["sell_count"] == 1
    assert data["trade_summary"]["total_realized_pnl"] == 30

    assert data["positions"] == [
        {
            "stock_code": "005930",
            "stock_name": "005930",
            "quantity": 2,
            "avg_price": 100.0,
            "total_buy": 200.0,
        }
    ]


@pytest.mark.asyncio
async def test_collect_briefing_data_macro_maps_gate_and_risks():
    conn = _FakeConn(
        rows_by_prefix={
            "FROM macro_runs": [
                {
                    "gate": "open",
                    "size_multiplier": 0.5,
                    "reasoning": "선별 매수",
                    "top_risks_json": [{"description": "금리 리스크"}, "환율"],
                    "confidence": "high",
                    "next_review_hint": "내일 08:00",
                    "generated_at": None,
                }
            ]
        }
    )
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 17))

    assert data["macro"]["sentiment"] == "open"
    assert data["macro"]["sentiment_score"] == 0.5
    assert data["macro"]["trading_reasoning"] == "선별 매수"
    assert data["macro"]["risk_factors"] == ["금리 리스크", "환율"]
    assert data["macro"]["regime_hint"] == "high"


@pytest.mark.asyncio
async def test_collect_briefing_data_watchlist_failure_returns_empty():
    """legacy_quant_scores 테이블이 비어/없어도 전체가 실패하지 않는다."""
    conn = _FakeConn(raise_on_prefix={"FROM legacy_quant_scores"})
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 17))
    assert data["watchlist"] == []


@pytest.mark.asyncio
async def test_collect_briefing_data_news_failure_returns_empty():
    conn = _FakeConn(raise_on_prefix={"FROM news_sentiments"})
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 17))
    assert data["news"] == []
