"""fast_loop/app.py 유닛 — PostgresSheetFetcher, BalanceAwareSizer 핵심 동작."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from prime_jennie_runtime.control.state import SystemStateSnapshot
from prime_jennie_runtime.fast_loop import app as fast_app
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet


class _FakePool:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, sql, *args):
        return self._row


class _FakeSystemState:
    def __init__(self, snapshot: SystemStateSnapshot):
        self._snapshot = snapshot

    async def snapshot(self) -> SystemStateSnapshot:
        return self._snapshot


def _blocking_snapshot() -> SystemStateSnapshot:
    return SystemStateSnapshot(stopped=True, pause_reason=None, dryrun=False, liquidate_armed=False)


def _open_snapshot() -> SystemStateSnapshot:
    return SystemStateSnapshot(
        stopped=False, pause_reason=None, dryrun=False, liquidate_armed=False
    )


async def test_postgres_sheet_fetcher_missing_returns_empty():
    pool = _FakePool(row=None)
    fetcher = fast_app.PostgresSheetFetcher(pool)  # type: ignore[arg-type]
    assert await fetcher("no_such_sheet") == []


async def test_postgres_sheet_fetcher_invalid_json_returns_empty():
    pool = _FakePool(row={"sheet_json": {"not": "a valid sheet"}})
    fetcher = fast_app.PostgresSheetFetcher(pool)  # type: ignore[arg-type]
    assert await fetcher("any") == []


async def test_postgres_sheet_fetcher_handles_jsonb_string():
    # asyncpg jsonb 컬럼은 codec 미등록 시 string 으로 반환되므로
    # model_validate_json 경로가 살아있어야 한다.
    sheet = _minimal_sheet()
    pool = _FakePool(row={"sheet_json": sheet.model_dump_json()})
    fetcher = fast_app.PostgresSheetFetcher(pool)  # type: ignore[arg-type]
    result = await fetcher(sheet.sheet_id)
    assert len(result) == 1
    assert result[0].sheet_id == sheet.sheet_id


async def test_postgres_sheet_fetcher_handles_dict():
    sheet = _minimal_sheet()
    pool = _FakePool(row={"sheet_json": sheet.model_dump(mode="json")})
    fetcher = fast_app.PostgresSheetFetcher(pool)  # type: ignore[arg-type]
    result = await fetcher(sheet.sheet_id)
    assert len(result) == 1
    assert result[0].sheet_id == sheet.sheet_id


async def test_balance_sizer_returns_zero_when_stopped():
    kis = AsyncMock()
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_blocking_snapshot()))
    sheet = _minimal_sheet()
    qty = await sizer(sheet)
    assert qty == 0
    kis.get_balance.assert_not_called()
    kis.get_snapshot.assert_not_called()


async def test_balance_sizer_returns_zero_on_zero_price():
    kis = AsyncMock()
    kis.get_balance.return_value = _fake_portfolio(10_000_000)
    kis.get_snapshot.return_value = _fake_stock(price=0)
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_open_snapshot()))
    assert await sizer(_minimal_sheet()) == 0


async def test_balance_sizer_computes_quantity():
    kis = AsyncMock()
    kis.get_balance.return_value = _fake_portfolio(10_000_000)  # 1천만원
    kis.get_snapshot.return_value = _fake_stock(price=20_000)  # 주당 2만원
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_open_snapshot()))
    sheet = _minimal_sheet(final_pct=0.1)  # 10% → 100만원 → 50주
    assert await sizer(sheet) == 50


# ─── helpers ─────────────────────────────────────────────


def _fake_portfolio(cash: int):
    return type(
        "PF",
        (),
        {
            "cash_balance": cash,
            "total_asset": cash,
            "stock_eval_amount": 0,
            "position_count": 0,
            "positions": [],
            "timestamp": datetime.now(tz=UTC),
        },
    )()


def _fake_stock(price: int):
    return type(
        "Stk",
        (),
        {
            "stock_code": "005930",
            "price": price,
            "timestamp": datetime.now(tz=UTC),
        },
    )()


def _minimal_sheet(*, final_pct: float = 0.05) -> PositionSheet:
    now = datetime(2026, 4, 17, 9, 0, 0, tzinfo=KST)
    return PositionSheet(
        sheet_id="ps_20260417_005930_0001",
        generated_at=now,
        valid_until=now + timedelta(hours=6),
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        size={
            "base_pct": final_pct,
            "macro_multiplier": 1.0,
            "risk_multiplier": 1.0,
            "final_pct": final_pct,
            "max_notional_krw": 5_000_000,
        },
        entry={"trigger": "market", "valid_until": now + timedelta(hours=1)},
        exit={
            "rules": [
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ]
        },
        provenance={
            "scout_run_id": "sr_test",
            "scout_code_hash": "sha256:x",
            "scout_hypothesis": "test",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 1.0,
                "gate_run_id": "mr_test",
            },
            "strategy_policy_version": "v3.0.1",
            "generated_by": "test",
        },
    )
