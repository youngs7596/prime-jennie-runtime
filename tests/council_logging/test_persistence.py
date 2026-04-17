"""persistence.save/fetch/list — fake AsyncEngine 로 SQL/라운드트립 검증.

실 Postgres 는 CI 가용성 이슈로 scope 밖 (tests/briefing/test_context_builder.py 와 동일 패턴).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from prime_jennie_runtime.council_logging import (
    CouncilRunRecord,
    CouncilStepOutput,
    fetch_council_run,
    list_council_runs,
    record_as_dict,
    save_council_run,
)

# --- fake engine scaffolding ---


@dataclass
class _FakeResult:
    _rows: list[Any]

    def one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


@dataclass
class _FakeConn:
    rows: list[Any] = field(default_factory=list)
    executed: list[tuple[str, dict]] = field(default_factory=list)

    async def execute(self, stmt, params=None) -> _FakeResult:
        self.executed.append((str(stmt), dict(params or {})))
        return _FakeResult(self.rows)

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@dataclass
class _FakeEngine:
    conn: _FakeConn

    def connect(self) -> _FakeConn:
        return self.conn

    def begin(self) -> _FakeConn:
        return self.conn


def _full_record() -> CouncilRunRecord:
    return CouncilRunRecord(
        macro_run_id="macro_20260417_0800",
        generated_at=datetime(2026, 4, 17, 8, 0, 0),
        trigger_reason="scheduled_0800",
        gate="open",
        size_multiplier=1.1,
        reasoning="ok",
        top_risks=[{"category": "monetary", "severity": "low"}],
        confidence="high",
        cost_usd=0.21,
        latency_ms=3200,
        auto_override=False,
        target_date=date(2026, 4, 17),
        steps=[
            CouncilStepOutput(name="strategist", output={"sentiment_score": 72}, cost_usd=0.1),
            CouncilStepOutput(
                name="chief_judge",
                output={"council_consensus": "agree"},
                cost_usd=0.11,
            ),
        ],
        council_consensus="agree",
        trading_reasoning="KOSPI bullish",
        strategies_to_favor=["MOMENTUM"],
        strategies_to_avoid=["DIP_BUY"],
        sectors_to_favor=["반도체"],
        sectors_to_avoid=["건설"],
        opportunity_factors=["SOX 반등"],
        risk_factors=["환율"],
        input_briefing_text="brief",
        input_global_snapshot={"vix": 15.2},
        input_political_news=["news1"],
        input_sector_momentum_text="semis+",
        input_index_technical_text="MA50>MA200",
        final_sentiment="bullish",
        final_sentiment_score=72,
        final_regime_hint="uptrend",
        final_position_size_pct=110,
        final_stop_loss_adjust_pct=95,
        political_risk_level="medium",
    )


# --- save_council_run ---


@pytest.mark.asyncio
async def test_save_council_run_serializes_metadata_payload():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    await save_council_run(engine, _full_record())

    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "INSERT INTO macro_runs" in sql
    assert "ON CONFLICT (macro_run_id) DO UPDATE" in sql
    # 주요 컬럼
    assert params["macro_run_id"] == "macro_20260417_0800"
    assert params["gate"] == "open"
    assert params["size_multiplier"] == 1.1
    # top_risks 는 JSON 문자열로
    top_risks = json.loads(params["top_risks_json"])
    assert top_risks[0]["category"] == "monetary"
    # metadata_json 에 council_log 병합
    meta = json.loads(params["metadata_json"])
    assert "council_log" in meta
    payload = meta["council_log"]
    assert payload["target_date"] == "2026-04-17"
    assert payload["council_consensus"] == "agree"
    assert payload["strategies_to_favor"] == ["MOMENTUM"]
    assert payload["sectors_to_avoid"] == ["건설"]
    assert payload["final_sentiment"] == "bullish"
    assert payload["input"]["briefing_text"] == "brief"
    assert payload["input"]["global_snapshot"]["vix"] == 15.2
    # step 배열 직렬화
    assert len(payload["steps"]) == 2
    assert payload["steps"][0]["name"] == "strategist"
    assert payload["steps"][0]["output"]["sentiment_score"] == 72


@pytest.mark.asyncio
async def test_save_council_run_handles_minimal_record():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    rec = CouncilRunRecord(
        macro_run_id="macro_min",
        generated_at=datetime(2026, 4, 17, 8, 0),
        trigger_reason="manual",
        gate="closed",
        size_multiplier=0.0,
    )
    await save_council_run(engine, rec)
    _, params = conn.executed[0]
    meta = json.loads(params["metadata_json"])
    assert meta["council_log"]["target_date"] is None
    assert meta["council_log"]["steps"] == []
    assert meta["council_log"]["success"] is True


# --- fetch_council_run ---


def _make_row(record: CouncilRunRecord, *, metadata: dict | None = None) -> SimpleNamespace:
    if metadata is None:
        metadata = {
            "council_log": {
                "target_date": record.target_date.isoformat() if record.target_date else None,
                "steps": [
                    {
                        "name": s.name,
                        "output": s.output,
                        "model_used": s.model_used,
                        "cost_usd": s.cost_usd,
                        "latency_ms": s.latency_ms,
                        "prompt_version": s.prompt_version,
                    }
                    for s in record.steps
                ],
                "council_consensus": record.council_consensus,
                "trading_reasoning": record.trading_reasoning,
                "strategies_to_favor": record.strategies_to_favor,
                "strategies_to_avoid": record.strategies_to_avoid,
                "sectors_to_favor": record.sectors_to_favor,
                "sectors_to_avoid": record.sectors_to_avoid,
                "opportunity_factors": record.opportunity_factors,
                "risk_factors": record.risk_factors,
                "input": {
                    "briefing_text": record.input_briefing_text,
                    "global_snapshot": record.input_global_snapshot,
                    "political_news": record.input_political_news,
                    "sector_momentum_text": record.input_sector_momentum_text,
                    "index_technical_text": record.input_index_technical_text,
                },
                "final_sentiment": record.final_sentiment,
                "final_sentiment_score": record.final_sentiment_score,
                "final_regime_hint": record.final_regime_hint,
                "final_position_size_pct": record.final_position_size_pct,
                "final_stop_loss_adjust_pct": record.final_stop_loss_adjust_pct,
                "political_risk_level": record.political_risk_level,
                "success": record.success,
                "error": record.error,
            }
        }
    return SimpleNamespace(
        macro_run_id=record.macro_run_id,
        generated_at=record.generated_at,
        trigger_reason=record.trigger_reason,
        gate=record.gate,
        size_multiplier=record.size_multiplier,
        reasoning=record.reasoning,
        top_risks_json=record.top_risks,
        confidence=record.confidence,
        news_digest_ref=record.news_digest_ref,
        next_review_hint=record.next_review_hint,
        prompt_version=record.prompt_version,
        model_used=record.model_used,
        cost_usd=record.cost_usd,
        latency_ms=record.latency_ms,
        auto_override=record.auto_override,
        metadata_json=metadata,
    )


@pytest.mark.asyncio
async def test_fetch_council_run_returns_none_when_missing():
    engine = _FakeEngine(_FakeConn(rows=[]))
    assert await fetch_council_run(engine, "nope") is None


@pytest.mark.asyncio
async def test_fetch_council_run_round_trips_metadata():
    original = _full_record()
    conn = _FakeConn(rows=[_make_row(original)])
    engine = _FakeEngine(conn)
    rec = await fetch_council_run(engine, original.macro_run_id)
    assert rec is not None
    assert rec.macro_run_id == original.macro_run_id
    assert rec.target_date == date(2026, 4, 17)
    assert rec.final_sentiment == "bullish"
    assert rec.final_sentiment_score == 72
    assert rec.strategies_to_favor == ["MOMENTUM"]
    assert rec.sectors_to_avoid == ["건설"]
    assert rec.input_global_snapshot == {"vix": 15.2}
    assert len(rec.steps) == 2
    assert rec.steps[0].name == "strategist"
    assert rec.steps[0].output == {"sentiment_score": 72}


@pytest.mark.asyncio
async def test_fetch_council_run_tolerates_string_metadata_json():
    """metadata_json 가 이미 직렬화된 문자열로 도착해도 파싱한다."""
    original = _full_record()
    row = _make_row(original)
    row.metadata_json = json.dumps({"council_log": {"council_consensus": "disagree"}}, default=str)
    conn = _FakeConn(rows=[row])
    engine = _FakeEngine(conn)
    rec = await fetch_council_run(engine, original.macro_run_id)
    assert rec is not None
    assert rec.council_consensus == "disagree"


@pytest.mark.asyncio
async def test_fetch_council_run_handles_missing_metadata_key():
    """council_log 가 없는 과거 macro_runs 행도 핵심 컬럼만으로 레코드 생성."""
    original = _full_record()
    row = _make_row(original, metadata={})
    conn = _FakeConn(rows=[row])
    engine = _FakeEngine(conn)
    rec = await fetch_council_run(engine, original.macro_run_id)
    assert rec is not None
    assert rec.macro_run_id == original.macro_run_id
    assert rec.gate == "open"
    # metadata_json.council_log 없으면 확장 필드는 기본값
    assert rec.steps == []
    assert rec.strategies_to_favor == []
    assert rec.final_sentiment is None


# --- list_council_runs ---


@pytest.mark.asyncio
async def test_list_council_runs_returns_all_rows_in_order():
    r1 = _full_record()
    r2 = _full_record()
    r2.macro_run_id = "macro_20260416_0800"
    r2.generated_at = datetime(2026, 4, 16, 8, 0, 0)
    conn = _FakeConn(rows=[_make_row(r1), _make_row(r2)])
    engine = _FakeEngine(conn)
    rows = await list_council_runs(engine, limit=10)
    assert [r.macro_run_id for r in rows] == [r1.macro_run_id, r2.macro_run_id]


@pytest.mark.asyncio
async def test_list_council_runs_applies_date_filters_in_sql():
    conn = _FakeConn(rows=[])
    engine = _FakeEngine(conn)
    since = datetime(2026, 4, 10, 0, 0)
    until = datetime(2026, 4, 17, 23, 59)
    await list_council_runs(engine, since=since, until=until, limit=25)
    sql, params = conn.executed[0]
    assert "generated_at >= :since" in sql
    assert "generated_at <= :until" in sql
    assert params["since"] == since
    assert params["until"] == until
    assert params["limit"] == 25


# --- record_as_dict ---


def test_record_as_dict_isoformats_datetimes():
    rec = _full_record()
    d = record_as_dict(rec)
    assert d["generated_at"] == "2026-04-17T08:00:00"
    assert d["target_date"] == "2026-04-17"
    assert d["strategies_to_favor"] == ["MOMENTUM"]
