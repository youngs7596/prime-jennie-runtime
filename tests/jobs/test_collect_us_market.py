"""`collect_us_market` 스모크 — Yahoo chart API 모킹 + upsert 검증."""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.market_data import collect_us_market

_YAHOO_URL_RE = r"https://query1\.finance\.yahoo\.com/v8/finance/chart/.*"


def _yahoo_payload(
    timestamps: list[int],
    opens: list[float | None],
    highs: list[float | None],
    lows: list[float | None],
    closes: list[float | None],
    volumes: list[int | None],
) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": volumes,
                            }
                        ]
                    },
                }
            ]
        }
    }


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_collect_us_market_upserts_with_change_pct():
    pool = _FakePool()
    # 모든 5개 티커에 동일 페이로드를 반환 — change_pct 가 두 번째 바부터 채워지는지 검증.
    payload = _yahoo_payload(
        timestamps=[1704153600, 1704240000],  # 2024-01-02, 2024-01-03 UTC
        opens=[100.0, 102.0],
        highs=[101.0, 103.0],
        lows=[99.0, 101.0],
        closes=[100.5, 102.5],
        volumes=[1_000_000, 1_200_000],
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_YAHOO_URL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            await collect_us_market(pool, client, days=30)

    inserts = [c for c in pool.conn.calls if "INSERT INTO us_market_daily" in c[0]]
    # 5 티커 × 2 바 = 10 upsert
    assert len(inserts) == 10

    # 첫 SOX 바는 change_pct=None, 두 번째 SOX 바는 (102.5-100.5)/100.5*100
    sox_calls = [c for c in inserts if c[1][0] == "SOX"]
    assert sox_calls[0][1][7] is None
    expected = round((102.5 - 100.5) / 100.5 * 100, 4)
    assert abs(sox_calls[1][1][7] - expected) < 1e-9


@pytest.mark.asyncio
async def test_collect_us_market_skips_when_all_fail():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_YAHOO_URL_RE).respond(500, json={})
        async with httpx.AsyncClient() as client:
            await collect_us_market(pool, client, days=30)
    assert pool.conn.calls == []
