"""BacktestPersistence — DB 영속화 path 검증.

* 운영 sheet 와 ``sheet_id`` 충돌 방지 (``bt_`` prefix 강제)
* provenance_json 에 ``is_backtest=true`` + ``backtest_run_id`` 마커
* filled 시트는 outcomes 가 기록되고, 미체결 시트는 outcomes 가 없음
* executions 는 모든 trade (buy + sell) 가 기록됨
* 동일 run 으로 두 번 영속화해도 무해 (ON CONFLICT DO NOTHING / UPDATE)

SQLite (aiosqlite) 인메모리에서 dashboard 와 동일한 단순 스키마로 검증.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from prime_jennie_runtime.backtest import (
    BacktestPersistence,
    DailyBar,
    simulate_sheet,
    to_backtest_sheet_id,
)
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    EntrySection,
    ExitSection,
    FixedSlRule,
    FixedTpRule,
    MacroStateSnapshot,
    PositionSheet,
    ProvenanceSection,
    SizeSection,
    TimeStopRule,
)

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS position_sheets (
        sheet_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL DEFAULT '1.1',
        generated_at TIMESTAMP NOT NULL,
        valid_until TIMESTAMP NOT NULL,
        ticker TEXT NOT NULL,
        strategy_tag TEXT NOT NULL,
        sheet_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS executions (
        execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
        sheet_id TEXT NOT NULL,
        side TEXT NOT NULL,
        price NUMERIC NOT NULL,
        qty INT NOT NULL,
        executed_at TIMESTAMP NOT NULL,
        slippage_bps NUMERIC,
        metadata_json TEXT DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outcomes (
        sheet_id TEXT PRIMARY KEY,
        entry_price NUMERIC NOT NULL,
        exit_price NUMERIC,
        holding_period_s INT,
        pnl_krw NUMERIC,
        pnl_pct NUMERIC,
        exit_reason TEXT,
        closed_at TIMESTAMP,
        metadata_json TEXT DEFAULT '{}'
    )
    """,
]


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        for stmt in _SCHEMA:
            await conn.execute(text(stmt))
    yield eng
    await eng.dispose()


def _mk_sheet(
    *,
    rules: list,
    sheet_id: str = "ps_20260504_090000_a1b2",
    ticker: str = "005930",
) -> PositionSheet:
    generated_at = datetime(2026, 5, 4, 9, 0, tzinfo=KST)
    valid_until = generated_at.replace(hour=15, minute=20)
    return PositionSheet(
        sheet_id=sheet_id,
        schema_version="1.1",
        generated_at=generated_at,
        valid_until=valid_until,
        ticker=ticker,
        side="long",
        strategy_tag="GAP_UP_REBOUND",
        size=SizeSection(
            base_pct=0.05,
            macro_multiplier=1.0,
            risk_multiplier=1.0,
            final_pct=0.05,
            max_notional_krw=5_000_000,
        ),
        entry=EntrySection(
            trigger="market",
            price=None,
            valid_until=valid_until,
            conditions=[],
        ),
        exit=ExitSection(rules=rules),
        provenance=ProvenanceSection(
            scout_run_id="scout_test",
            scout_code_hash="sha256:test",
            scout_hypothesis="test",
            macro_state_snapshot=MacroStateSnapshot(
                gate="open", size_multiplier=1.0, gate_run_id="macro_test"
            ),
            strategy_policy_version="test",
            generated_by="test",
        ),
    )


def _bars(start: date, ohlcv: list[tuple[float, float, float, float]]) -> list[DailyBar]:
    out: list[DailyBar] = []
    d = start
    for o, h, lo, c in ohlcv:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(
            DailyBar(
                ticker="005930",
                price_date=d,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=1_000_000,
            )
        )
        d += timedelta(days=1)
    return out


def test_to_backtest_sheet_id_swaps_prefix():
    assert to_backtest_sheet_id("ps_20260504_090000_a1b2") == "bt_20260504_090000_a1b2"
    # 이미 bt_ 면 그대로
    assert to_backtest_sheet_id("bt_20260504_090000_a1b2") == "bt_20260504_090000_a1b2"


def test_to_backtest_sheet_id_rejects_other_prefix():
    with pytest.raises(ValueError, match="unsupported sheet_id prefix"):
        to_backtest_sheet_id("xx_20260504_090000_a1b2")


async def test_persist_filled_sheet_writes_three_tables(engine: AsyncEngine):
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.10),
            FixedTpRule(pct=0.05),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),
            (10_000, 10_700, 9_900, 10_500),  # tp 트리거
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "fixed_tp"

    persistence = BacktestPersistence(engine, backtest_run_id="bt_run_test_1")
    out = await persistence.persist_one(sheet, result)
    assert out.sheet_id == "bt_20260504_090000_a1b2"
    assert out.persisted
    assert out.n_executions == len(result.trades)

    # 1) position_sheets — bt_ prefix + marker
    async with engine.connect() as conn:
        rows = (
            (await conn.execute(text("SELECT sheet_id, provenance_json FROM position_sheets")))
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["sheet_id"] == "bt_20260504_090000_a1b2"
        prov = json.loads(rows[0]["provenance_json"])
        assert prov["is_backtest"] is True
        assert prov["backtest_run_id"] == "bt_run_test_1"
        assert prov["backtest_original_sheet_id"] == "ps_20260504_090000_a1b2"

        # 2) executions — buy + sell
        exec_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT side, qty FROM executions WHERE sheet_id = :sid "
                        "ORDER BY execution_id"
                    ),
                    {"sid": out.sheet_id},
                )
            )
            .mappings()
            .all()
        )
        sides = [r["side"] for r in exec_rows]
        assert "buy" in sides
        assert "sell" in sides

        # 3) outcomes
        outcome_rows = (
            (
                await conn.execute(
                    text("SELECT pnl_pct, exit_reason FROM outcomes WHERE sheet_id = :sid"),
                    {"sid": out.sheet_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["exit_reason"] == "fixed_tp"
        assert outcome_rows[0]["pnl_pct"] is not None


async def test_persist_unfilled_skips_outcomes(engine: AsyncEngine):
    """미체결 시트는 outcomes 미기록 — 운영 path 일관."""
    sheet = _mk_sheet(
        rules=[FixedSlRule(pct=0.05), TimeStopRule(mode="eod")],
    )
    # entry 없도록 빈 bars 1개에 limit unfilled 시나리오 흉내 — 여기선 그냥 trades=0 이도록
    # limit 으로 만들고 limit 미체결로 강제하는 게 가장 깔끔
    sheet = _mk_sheet(
        rules=[FixedSlRule(pct=0.05), TimeStopRule(mode="eod")],
    )
    # entry=limit, 가격을 0 으로 두면 검증 실패 → market 으로 변경 후 빈 bars 처리
    # 가장 단순: bars 가 없으면 unfilled (no_bars). entry_index=1, bars 1개 → entry_index out
    bars = _bars(date(2026, 5, 4), [(10_000, 10_100, 9_900, 10_000)])
    result = simulate_sheet(sheet, bars, entry_index=99, capital_krw=1_000_000)
    assert not result.filled

    persistence = BacktestPersistence(engine, backtest_run_id="bt_run_unfilled")
    out = await persistence.persist_one(sheet, result)
    assert not out.persisted
    assert out.n_executions == 0

    async with engine.connect() as conn:
        n_outcomes = (await conn.execute(text("SELECT COUNT(*) AS c FROM outcomes"))).scalar_one()
        assert n_outcomes == 0
        n_sheets = (
            await conn.execute(text("SELECT COUNT(*) AS c FROM position_sheets"))
        ).scalar_one()
        # 시트는 영속화 — meta 평가가 unfilled 사유도 본다
        assert n_sheets == 1


async def test_persist_twice_is_idempotent(engine: AsyncEngine):
    """동일 run 으로 두 번 호출해도 PK 충돌이 raise 되지 않음."""
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.10),
            FixedTpRule(pct=0.05),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),
            (10_000, 10_700, 9_900, 10_500),
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    persistence = BacktestPersistence(engine, backtest_run_id="bt_run_idem")

    await persistence.persist_one(sheet, result)
    # 두 번째 호출 — 시트는 INSERT OR IGNORE, outcome 은 INSERT OR REPLACE
    await persistence.persist_one(sheet, result)

    async with engine.connect() as conn:
        n_outcomes = (await conn.execute(text("SELECT COUNT(*) AS c FROM outcomes"))).scalar_one()
        assert n_outcomes == 1
        n_sheets = (
            await conn.execute(text("SELECT COUNT(*) AS c FROM position_sheets"))
        ).scalar_one()
        assert n_sheets == 1


async def test_new_run_id_format():
    rid = BacktestPersistence.new_run_id()
    assert rid.startswith("bt_run_")
    # 길이는 prefix + ISO + uuid8 — 단순 sanity
    assert len(rid) > len("bt_run_")
