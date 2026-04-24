"""collect_briefing_data — fake AsyncEngine 으로 shape 검증.

실 Postgres 는 CI 가용성 이슈로 scope 밖 (news_pipeline_kor/test_pg_sentiment_repo.py 와 동일 패턴).
각 섹션 helper 가 독립적으로 비어있는 결과에 대응하는지를 본다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest

from prime_jennie_runtime.briefing.context_builder import (
    MACRO_SNAPSHOT_KEY_PREFIX,
    collect_briefing_data,
)


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


@dataclass
class _FakeRedis:
    payloads: dict[str, str] = field(default_factory=dict)
    raise_on_get: bool = False

    async def get(self, key: str) -> str | None:
        if self.raise_on_get:
            raise RuntimeError("simulated redis failure")
        return self.payloads.get(key)


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
    # 인덱스 별도 테이블 행이 없으니 None 으로 수렴
    assert data["macro"]["kospi_index"] is None
    # redis_client 미주입 — Redis snapshot 필드는 생략돼야 함
    for key in ("vix", "vix_regime", "nikkei_close", "hsi_close", "sox_close", "usd_jpy"):
        assert key not in data["macro"]
    # sectors/consensus/themes 는 수집 경로 자체가 없어 여전히 제외
    assert "sectors_to_favor" not in data["macro"]


@pytest.mark.asyncio
async def test_collect_briefing_data_macro_merges_kospi_kosdaq_indices():
    """macro_runs + index_daily_prices 병합 — 전일 대비 change_pct 계산까지."""
    conn = _FakeConn(
        rows_by_prefix={
            "FROM macro_runs": [
                {
                    "gate": "open",
                    "size_multiplier": 1.0,
                    "reasoning": "정상 운영",
                    "top_risks_json": None,
                    "confidence": "medium",
                    "next_review_hint": None,
                    "generated_at": None,
                }
            ],
            "FROM index_daily_prices": [
                {"index_code": "KOSPI", "price_date": date(2026, 4, 23), "close_price": 6200.0},
                {"index_code": "KOSPI", "price_date": date(2026, 4, 22), "close_price": 6100.0},
                {"index_code": "KOSDAQ", "price_date": date(2026, 4, 23), "close_price": 1170.0},
                {"index_code": "KOSDAQ", "price_date": date(2026, 4, 22), "close_price": 1160.0},
            ],
        }
    )
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 23))

    m = data["macro"]
    assert m["kospi_index"] == 6200.0
    assert m["kospi_change_pct"] == pytest.approx((6200 - 6100) / 6100 * 100)
    assert m["kosdaq_index"] == 1170.0


@pytest.mark.asyncio
async def test_collect_briefing_data_assets_from_recent_snapshot():
    conn = _FakeConn(
        rows_by_prefix={
            "FROM daily_asset_snapshots": [
                {
                    "snapshot_date": date(2026, 4, 22),
                    "total_asset": 200_000_000,
                    "cash_balance": 500_000,
                    "stock_eval_amount": 199_500_000,
                    "position_count": 3,
                }
            ]
        }
    )
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 23))

    assert data["assets"] == {
        "total_asset": 200_000_000,
        "cash_balance": 500_000,
        "stock_eval": 199_500_000,
        "position_count": 3,
    }


@pytest.mark.asyncio
async def test_collect_briefing_data_assets_none_when_snapshot_stale():
    """daily_asset_snapshots 쿼리에 WHERE snapshot_date >= :since 가 걸려 있어
    fake conn 이 행을 반환해도 SQL fresh window 밖이면 None 이 되어야 한다.
    (여기서는 0 행 반환 simulation — 7일 이전 snapshot 만 존재하는 상황 근사)"""
    conn = _FakeConn()  # no rows for any prefix
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 23))
    assert data["assets"] is None


@pytest.mark.asyncio
async def test_collect_briefing_data_watchlist_failure_returns_empty():
    """legacy_quant_scores 테이블이 비어/없어도 전체가 실패하지 않는다."""
    conn = _FakeConn(raise_on_prefix={"FROM legacy_quant_scores"})
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 17))
    assert data["watchlist"] == []


@pytest.mark.asyncio
async def test_collect_briefing_data_news_failure_returns_empty():
    conn = _FakeConn(raise_on_prefix={"FROM news_events"})
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 17))
    assert data["news"] == []


@pytest.mark.asyncio
async def test_collect_briefing_data_macro_merges_redis_snapshot():
    """Redis `macro:data:snapshot:{today}` → VIX/닛케이/항셍/SOX/NVDA/환율/원자재/수급 병합."""
    import json

    snapshot_payload = {
        "kospi_index": 6475.63,
        "kospi_change_pct": 0.12,
        "kosdaq_index": 1203.84,
        "kosdaq_change_pct": 2.51,
        "vix": 19.31,
        "vix_regime": "normal",
        "sox_close": 10078.57,
        "sox_change_pct": 1.71,
        "nvda_close": 199.64,
        "nvda_change_pct": -1.41,
        "nikkei_close": 59716.18,
        "nikkei_change_pct": 0.97,
        "hsi_close": 25975.94,
        "hsi_change_pct": 0.23,
        "usd_jpy": 159.68,
        "usd_jpy_change_pct": 0.12,
        "crude_oil": 95.99,
        "crude_oil_change_pct": 0.15,
        "gold": 4688.6,
        "gold_change_pct": -0.35,
        "kospi_foreign_net": -19497.0,
        "kospi_institutional_net": 8074.0,
        "kospi_retail_net": 11810.0,
    }
    as_of = date(2026, 4, 24)
    redis = _FakeRedis(
        payloads={f"{MACRO_SNAPSHOT_KEY_PREFIX}{as_of.isoformat()}": json.dumps(snapshot_payload)}
    )
    conn = _FakeConn(
        rows_by_prefix={
            "FROM macro_runs": [
                {
                    "gate": "open",
                    "size_multiplier": 1.0,
                    "reasoning": "정상",
                    "top_risks_json": None,
                    "confidence": "medium",
                    "next_review_hint": None,
                    "generated_at": None,
                }
            ]
        }
    )
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=as_of, redis_client=redis)

    m = data["macro"]
    assert m["vix"] == 19.31
    assert m["vix_regime"] == "normal"
    assert m["nikkei_close"] == 59716.18
    assert m["hsi_close"] == 25975.94
    assert m["sox_close"] == 10078.57
    assert m["usd_jpy"] == 159.68
    assert m["crude_oil"] == 95.99
    assert m["gold"] == 4688.6
    assert m["kospi_foreign_net"] == -19497.0
    # DB 에 index 행이 없으므로 Redis kospi_index 로 fallback
    assert m["kospi_index"] == 6475.63
    assert m["kosdaq_index"] == 1203.84
    # BOK 키 없어서 usd_krw 는 Redis 에도 없음
    assert "usd_krw" not in m


@pytest.mark.asyncio
async def test_collect_briefing_data_macro_redis_failure_is_nonfatal():
    """Redis 장애가 나도 macro_runs + DB index 기반 macro 는 여전히 정상 반환."""
    redis = _FakeRedis(raise_on_get=True)
    conn = _FakeConn(
        rows_by_prefix={
            "FROM macro_runs": [
                {
                    "gate": "open",
                    "size_multiplier": 1.0,
                    "reasoning": "정상",
                    "top_risks_json": None,
                    "confidence": "medium",
                    "next_review_hint": None,
                    "generated_at": None,
                }
            ]
        }
    )
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 24), redis_client=redis)
    assert data["macro"]["sentiment"] == "open"
    assert "vix" not in data["macro"]


@pytest.mark.asyncio
async def test_collect_briefing_data_macro_side_tables_failure_are_nonfatal():
    """index_daily_prices / daily_asset_snapshots 장애는 브리핑 본체
    (trades/positions/macro_runs/watchlist/news) 에 영향 없어야 한다."""
    conn = _FakeConn(
        rows_by_prefix={
            "FROM macro_runs": [
                {
                    "gate": "open",
                    "size_multiplier": 1.0,
                    "reasoning": "정상",
                    "top_risks_json": None,
                    "confidence": "medium",
                    "next_review_hint": None,
                    "generated_at": None,
                }
            ]
        },
        raise_on_prefix={
            "FROM index_daily_prices",
            "FROM daily_asset_snapshots",
        },
    )
    engine = _FakeEngine(conn)
    data = await collect_briefing_data(engine, as_of=date(2026, 4, 23))
    assert data["macro"]["sentiment"] == "open"
    assert data["macro"]["kospi_index"] is None
    assert data["assets"] is None
