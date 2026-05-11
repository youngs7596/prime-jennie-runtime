"""fetch_previous_run_candidates 테스트 — use_previous_run fallback 입력 단.

조건 검증:
1. 24h 내 1건 매칭 — 동일 macro_state → 후보 반환
2. macro_gate 불일치 → None
3. macro_size_multiplier 차이 > tolerance → None
4. 모든 row 가 ctx_gate/ctx_size missing → None
5. DB engine=None 이면 즉시 None (no-op)
6. candidates 가 없는 chosen run → None
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from prime_jennie_runtime.slow_loop.persistence import fetch_previous_run_candidates


@dataclass
class _MappingResult:
    """SQLAlchemy 의 result.mappings().all() 흉내."""

    _rows: list[dict[str, Any]]

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


@dataclass
class _Result:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def mappings(self) -> _MappingResult:
        return _MappingResult(self.rows)


@dataclass
class _FakeConn:
    """순차적으로 결과를 반환. 호출 순서대로 results 를 pop."""

    results: list[_Result] = field(default_factory=list)
    executed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def execute(self, stmt, params=None) -> _Result:
        self.executed.append((str(stmt), dict(params or {})))
        if not self.results:
            return _Result()
        return self.results.pop(0)

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@dataclass
class _FakeEngine:
    conn: _FakeConn

    def begin(self) -> _FakeConn:
        return self.conn


def _kst_dt(y, m, d, h=12, minute=0) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=timezone(timedelta(hours=9)))


@pytest.mark.asyncio
async def test_engine_none_returns_none():
    res = await fetch_previous_run_candidates(
        None,
        as_of=_kst_dt(2026, 5, 10),
        macro_gate="open",
        macro_size_multiplier=0.75,
    )
    assert res is None


@pytest.mark.asyncio
async def test_match_returns_run_id_and_candidates():
    """직전 run 의 macro_state 가 동일 → reuse."""
    run_id = "sr_20260509_1430"
    list_rows = _Result(
        [
            {
                "scout_run_id": run_id,
                "prev_gate": None,
                "prev_size": None,
                "ctx_gate": "open",
                "ctx_size": "0.75",
                "generated_at": _kst_dt(2026, 5, 9, 14, 30),
            }
        ]
    )
    candidates_rows = _Result(
        [
            {
                "ticker": "005930",
                "strategy_tag": "SECTOR_MOMENTUM",
                "conviction": 0.7,
                "entry_hint_json": {"trigger": "market", "conditions_hint": []},
                "exit_hint_json": None,
                "factors_json": {"momentum_20d": 0.04},
                "notes": "",
            },
            {
                "ticker": "000660",
                "strategy_tag": "EARNINGS_DRIFT",
                "conviction": 0.65,
                "entry_hint_json": json.dumps({"trigger": "market"}),
                "exit_hint_json": json.dumps({"rules_hint": [{"type": "fixed_sl", "pct": 0.05}]}),
                "factors_json": json.dumps({"surprise": 0.12}),
                "notes": "earnings beat",
            },
        ]
    )
    conn = _FakeConn(results=[list_rows, candidates_rows])
    engine = _FakeEngine(conn)

    result = await fetch_previous_run_candidates(
        engine,
        as_of=_kst_dt(2026, 5, 10, 9, 0),
        macro_gate="open",
        macro_size_multiplier=0.75,
    )
    assert result is not None
    chosen_id, candidates = result
    assert chosen_id == run_id
    assert len(candidates) == 2
    assert {c.ticker for c in candidates} == {"005930", "000660"}
    # exit_hint 가 JSON string 으로 와도 잘 파싱
    earnings = next(c for c in candidates if c.ticker == "000660")
    assert earnings.exit_hint is not None
    assert earnings.exit_hint.rules_hint[0]["type"] == "fixed_sl"


@pytest.mark.asyncio
async def test_macro_gate_mismatch_returns_none():
    list_rows = _Result(
        [
            {
                "scout_run_id": "sr_abc",
                "prev_gate": None,
                "prev_size": None,
                "ctx_gate": "closed",  # 어제는 closed
                "ctx_size": "0.0",
                "generated_at": _kst_dt(2026, 5, 9),
            }
        ]
    )
    conn = _FakeConn(results=[list_rows])
    engine = _FakeEngine(conn)
    res = await fetch_previous_run_candidates(
        engine,
        as_of=_kst_dt(2026, 5, 10),
        macro_gate="open",
        macro_size_multiplier=0.75,
    )
    assert res is None


@pytest.mark.asyncio
async def test_macro_size_diff_exceeds_tolerance_returns_none():
    list_rows = _Result(
        [
            {
                "scout_run_id": "sr_abc",
                "prev_gate": None,
                "prev_size": None,
                "ctx_gate": "open",
                "ctx_size": "1.0",  # 1.0 vs 0.75 — diff 0.25 > tolerance 0.05
                "generated_at": _kst_dt(2026, 5, 9),
            }
        ]
    )
    conn = _FakeConn(results=[list_rows])
    engine = _FakeEngine(conn)
    res = await fetch_previous_run_candidates(
        engine,
        as_of=_kst_dt(2026, 5, 10),
        macro_gate="open",
        macro_size_multiplier=0.75,
    )
    assert res is None


@pytest.mark.asyncio
async def test_size_within_tolerance_matches():
    """0.05 차이는 tolerance 이내 → 매칭."""
    list_rows = _Result(
        [
            {
                "scout_run_id": "sr_abc",
                "prev_gate": None,
                "prev_size": None,
                "ctx_gate": "open",
                "ctx_size": "0.78",
                "generated_at": _kst_dt(2026, 5, 9),
            }
        ]
    )
    candidates_rows = _Result(
        [
            {
                "ticker": "005930",
                "strategy_tag": "SECTOR_MOMENTUM",
                "conviction": 0.7,
                "entry_hint_json": {"trigger": "market"},
                "exit_hint_json": None,
                "factors_json": {},
                "notes": "",
            }
        ]
    )
    conn = _FakeConn(results=[list_rows, candidates_rows])
    engine = _FakeEngine(conn)
    res = await fetch_previous_run_candidates(
        engine,
        as_of=_kst_dt(2026, 5, 10),
        macro_gate="open",
        macro_size_multiplier=0.75,
    )
    assert res is not None


@pytest.mark.asyncio
async def test_missing_context_fields_skipped():
    """ctx_gate/ctx_size 가 None 인 row 는 무시. 매칭되는 다음 row 가 없으면 None."""
    list_rows = _Result(
        [
            {
                "scout_run_id": "sr_old",
                "prev_gate": None,
                "prev_size": None,
                "ctx_gate": None,  # missing
                "ctx_size": None,
                "generated_at": _kst_dt(2026, 5, 9),
            }
        ]
    )
    conn = _FakeConn(results=[list_rows])
    engine = _FakeEngine(conn)
    res = await fetch_previous_run_candidates(
        engine,
        as_of=_kst_dt(2026, 5, 10),
        macro_gate="open",
        macro_size_multiplier=0.75,
    )
    assert res is None


@pytest.mark.asyncio
async def test_match_with_zero_candidates_returns_none():
    """매칭은 됐지만 screening_candidates 비어있으면 None."""
    list_rows = _Result(
        [
            {
                "scout_run_id": "sr_empty",
                "prev_gate": None,
                "prev_size": None,
                "ctx_gate": "open",
                "ctx_size": "0.75",
                "generated_at": _kst_dt(2026, 5, 9),
            }
        ]
    )
    candidates_rows = _Result([])  # 빈 candidates
    conn = _FakeConn(results=[list_rows, candidates_rows])
    engine = _FakeEngine(conn)
    res = await fetch_previous_run_candidates(
        engine,
        as_of=_kst_dt(2026, 5, 10),
        macro_gate="open",
        macro_size_multiplier=0.75,
    )
    assert res is None
