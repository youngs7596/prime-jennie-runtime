"""paper_outcomes simulator v0 — 일봉 simulator 기본 시나리오."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from prime_jennie_runtime.jobs.paper_outcomes import (
    measure_paper_outcomes,
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

# =====================================================================
# Fixtures / Helpers
# =====================================================================


def _make_sheet(
    *,
    sheet_id: str,
    ticker: str = "005930",
    generated_at: datetime | None = None,
    fixed_sl_pct: float = 0.05,
    fixed_tp_pct: float = 0.08,
    time_stop_days: int = 5,
) -> PositionSheet:
    gen = generated_at or datetime(2026, 5, 20, 11, 0, tzinfo=KST)
    return PositionSheet(
        sheet_id=sheet_id,
        generated_at=gen,
        valid_until=gen.replace(hour=15, minute=30),
        ticker=ticker,
        strategy_tag="GAP_UP_REBOUND",
        size=SizeSection(
            base_pct=0.02,
            macro_multiplier=1.0,
            risk_multiplier=1.0,
            final_pct=0.02,
            max_notional_krw=1_000_000,
        ),
        entry=EntrySection(
            trigger="market",
            price=None,
            valid_until=gen.replace(hour=15, minute=0),
        ),
        exit=ExitSection(
            rules=[
                FixedSlRule(pct=fixed_sl_pct),
                FixedTpRule(pct=fixed_tp_pct),
                TimeStopRule(mode="hold_days", value=time_stop_days),
            ],
        ),
        provenance=ProvenanceSection(
            scout_run_id="scout_20260520_1100",
            scout_code_hash="a" * 64,
            scout_hypothesis="t",
            macro_state_snapshot=MacroStateSnapshot(
                gate="open", size_multiplier=1.0, gate_run_id="macro_20260520_0800"
            ),
            strategy_policy_version="v3.1",
            generated_by="strategy_engine@deterministic",
        ),
    )


class _FakeConn:
    """SQL 패턴별로 분기해 응답하는 fake asyncpg 연결.

    paper_outcomes 가 호출하는 5가지 쿼리:
      - fetchval: daily_prices entry close 조회
      - fetch: daily_prices 범위 조회
      - fetchrow: business days +/- offset
      - fetch (pending sheets) — paper_outcomes JOIN
      - execute (upsert)
    """

    def __init__(
        self,
        *,
        daily: dict[date, dict],
        sheets_pending: list[dict],
        fail_upsert_for: set[str] | None = None,
    ) -> None:
        # daily: price_date -> {open, high, low, close}
        self.daily = daily
        self.sheets_pending = sheets_pending
        self.upserts: list[tuple] = []
        # 특정 sheet_id 의 upsert 가 제약 위반 등으로 터지는 상황 재현용.
        self.fail_upsert_for = fail_upsert_for or set()

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        if "FROM position_sheets ps" in sql:
            return self.sheets_pending
        if "FROM daily_prices" in sql and "BETWEEN" in sql:
            _ticker, start, end = args
            out = []
            for d in sorted(self.daily.keys()):
                if start <= d <= end:
                    out.append({"price_date": d, **self.daily[d]})
            return out
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "SELECT close_price FROM daily_prices" in sql:
            _ticker, d = args
            row = self.daily.get(d)
            return row["close_price"] if row else None
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        if "WHERE price_date > $1" in sql:
            base, offset = args
            after = sorted(d for d in self.daily if d > base)
            idx = offset
            if idx < len(after):
                return {"price_date": after[idx]}
            return None
        if "WHERE price_date < $1" in sql:
            base, offset = args
            before = sorted((d for d in self.daily if d < base), reverse=True)
            idx = offset
            if idx < len(before):
                return {"price_date": before[idx]}
            return None
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO paper_outcomes" in sql:
            sheet_id = args[0]
            if sheet_id in self.fail_upsert_for:
                raise RuntimeError(f"simulated upsert constraint violation: {sheet_id}")
            self.upserts.append(args)
        return "OK"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _ohlc(close: int, *, open_=None, high=None, low=None) -> dict:
    return {
        "open_price": open_ if open_ is not None else close,
        "high_price": high if high is not None else close,
        "low_price": low if low is not None else close,
        "close_price": close,
    }


def _daily_series(start: date, prices: list[int]) -> dict[date, dict]:
    """간단 series — 거래일 연속 (주말 skip 안 함; 테스트라 단순화)."""
    out: dict[date, dict] = {}
    d = start
    for p in prices:
        out[d] = _ohlc(p)
        d += timedelta(days=1)
    return out


# =====================================================================
# Tests
# =====================================================================


@pytest.mark.asyncio
async def test_fixed_tp_hit_on_high():
    """5 거래일 윈도우 — 3 일째 high 가 +10% 도달, fixed_tp 8% 조건 hit."""
    publish = date(2026, 5, 20)
    sheet = _make_sheet(
        sheet_id="ps_20260520_110000_aaaa",
        generated_at=datetime(2026, 5, 20, 11, 0, tzinfo=KST),
        fixed_sl_pct=0.05,
        fixed_tp_pct=0.08,
        time_stop_days=5,
    )

    # entry close 100. day+3 high=110 에서 tp hit.
    daily = {
        publish: _ohlc(100),
        publish + timedelta(days=1): _ohlc(102, low=99, high=103),
        publish + timedelta(days=2): _ohlc(104, low=101, high=105),
        publish + timedelta(days=3): _ohlc(108, low=103, high=110),  # tp hit
        publish + timedelta(days=4): _ohlc(105),
        publish + timedelta(days=5): _ohlc(106),
        publish + timedelta(days=6): _ohlc(107),  # window margin
    }

    conn = _FakeConn(
        daily=daily,
        sheets_pending=[
            {
                "sheet_id": sheet.sheet_id,
                "sheet_json": sheet.model_dump_json(),
                "generated_at": sheet.generated_at,
            }
        ],
    )
    pool = _FakePool(conn)

    stats = await measure_paper_outcomes(pool, today=publish + timedelta(days=7))

    assert stats["measured"] == 1
    assert stats["data_missing"] == 0
    assert len(conn.upserts) == 1
    args = conn.upserts[0]
    # sheet_id, simulator_version, entry_model, coverage,
    # entry_date, entry_price, exit_date, exit_price,
    # exit_reason, holding_days, pnl_pct, ...
    assert args[0] == sheet.sheet_id
    assert args[1] == "v0"
    assert args[3] == "daily_only"
    assert args[5] == 100.0
    assert args[8] == "fixed_tp"
    # day+3 의 4-tick 순서가 [open=108, high=110, low=103, close=108]. open 이
    # 첫 tick 인데 current_return = (108-100)/100 = 0.08 이 이미 fixed_tp 0.08 을
    # 만족 → open tick 에서 매칭. high(110) 까지 가지 않음.
    assert args[7] == 108.0
    assert args[10] == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_fixed_sl_hit_before_tp():
    """fixed_sl 우선 매칭 — 같은 날 high 가 tp 도달이지만 low 가 sl 먼저.

    evaluate 의 first_match 순서가 [fixed_sl, fixed_tp, time_stop] 이라 sl 이 먼저.
    그러나 같은 거래일 안 tick 시퀀스는 [open, high, low, close] — high 가 tp 먼저
    매칭되면 그 tick 에서 청산. 그래서 이 테스트는 sl 만 발화하는 케이스로 분리.
    """
    publish = date(2026, 5, 20)
    sheet = _make_sheet(
        sheet_id="ps_20260520_110000_bbbb",
        generated_at=datetime(2026, 5, 20, 11, 0, tzinfo=KST),
        fixed_sl_pct=0.05,
        fixed_tp_pct=0.20,  # 도달 안 함
        time_stop_days=5,
    )

    daily = {
        publish: _ohlc(100),
        publish + timedelta(days=1): _ohlc(98, low=94, high=99),  # sl hit at 94 (-6%)
        publish + timedelta(days=2): _ohlc(95),
        publish + timedelta(days=3): _ohlc(96),
        publish + timedelta(days=4): _ohlc(97),
        publish + timedelta(days=5): _ohlc(98),
        publish + timedelta(days=6): _ohlc(99),
    }

    conn = _FakeConn(
        daily=daily,
        sheets_pending=[
            {
                "sheet_id": sheet.sheet_id,
                "sheet_json": sheet.model_dump_json(),
                "generated_at": sheet.generated_at,
            }
        ],
    )
    pool = _FakePool(conn)

    stats = await measure_paper_outcomes(pool, today=publish + timedelta(days=7))

    assert stats["measured"] == 1
    args = conn.upserts[0]
    assert args[8] == "fixed_sl"
    assert args[7] == 94.0  # low tick


@pytest.mark.asyncio
async def test_window_expired_no_rule_match():
    """모든 룰 미발화 — 5 일 후 window_expired, 마지막 종가로 close."""
    publish = date(2026, 5, 20)
    sheet = _make_sheet(
        sheet_id="ps_20260520_110000_cccc",
        generated_at=datetime(2026, 5, 20, 11, 0, tzinfo=KST),
        fixed_sl_pct=0.05,  # schema max 0.10. 진폭이 더 작아 발화 안 함.
        fixed_tp_pct=0.20,  # 도달 안 함
        time_stop_days=5,
    )

    # 진폭 작아서 sl(threshold 95)/tp(threshold 120) 둘 다 미발화. 5 일째 마지막
    # tick (15:30 cutoff) 에서 time_stop hit, 그 날 close 103 으로 청산.
    daily = {
        publish: _ohlc(100),
        publish + timedelta(days=1): _ohlc(101, low=99, high=102),
        publish + timedelta(days=2): _ohlc(102, low=100, high=103),
        publish + timedelta(days=3): _ohlc(101),
        publish + timedelta(days=4): _ohlc(102),
        publish + timedelta(days=5): _ohlc(103),
        publish + timedelta(days=6): _ohlc(104),  # margin
    }

    conn = _FakeConn(
        daily=daily,
        sheets_pending=[
            {
                "sheet_id": sheet.sheet_id,
                "sheet_json": sheet.model_dump_json(),
                "generated_at": sheet.generated_at,
            }
        ],
    )
    pool = _FakePool(conn)

    stats = await measure_paper_outcomes(pool, today=publish + timedelta(days=7))

    assert stats["measured"] == 1
    args = conn.upserts[0]
    # time_stop hold_days=5 → 5 거래일째 마지막 tick (15:30) cutoff 통과 → time_stop hit.
    # 5 일째 종가 103 으로 close.
    assert args[8] == "time_stop"
    assert args[7] == 103.0


@pytest.mark.asyncio
async def test_data_missing_when_entry_close_absent():
    """발행일 종가 없으면 data_missing 으로 즉시 기록."""
    publish = date(2026, 5, 20)
    sheet = _make_sheet(
        sheet_id="ps_20260520_110000_dddd",
        generated_at=datetime(2026, 5, 20, 11, 0, tzinfo=KST),
    )

    # 발행일 데이터 없음 — 이후 일봉만 존재.
    daily = {
        publish + timedelta(days=1): _ohlc(102),
        publish + timedelta(days=2): _ohlc(103),
        publish + timedelta(days=3): _ohlc(104),
        publish + timedelta(days=5): _ohlc(105),
        publish + timedelta(days=7): _ohlc(106),
    }

    conn = _FakeConn(
        daily=daily,
        sheets_pending=[
            {
                "sheet_id": sheet.sheet_id,
                "sheet_json": sheet.model_dump_json(),
                "generated_at": sheet.generated_at,
            }
        ],
    )
    pool = _FakePool(conn)

    stats = await measure_paper_outcomes(pool, today=publish + timedelta(days=10))

    assert stats["measured"] == 1
    assert stats["data_missing"] == 1
    args = conn.upserts[0]
    assert args[8] == "data_missing"
    assert args[5] is None  # entry_price 없음
    assert args[10] is None  # pnl_pct 없음
    # metadata 에 reason 기록
    meta = json.loads(args[13])
    assert meta["reason"] == "entry_close_missing"


@pytest.mark.asyncio
async def test_pending_filter_skips_bt_sheets():
    """sheet_id LIKE 'ps_%' 필터로 backtest 시트 (bt_) 는 측정 대상 아님."""
    publish = date(2026, 5, 20)
    sheet = _make_sheet(
        sheet_id="bt_20260520_110000_eeee",  # bt_ 접두
        generated_at=datetime(2026, 5, 20, 11, 0, tzinfo=KST),
    )

    conn = _FakeConn(daily={publish: _ohlc(100)}, sheets_pending=[])
    pool = _FakePool(conn)

    # SQL 단계에서 LIKE 'ps_%' 가 막아 sheets_pending 이 비어 있는 상태로 들어와도
    # measure 가 0건으로 정상 종료해야 함.
    _ = sheet  # 발행됐다고 가정만, fake pool 은 빈 응답을 돌려준다
    stats = await measure_paper_outcomes(pool, today=publish + timedelta(days=10))

    assert stats["candidates"] == 0
    assert stats["measured"] == 0
    assert len(conn.upserts) == 0


@pytest.mark.asyncio
async def test_upsert_failure_isolated_per_sheet():
    """한 시트의 upsert 가 터져도 나머지 시트는 측정된다 — 배치 전체 롤백 방지.

    운영에서 entry_price NOT NULL 제약이 data_missing 행을 거부했을 때, upsert
    호출이 격리돼 있지 않아 첫 실패가 잡 전체를 죽이고 멀쩡한 시트까지 못 들어갔다
    (paper_outcomes 0건 사고). 격리 후엔 실패 건만 errors 로 세고 나머지는 측정된다.
    """
    publish = date(2026, 5, 20)
    daily = {
        publish: _ohlc(100),
        publish + timedelta(days=1): _ohlc(102),
        publish + timedelta(days=2): _ohlc(103),
        publish + timedelta(days=3): _ohlc(104),
        publish + timedelta(days=4): _ohlc(105),
        publish + timedelta(days=5): _ohlc(106),
        publish + timedelta(days=6): _ohlc(107),
    }
    s_fail = _make_sheet(
        sheet_id="ps_20260520_110000_fa01",
        generated_at=datetime(2026, 5, 20, 11, 0, tzinfo=KST),
    )
    s_ok = _make_sheet(
        sheet_id="ps_20260520_120000_fa02",
        generated_at=datetime(2026, 5, 20, 12, 0, tzinfo=KST),
    )
    conn = _FakeConn(
        daily=daily,
        sheets_pending=[
            {
                "sheet_id": s_fail.sheet_id,
                "sheet_json": s_fail.model_dump_json(),
                "generated_at": s_fail.generated_at,
            },
            {
                "sheet_id": s_ok.sheet_id,
                "sheet_json": s_ok.model_dump_json(),
                "generated_at": s_ok.generated_at,
            },
        ],
        fail_upsert_for={s_fail.sheet_id},
    )
    pool = _FakePool(conn)

    stats = await measure_paper_outcomes(pool, today=publish + timedelta(days=10))

    # 첫 시트는 upsert 실패 → errors, 둘째는 정상 측정. 배치가 안 죽는다.
    assert stats["candidates"] == 2
    assert stats["errors"] == 1
    assert stats["measured"] == 1
    # 성공한 시트만 적재됐다.
    assert len(conn.upserts) == 1
    assert conn.upserts[0][0] == s_ok.sheet_id
