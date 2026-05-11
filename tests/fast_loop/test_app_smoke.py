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


async def test_balance_sizer_applies_intraday_multiplier():
    kis = AsyncMock()
    kis.get_balance.return_value = _fake_portfolio(10_000_000)
    kis.get_snapshot.return_value = _fake_stock(price=20_000)

    class _StubThrottle:
        def __init__(self, mult: float):
            self._mult = mult

        def current_multiplier(self) -> float:
            return self._mult

    # 10% × 0.6 (WARNING) = 6% → 60만원 → 30주
    sizer = fast_app.BalanceAwareSizer(
        kis=kis,
        system_state=_FakeSystemState(_open_snapshot()),
        risk_throttle=_StubThrottle(0.6),  # type: ignore[arg-type]
    )
    sheet = _minimal_sheet(final_pct=0.1)
    assert await sizer(sheet) == 30


async def test_balance_sizer_critical_zero_blocks_entry():
    kis = AsyncMock()
    kis.get_balance.return_value = _fake_portfolio(10_000_000)
    kis.get_snapshot.return_value = _fake_stock(price=20_000)

    class _ZeroThrottle:
        def current_multiplier(self) -> float:
            return 0.0

    sizer = fast_app.BalanceAwareSizer(
        kis=kis,
        system_state=_FakeSystemState(_open_snapshot()),
        risk_throttle=_ZeroThrottle(),  # type: ignore[arg-type]
    )
    assert await sizer(_minimal_sheet(final_pct=0.1)) == 0


async def test_balance_sizer_uses_total_asset_not_cash():
    """분모가 total_asset 이라 cash 가 작아도 시트 의도대로 사이즈."""
    kis = AsyncMock()
    # cash 100만, 평가액 9백만 → total 1천만. cash 만 보면 10% = 10만 = 5주.
    # total 보면 10% = 100만 = 50주 (단, cash 100만 이내라 50주 가능)
    kis.get_balance.return_value = _fake_portfolio(cash=1_000_000, stock_eval=9_000_000)
    kis.get_snapshot.return_value = _fake_stock(price=20_000)
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_open_snapshot()))
    sheet = _minimal_sheet(final_pct=0.1)
    assert await sizer(sheet) == 50


async def test_balance_sizer_clamped_by_cash():
    """target 이 cash 초과하면 cash 한도로 클램프."""
    kis = AsyncMock()
    # total 1천만, cash 50만 (대부분 stock 평가). 10% = 100만이 target 이지만
    # cash 50만이 상한 → 25주.
    kis.get_balance.return_value = _fake_portfolio(cash=500_000, stock_eval=9_500_000)
    kis.get_snapshot.return_value = _fake_stock(price=20_000)
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_open_snapshot()))
    sheet = _minimal_sheet(final_pct=0.1)
    assert await sizer(sheet) == 25


async def test_balance_sizer_clamped_by_max_notional():
    """target 이 max_notional_krw 초과하면 시트 cap 적용."""
    kis = AsyncMock()
    # total 1억, cash 5천만, final_pct 10% → target 1천만.
    # 시트 max_notional 3백만 → 3백만 / 2만 = 150주.
    kis.get_balance.return_value = _fake_portfolio(cash=50_000_000, stock_eval=50_000_000)
    kis.get_snapshot.return_value = _fake_stock(price=20_000)
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_open_snapshot()))
    sheet = _minimal_sheet(final_pct=0.1, max_notional_krw=3_000_000)
    assert await sizer(sheet) == 150


async def test_balance_sizer_clamped_by_max_notional_pct():
    """v2 동등: 자산비례 cap (pct). pct cap 이 target 보다 작을 때 발동."""
    kis = AsyncMock()
    # total 2.1억, cash 1.5억. final_pct 10% → target 2100만.
    # pct 3% = 630만 (target 보다 작음, krw cap 5천만보다 작음) → 630만 / 2만 = 315주
    kis.get_balance.return_value = _fake_portfolio(cash=150_000_000, stock_eval=60_000_000)
    kis.get_snapshot.return_value = _fake_stock(price=20_000)
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_open_snapshot()))
    sheet = _minimal_sheet(final_pct=0.10, max_notional_krw=50_000_000, max_notional_pct=0.03)
    assert await sizer(sheet) == 315


async def test_balance_sizer_pct_cap_loses_to_krw_cap():
    """절대 한도 (max_notional_krw) 가 pct cap 보다 작을 때 절대 한도 발동."""
    kis = AsyncMock()
    # total 5억, cash 5억. final_pct 10% → target 5천만.
    # pct 12% = 6천만, krw cap 50M = 5천만 → krw cap 발동. 5천만 / 2만 = 2500주.
    kis.get_balance.return_value = _fake_portfolio(cash=500_000_000, stock_eval=0)
    kis.get_snapshot.return_value = _fake_stock(price=20_000)
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_open_snapshot()))
    sheet = _minimal_sheet(final_pct=0.10, max_notional_krw=50_000_000, max_notional_pct=0.12)
    assert await sizer(sheet) == 2500


async def test_balance_sizer_pct_cap_none_backward_compat():
    """max_notional_pct=None 이면 기존 동작 (krw cap + cash 만)."""
    kis = AsyncMock()
    kis.get_balance.return_value = _fake_portfolio(cash=50_000_000, stock_eval=50_000_000)
    kis.get_snapshot.return_value = _fake_stock(price=20_000)
    sizer = fast_app.BalanceAwareSizer(kis=kis, system_state=_FakeSystemState(_open_snapshot()))
    sheet = _minimal_sheet(final_pct=0.1, max_notional_krw=3_000_000)  # pct None
    assert await sizer(sheet) == 150


# ─── helpers ─────────────────────────────────────────────


def _fake_portfolio(cash: int, stock_eval: int = 0):
    return type(
        "PF",
        (),
        {
            "cash_balance": cash,
            "total_asset": cash + stock_eval,
            "stock_eval_amount": stock_eval,
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


def _minimal_sheet(
    *,
    final_pct: float = 0.05,
    max_notional_krw: int = 5_000_000,
    max_notional_pct: float | None = None,
) -> PositionSheet:
    now = datetime(2026, 4, 17, 9, 0, 0, tzinfo=KST)
    size_dict: dict = {
        "base_pct": final_pct,
        "macro_multiplier": 1.0,
        "risk_multiplier": 1.0,
        "final_pct": final_pct,
        "max_notional_krw": max_notional_krw,
    }
    if max_notional_pct is not None:
        size_dict["max_notional_pct"] = max_notional_pct
    return PositionSheet(
        sheet_id="ps_20260417_005930_0001",
        generated_at=now,
        valid_until=now + timedelta(hours=6),
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        size=size_dict,
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
