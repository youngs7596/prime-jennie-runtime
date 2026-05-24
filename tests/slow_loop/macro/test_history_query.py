"""fetch_recent_macro_runs 단위 — DB 행을 RecentMacroRun 으로 매핑."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from prime_jennie_runtime.slow_loop.macro import history_query
from prime_jennie_runtime.slow_loop.macro.history_query import fetch_recent_macro_runs
from prime_jennie_runtime.slow_loop.macro.schemas import RecentMacroRun


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.captured_params: dict[str, Any] | None = None

    async def execute(self, _stmt: Any, params: dict[str, Any]) -> _FakeResult:
        self.captured_params = params
        return _FakeResult(self._rows)


class _FakeEngine:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._conn = _FakeConn(rows)

    def connect(self) -> _FakeEngine:
        return self

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _BoomEngine:
    def connect(self) -> _BoomEngine:
        return self

    async def __aenter__(self) -> _BoomEngine:
        raise RuntimeError("db down")

    async def __aexit__(self, *_exc: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_maps_rows_in_order_with_summary() -> None:
    rows = [
        {
            "macro_run_id": "mr_20260524_0900_aaa",
            "generated_at": datetime(2026, 5, 24, 9, 0, tzinfo=UTC),
            "gate": "open",
            "size_multiplier": 1.0,
            "top_risks_json": json.dumps(
                [
                    {"category": "geo", "description": "이스라엘 긴장", "severity": "high"},
                    {"category": "rate", "description": "FOMC 매파 우려", "severity": "med"},
                ]
            ),
        },
        {
            "macro_run_id": "mr_20260523_0900_bbb",
            "generated_at": datetime(2026, 5, 23, 9, 0, tzinfo=UTC),
            "gate": "closed",
            "size_multiplier": 0.0,
            "top_risks_json": None,
        },
    ]
    engine = _FakeEngine(rows)
    out = await fetch_recent_macro_runs(engine, limit=2)  # type: ignore[arg-type]

    assert len(out) == 2
    assert all(isinstance(r, RecentMacroRun) for r in out)
    assert out[0].macro_run_id == "mr_20260524_0900_aaa"
    assert out[0].size_multiplier == 1.0
    assert out[0].top_risks_summary == "이스라엘 긴장; FOMC 매파 우려"
    assert out[1].gate == "closed"
    assert out[1].top_risks_summary == ""
    assert engine._conn.captured_params == {"limit": 2}


@pytest.mark.asyncio
async def test_accepts_already_parsed_top_risks() -> None:
    rows = [
        {
            "macro_run_id": "mr_x",
            "generated_at": datetime(2026, 5, 24, tzinfo=UTC),
            "gate": "open",
            "size_multiplier": 0.75,
            "top_risks_json": [
                {"category": "x", "description": "A 위험", "severity": "high"},
                {"category": "y", "description": "B 위험", "severity": "low"},
            ],
        }
    ]
    out = await fetch_recent_macro_runs(_FakeEngine(rows))  # type: ignore[arg-type]
    assert out[0].top_risks_summary == "A 위험; B 위험"


@pytest.mark.asyncio
async def test_malformed_top_risks_falls_back_to_empty_summary() -> None:
    rows = [
        {
            "macro_run_id": "mr_y",
            "generated_at": datetime(2026, 5, 24, tzinfo=UTC),
            "gate": "open",
            "size_multiplier": 1.0,
            "top_risks_json": "not-json",
        }
    ]
    out = await fetch_recent_macro_runs(_FakeEngine(rows))  # type: ignore[arg-type]
    assert len(out) == 1
    assert out[0].top_risks_summary == ""


@pytest.mark.asyncio
async def test_default_limit_is_seven() -> None:
    engine = _FakeEngine([])
    await fetch_recent_macro_runs(engine)  # type: ignore[arg-type]
    assert engine._conn.captured_params == {"limit": history_query.DEFAULT_LIMIT}


@pytest.mark.asyncio
async def test_db_error_returns_empty_list() -> None:
    out = await fetch_recent_macro_runs(_BoomEngine())  # type: ignore[arg-type]
    assert out == []
