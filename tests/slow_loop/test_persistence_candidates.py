"""migration 012 — scout_runs.context_snapshot_json + screening_candidates 테스트.

_FakeEngine 로 SQL/파라미터 캡처를 검증한다 (tests/council_logging/test_persistence.py
패턴). 실 Postgres 라운드트립은 아직 CI 에서 돌리지 않음.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from prime_jennie_runtime.slow_loop.persistence import (
    persist_scout_run,
    persist_screening_candidates,
    update_candidate_promotion,
)
from prime_jennie_runtime.slow_loop.scout.schemas import (
    EntryHint,
    ExitHint,
    ScoutOutput,
    ScreeningCandidate,
)

# --- fake engine (공유 패턴) ---


@dataclass
class _FakeResult:
    _rows: list[Any] = field(default_factory=list)

    def one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None


@dataclass
class _FakeConn:
    executed: list[tuple[str, dict]] = field(default_factory=list)

    async def execute(self, stmt, params=None) -> _FakeResult:
        self.executed.append((str(stmt), dict(params or {})))
        return _FakeResult()

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@dataclass
class _FakeEngine:
    conn: _FakeConn

    def begin(self) -> _FakeConn:
        return self.conn


def _scout_out() -> ScoutOutput:
    return ScoutOutput(
        screening_code="def screen(m, c):\n    return []\n",
        hypothesis="test hypothesis",
        expected_candidates=0,
        factor_weights={"momentum": 0.7},
        strategy_tags_used=["MOMENTUM"],
        fallback_strategy="skip_today",
        estimated_runtime_seconds=5.0,
    )


def _candidate(ticker: str = "005930", tag: str = "MOMENTUM") -> ScreeningCandidate:
    return ScreeningCandidate(
        ticker=ticker,
        strategy_tag=tag,
        conviction=0.75,
        entry_hint=EntryHint(trigger="market", price_hint=None, conditions_hint=[]),
        exit_hint=ExitHint(rules_hint=[{"type": "fixed_sl", "loss_pct": -0.05}]),
        factors={"momentum": 0.8, "volume": 0.6},
        notes="large cap semi",
    )


# --- persist_scout_run: context_snapshot round-trip ---


@pytest.mark.asyncio
async def test_persist_scout_run_serializes_context_snapshot():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    ctx_snap = {
        "as_of": "2026-04-20",
        "universe_size": 200,
        "universe_hash": "abcdef",
        "news_events": {"005930": {"score": 0.3, "staleness_hours": 2.0}},
        "sector_momentum": {"반도체": 0.05},
        "macro_size_multiplier": 1.0,
        "macro_run_id": "mr_20260420_0800_scheduled",
    }
    await persist_scout_run(
        engine,
        scout_run_id="scout_20260420_0830",
        generated_at=datetime(2026, 4, 20, 8, 30),
        scout_out=_scout_out(),
        context_snapshot=ctx_snap,
    )
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "context_snapshot_json" in sql
    assert "CAST(:ctx AS JSONB)" in sql
    assert "ON CONFLICT (scout_run_id) DO UPDATE" in sql
    assert "context_snapshot_json = EXCLUDED.context_snapshot_json" in sql
    ctx_payload = json.loads(params["ctx"])
    assert ctx_payload["universe_hash"] == "abcdef"
    assert ctx_payload["macro_run_id"] == "mr_20260420_0800_scheduled"
    assert ctx_payload["news_events"]["005930"]["score"] == 0.3


@pytest.mark.asyncio
async def test_persist_scout_run_writes_no_llm_columns():
    """결정론 scout — model_used / cost_usd 는 INSERT 에서 제외 (항상 NULL).

    scout 는 2026-05-22 Phase 1 이후 LLM 을 호출하지 않으므로 LLM 메타 컬럼을
    쓰지 않는다. prompt_version 컬럼에는 스코어러 버전이 들어간다.
    """
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    await persist_scout_run(
        engine,
        scout_run_id="scout_20260525_0830",
        generated_at=datetime(2026, 5, 25, 8, 30),
        scout_out=_scout_out(),
        scorer_version="deterministic-quant-v2-port@1",
    )
    sql, params = conn.executed[0]
    assert "model_used" not in sql
    assert "cost_usd" not in sql
    assert params["ver"] == "deterministic-quant-v2-port@1"
    assert "model" not in params
    assert "cost" not in params


@pytest.mark.asyncio
async def test_persist_scout_run_engine_none_is_noop():
    """DB 미구성 smoke 환경 — 예외 없이 no-op (LLM 통계도 적재하지 않음)."""
    await persist_scout_run(
        None,
        scout_run_id="scout_x",
        generated_at=datetime(2026, 5, 25, 8, 30),
        scout_out=_scout_out(),
        scorer_version="deterministic-quant-v2-port@1",
    )


@pytest.mark.asyncio
async def test_persist_scout_run_candidates_count_uses_actual_value():
    """candidates_count 가 주어지면 그 값을 그대로 저장 — expected_candidates 로
    대체되지 않아야 한다.
    """
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    scout_out = _scout_out()  # expected_candidates=0
    await persist_scout_run(
        engine,
        scout_run_id="scout_x",
        generated_at=datetime(2026, 4, 27, 8, 30),
        scout_out=scout_out,
        candidates_count=7,
    )
    _, params = conn.executed[0]
    assert params["cnt"] == 7


@pytest.mark.asyncio
async def test_persist_scout_run_candidates_count_none_stores_null():
    """candidates_count 미지정 시 NULL 로 저장 — expected_candidates fallback 제거됨."""
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    scout_out = ScoutOutput(
        screening_code="def screen(m, c): return []\n",
        hypothesis="h",
        expected_candidates=15,  # fallback 으로 새지 않아야 함
        factor_weights={"momentum": 1.0},
        strategy_tags_used=["MOMENTUM"],
        fallback_strategy="skip_today",
        estimated_runtime_seconds=1.0,
    )
    await persist_scout_run(
        engine,
        scout_run_id="scout_x",
        generated_at=datetime(2026, 4, 27, 8, 30),
        scout_out=scout_out,
    )
    _, params = conn.executed[0]
    assert params["cnt"] is None


@pytest.mark.asyncio
async def test_persist_scout_run_empty_context_defaults_to_empty_dict():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    await persist_scout_run(
        engine,
        scout_run_id="scout_20260420_0830",
        generated_at=datetime(2026, 4, 20, 8, 30),
        scout_out=_scout_out(),
    )
    _, params = conn.executed[0]
    assert json.loads(params["ctx"]) == {}


# --- persist_screening_candidates ---


@pytest.mark.asyncio
async def test_persist_screening_candidates_inserts_all_with_rank():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    cands = [
        _candidate(ticker="005930", tag="MOMENTUM"),
        _candidate(ticker="000660", tag="MOMENTUM"),
        _candidate(ticker="035720", tag="DIP_BUY"),
    ]
    await persist_screening_candidates(
        engine,
        scout_run_id="scout_20260420_0830",
        candidates=cands,
    )
    # DELETE + 3×INSERT = 4 statements
    assert len(conn.executed) == 4
    assert "DELETE FROM screening_candidates" in conn.executed[0][0]
    for idx, expected_ticker in enumerate(["005930", "000660", "035720"]):
        sql, params = conn.executed[idx + 1]
        assert "INSERT INTO screening_candidates" in sql
        assert params["rank"] == idx
        assert params["ticker"] == expected_ticker
        assert params["sid"] == "scout_20260420_0830"
        # JSONB 컬럼은 string 으로 직렬화되어야 함
        entry = json.loads(params["entry"])
        assert entry["trigger"] == "market"


@pytest.mark.asyncio
async def test_persist_screening_candidates_empty_list_is_noop():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    await persist_screening_candidates(
        engine,
        scout_run_id="scout_20260420_0830",
        candidates=[],
    )
    assert conn.executed == []


@pytest.mark.asyncio
async def test_persist_screening_candidates_serializes_null_exit_hint():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    cand = ScreeningCandidate(
        ticker="005930",
        strategy_tag="MOMENTUM",
        conviction=0.5,
        entry_hint=EntryHint(trigger="limit", price_hint=70000.0, conditions_hint=[]),
        exit_hint=None,
        factors={},
        notes="",
    )
    await persist_screening_candidates(engine, scout_run_id="scout_x", candidates=[cand])
    _, params = conn.executed[1]  # skip DELETE
    assert params["exit"] is None


@pytest.mark.asyncio
async def test_persist_screening_candidates_engine_none_is_noop():
    # Smoke 환경 (DB 미구성) 에서 raise 하지 않아야 함
    await persist_screening_candidates(None, scout_run_id="x", candidates=[_candidate()])


# --- update_candidate_promotion ---


@pytest.mark.asyncio
async def test_update_candidate_promotion_sets_sheet_id():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    await update_candidate_promotion(
        engine,
        scout_run_id="scout_20260420_0830",
        ticker="005930",
        sheet_id="ps_20260420_005930_abcd",
    )
    sql, params = conn.executed[0]
    assert "UPDATE screening_candidates" in sql
    assert "promoted_to_sheet_id = :sid" in sql
    assert "rejection_reason = :reason" in sql
    assert params["sid"] == "ps_20260420_005930_abcd"
    assert params["reason"] is None
    assert params["ticker"] == "005930"


@pytest.mark.asyncio
async def test_update_candidate_promotion_sets_rejection_reason():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    await update_candidate_promotion(
        engine,
        scout_run_id="scout_x",
        ticker="000660",
        rejection_reason="macro_closed",
    )
    _, params = conn.executed[0]
    assert params["sid"] is None
    assert params["reason"] == "macro_closed"


@pytest.mark.asyncio
async def test_update_candidate_promotion_both_none_is_noop():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    await update_candidate_promotion(
        engine,
        scout_run_id="scout_x",
        ticker="005930",
    )
    assert conn.executed == []
