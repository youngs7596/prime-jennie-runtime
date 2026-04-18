"""scout.market_data_loader — SQL / records round-trip 테스트.

tests/slow_loop/test_persistence_candidates.py 의 _FakeEngine 패턴을 따른다.
실 Postgres 통합은 별도 integration test 에서.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest

from prime_jennie_runtime.slow_loop.scout.market_data_loader import (
    load_market_data_records,
)

# --- fake engine (mappings().all() 지원) ------------------------------


@dataclass
class _FakeMappings:
    rows: list[dict[str, Any]]

    def all(self) -> list[dict[str, Any]]:
        return list(self.rows)


@dataclass
class _FakeResult:
    rows: list[dict[str, Any]]

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self.rows)


@dataclass
class _FakeConn:
    rows: list[dict[str, Any]] = field(default_factory=list)
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


# --- tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_none_returns_empty():
    records = await load_market_data_records(
        None, universe=["005930"], as_of=date(2026, 4, 18), lookback_days=60
    )
    assert records == []


@pytest.mark.asyncio
async def test_empty_universe_returns_empty():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    records = await load_market_data_records(
        engine,  # type: ignore[arg-type]
        universe=[],
        as_of=date(2026, 4, 18),
        lookback_days=60,
    )
    assert records == []
    # SQL 을 아예 때리지 않아야 함 (빈 universe)
    assert conn.executed == []


@pytest.mark.asyncio
async def test_passes_universe_and_lookback_and_as_of_params():
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    await load_market_data_records(
        engine,  # type: ignore[arg-type]
        universe=["005930", "000660"],
        as_of=date(2026, 4, 18),
        lookback_days=30,
    )
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    # LATERAL 구조 + unnest(TEXT[]) 파라미터 바인딩 확인
    assert "LATERAL" in sql
    assert "daily_prices" in sql
    assert "unnest" in sql.lower()
    assert params["tickers"] == ["005930", "000660"]
    assert params["as_of"] == date(2026, 4, 18)
    assert params["lookback"] == 30


@pytest.mark.asyncio
async def test_maps_rows_to_record_dicts():
    conn = _FakeConn(
        rows=[
            {
                "ticker": "005930",
                "price_date": date(2026, 4, 17),
                "open": 71000,
                "high": 71500,
                "low": 70800,
                "close": 71200,
                "volume": 1_200_000,
            },
            {
                "ticker": "005930",
                "price_date": date(2026, 4, 18),
                "open": 71200,
                "high": 72000,
                "low": 71100,
                "close": 71800,
                "volume": 1_500_000,
            },
        ]
    )
    engine = _FakeEngine(conn)
    records = await load_market_data_records(
        engine,  # type: ignore[arg-type]
        universe=["005930"],
        as_of=date(2026, 4, 18),
        lookback_days=60,
    )
    assert len(records) == 2
    first = records[0]
    assert first["ticker"] == "005930"
    assert first["date"] == "2026-04-17"  # date → ISO string
    assert first["open"] == 71000
    assert first["close"] == 71200
    assert first["volume"] == 1_200_000
    # 모든 record 가 JSON 직렬화 가능한 scalar 필드만 포함
    for rec in records:
        for v in rec.values():
            assert isinstance(v, str | int)
