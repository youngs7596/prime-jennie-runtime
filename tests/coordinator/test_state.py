"""Coordinator State Hub (A1 v1) — get_held_positions 머지 로직 + fail-open.

design: .ai/designs/2026-05-14-agent-coordinator.md §4 DecisionContext.held_sheets

검증 범위
---------
1. positions 와 open_sheets 의 합집합 머지에서 source 분류 (external/sheet/mixed).
2. ticker 인자가 양쪽 fetch 헬퍼에 그대로 전달된다.
3. engine 미주입 시 빈 list (fail-open).
4. _fetch_positions / _fetch_open_sheets 는 DB 예외에서 빈 dict 로 fail-open.

DB / Redis 의존 없음. 모듈-level fetch 함수를 monkeypatch 로 교체해 머지 로직만 테스트.
"""

from __future__ import annotations

import pytest

from prime_jennie_runtime.coordinator import state
from prime_jennie_runtime.coordinator.state import (
    HeldPositionSummary,
    _fetch_open_sheets,
    _fetch_positions,
    get_held_positions,
)


def _pos_row(
    *,
    code: str,
    name: str = "테스트종목",
    qty: int = 10,
    avg: int = 50_000,
    sector: str | None = "Tech",
) -> dict:
    return {
        "stock_code": code,
        "stock_name": name,
        "quantity": qty,
        "average_buy_price": avg,
        "sector_group": sector,
    }


# ─────────────────────────────────────────────────────────────────────
# 머지 로직
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_only_when_kis_balance_no_sheet(monkeypatch):
    """KIS 잔고에만 있고 미청산 시트 없으면 source=external."""

    async def fake_positions(engine, *, ticker):
        return {"005935": _pos_row(code="005935", name="삼성전자우", qty=20, avg=60_000)}

    async def fake_sheets(engine, *, ticker):
        return {}

    monkeypatch.setattr(state, "_fetch_positions", fake_positions)
    monkeypatch.setattr(state, "_fetch_open_sheets", fake_sheets)

    result = await get_held_positions(engine=object())

    assert len(result) == 1
    item = result[0]
    assert item.ticker == "005935"
    assert item.source == "external"
    assert item.quantity == 20
    assert item.average_buy_price == 60_000
    assert item.open_sheet_ids == []


@pytest.mark.asyncio
async def test_sheet_only_when_unsold_sheet_no_balance(monkeypatch):
    """미청산 시트만 있고 KIS 잔고 없으면 source=sheet, quantity 0."""

    async def fake_positions(engine, *, ticker):
        return {}

    async def fake_sheets(engine, *, ticker):
        return {"005930": ["ps_20260527_005930_0001"]}

    monkeypatch.setattr(state, "_fetch_positions", fake_positions)
    monkeypatch.setattr(state, "_fetch_open_sheets", fake_sheets)

    result = await get_held_positions(engine=object())

    assert len(result) == 1
    item = result[0]
    assert item.ticker == "005930"
    assert item.source == "sheet"
    assert item.quantity == 0
    assert item.average_buy_price is None
    assert item.name is None
    assert item.open_sheet_ids == ["ps_20260527_005930_0001"]


@pytest.mark.asyncio
async def test_mixed_when_both_present(monkeypatch):
    """KIS 잔고 + 미청산 시트 동시 → source=mixed, KIS 메타 채워짐."""

    async def fake_positions(engine, *, ticker):
        return {
            "005930": _pos_row(code="005930", name="삼성전자", qty=100, avg=70_000),
        }

    async def fake_sheets(engine, *, ticker):
        return {
            "005930": ["ps_20260527_005930_0001", "ps_20260527_005930_0002"],
        }

    monkeypatch.setattr(state, "_fetch_positions", fake_positions)
    monkeypatch.setattr(state, "_fetch_open_sheets", fake_sheets)

    result = await get_held_positions(engine=object())

    assert len(result) == 1
    item = result[0]
    assert item.source == "mixed"
    assert item.quantity == 100
    assert item.name == "삼성전자"
    assert len(item.open_sheet_ids) == 2


@pytest.mark.asyncio
async def test_sort_and_combined_sources(monkeypatch):
    """여러 ticker 가 섞여 있을 때 사전순 정렬 + 각 source 분류 유지."""

    async def fake_positions(engine, *, ticker):
        return {
            "005935": _pos_row(code="005935", name="삼성전자우"),
            "069500": _pos_row(code="069500", name="KODEX 200"),
        }

    async def fake_sheets(engine, *, ticker):
        return {
            "069500": ["ps_a"],
            "035420": ["ps_b"],
        }

    monkeypatch.setattr(state, "_fetch_positions", fake_positions)
    monkeypatch.setattr(state, "_fetch_open_sheets", fake_sheets)

    result = await get_held_positions(engine=object())

    assert [r.ticker for r in result] == ["005935", "035420", "069500"]
    by_ticker = {r.ticker: r for r in result}
    assert by_ticker["005935"].source == "external"
    assert by_ticker["035420"].source == "sheet"
    assert by_ticker["069500"].source == "mixed"


@pytest.mark.asyncio
async def test_empty_when_no_data(monkeypatch):
    async def fake_empty(engine, *, ticker):
        return {}

    monkeypatch.setattr(state, "_fetch_positions", fake_empty)
    monkeypatch.setattr(state, "_fetch_open_sheets", fake_empty)

    result = await get_held_positions(engine=object())
    assert result == []


@pytest.mark.asyncio
async def test_ticker_filter_forwarded(monkeypatch):
    """ticker 인자가 양쪽 fetch 헬퍼에 그대로 전달되는지."""
    captured: dict[str, str | None] = {}

    async def fake_positions(engine, *, ticker):
        captured["positions"] = ticker
        return {}

    async def fake_sheets(engine, *, ticker):
        captured["sheets"] = ticker
        return {}

    monkeypatch.setattr(state, "_fetch_positions", fake_positions)
    monkeypatch.setattr(state, "_fetch_open_sheets", fake_sheets)

    await get_held_positions(engine=object(), ticker="005930")

    assert captured == {"positions": "005930", "sheets": "005930"}


# ─────────────────────────────────────────────────────────────────────
# fail-open
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_none_returns_empty():
    """engine 미주입 — 빈 list."""
    result = await get_held_positions(engine=None)
    assert result == []


@pytest.mark.asyncio
async def test_positions_fetch_failopen_on_db_error():
    """_fetch_positions: 예외 → 빈 dict, 로그만 남기고 진행."""

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("positions table missing")

    result = await _fetch_positions(_BrokenEngine(), ticker=None)
    assert result == {}


@pytest.mark.asyncio
async def test_open_sheets_fetch_failopen_on_db_error():
    """_fetch_open_sheets: 예외 → 빈 dict."""

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("position_sheets view missing")

    result = await _fetch_open_sheets(_BrokenEngine(), ticker=None)
    assert result == {}


# ─────────────────────────────────────────────────────────────────────
# Pydantic schema sanity
# ─────────────────────────────────────────────────────────────────────


def test_held_position_summary_defaults():
    """sheet-only 케이스 schema sanity — name/avg None, qty 0, open_sheet_ids."""
    s = HeldPositionSummary(ticker="005930", source="sheet", open_sheet_ids=["ps_x"])
    assert s.quantity == 0
    assert s.average_buy_price is None
    assert s.name is None
    assert s.sector_group is None
    assert s.open_sheet_ids == ["ps_x"]
