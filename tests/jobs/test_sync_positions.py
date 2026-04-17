"""`sync_positions` 스모크 — KIS gateway + positions 테이블 fake.

dry_run / apply 양쪽 + compare_positions 5-way 분기 커버.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.positions import (
    compare_positions,
    sync_positions,
)

GATEWAY = "http://kis-gateway:8080"
_BAL_RE = rf"{GATEWAY}/api/balance"


class _FakeRedisPipeline:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)

    async def execute(self) -> None:
        return None


class _FakeRedis:
    def __init__(self) -> None:
        self.pipelines: list[_FakeRedisPipeline] = []

    def pipeline(self) -> _FakeRedisPipeline:
        p = _FakeRedisPipeline()
        self.pipelines.append(p)
        return p


class _FakeConn:
    def __init__(
        self,
        *,
        db_positions: list[dict] | None = None,
        masters: dict[str, dict] | None = None,
    ) -> None:
        self.db_positions = db_positions or []
        self.masters = masters or {}
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[str] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.fetch_calls.append(sql)
        if "FROM positions" in sql:
            return self.db_positions
        return []

    async def fetchrow(self, sql: str, *args: object) -> dict | None:
        self.fetchrow_calls.append((sql, args))
        if "FROM stock_masters" in sql and "sector_naver" in sql:
            row = self.masters.get(args[0])
            return {"sector_naver": row.get("sector_naver")} if row else None
        if "FROM stock_masters" in sql:  # _ensure_stock_master existence check
            return {"1": 1} if args[0] in self.masters else None
        if "FROM positions WHERE stock_code" in sql:
            for p in self.db_positions:
                if p["stock_code"] == args[0]:
                    return p
            return None
        return None

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "OK"

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Tx()


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


def _kis_pos(
    code: str,
    name: str,
    qty: int,
    avg: int,
    *,
    cur: int | None = None,
    total: int | None = None,
) -> dict:
    return {
        "stock_code": code,
        "stock_name": name,
        "quantity": qty,
        "average_buy_price": avg,
        "total_buy_amount": total if total is not None else qty * avg,
        "current_price": cur if cur is not None else avg,
    }


def _bal(positions: list[dict]) -> dict:
    return {
        "positions": positions,
        "cash_balance": 0,
        "total_asset": sum(p["total_buy_amount"] for p in positions),
        "stock_eval_amount": 0,
        "position_count": len(positions),
        "timestamp": datetime.now().isoformat(),
    }


def test_compare_positions_5_way():
    def p(code: str, qty: int, avg: int) -> dict:
        return {
            "stock_code": code,
            "stock_name": f"{code}co",
            "quantity": qty,
            "average_buy_price": avg,
        }

    kis = [p("A", 100, 1000), p("B", 200, 2000), p("C", 50, 5000), p("D", 10, 10_000)]
    db = [
        p("B", 100, 2000),   # quantity_mismatch
        p("C", 50, 4900),    # price_mismatch
        p("D", 10, 10_000),  # matched
        p("E", 20, 3000),    # only_in_db
    ]
    diff = compare_positions(kis, db)
    assert [p["stock_code"] for p in diff["only_in_kis"]] == ["A"]
    assert [p["stock_code"] for p in diff["only_in_db"]] == ["E"]
    assert [m["stock_code"] for m in diff["quantity_mismatch"]] == ["B"]
    assert [m["stock_code"] for m in diff["price_mismatch"]] == ["C"]
    assert diff["matched"] == ["D"]


@pytest.mark.asyncio
async def test_sync_positions_dry_run_returns_diff_no_writes():
    conn = _FakeConn(
        db_positions=[
            {
                "stock_code": "005930",
                "stock_name": "삼성전자",
                "quantity": 100,
                "average_buy_price": 70_000,
                "total_buy_amount": 7_000_000,
                "high_watermark": 70_000,
                "sector_group": "반도체",
            }
        ]
    )
    pool = _FakePool(conn)
    redis_client: Any = _FakeRedis()
    payload = _bal([_kis_pos("005930", "삼성전자", 200, 70_000)])  # qty mismatch

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_BAL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            result = await sync_positions(
                pool, client, redis_client, GATEWAY, dry_run=True
            )

    assert result["dry_run"] is True
    assert result["actions"] == []
    assert len(result["diff"]["quantity_mismatch"]) == 1
    # dry_run 은 INSERT/UPDATE/DELETE 호출 없음
    assert not any(
        "INSERT" in c[0] or "UPDATE" in c[0] or "DELETE" in c[0]
        for c in conn.execute_calls
    )


@pytest.mark.asyncio
async def test_sync_positions_apply_writes_insert_update_delete():
    conn = _FakeConn(
        db_positions=[
            # B: quantity mismatch
            {
                "stock_code": "B",
                "stock_name": "Bco",
                "quantity": 100,
                "average_buy_price": 2000,
                "total_buy_amount": 200_000,
                "high_watermark": 2000,
                "sector_group": "기타",
            },
            # E: only_in_db → DELETE
            {
                "stock_code": "E",
                "stock_name": "Eco",
                "quantity": 20,
                "average_buy_price": 3000,
                "total_buy_amount": 60_000,
                "high_watermark": 3000,
                "sector_group": "기타",
            },
        ],
        masters={"A": {"sector_naver": "반도체"}, "B": {"sector_naver": "반도체"}},
    )
    pool = _FakePool(conn)
    redis_client: Any = _FakeRedis()
    payload = _bal(
        [
            _kis_pos("A", "Aco", 100, 1000, cur=1100),  # only_in_kis → INSERT
            _kis_pos("B", "Bco", 200, 2000),            # qty mismatch → UPDATE
        ]
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_BAL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            result = await sync_positions(
                pool, client, redis_client, GATEWAY, dry_run=False
            )

    assert result["dry_run"] is False
    actions = result["actions"]
    assert any(a.startswith("INSERT A") for a in actions)
    assert any(a.startswith("UPDATE B") for a in actions)
    assert any(a.startswith("DELETE E") for a in actions)

    inserts = [c for c in conn.execute_calls if "INSERT INTO positions" in c[0]]
    deletes = [c for c in conn.execute_calls if "DELETE FROM positions" in c[0]]
    updates = [c for c in conn.execute_calls if "UPDATE positions" in c[0]]
    assert len(inserts) == 1
    assert len(deletes) == 1
    assert len(updates) == 1
    assert inserts[0][1][0] == "A"  # stock_code
    assert deletes[0][1][0] == "E"
    # Redis cleanup invoked for INSERT (only_in_kis)
    assert any("watermark:A" in p.deleted for p in redis_client.pipelines)


@pytest.mark.asyncio
async def test_sync_positions_all_matched_short_circuits():
    conn = _FakeConn(
        db_positions=[
            {
                "stock_code": "005930",
                "stock_name": "삼성전자",
                "quantity": 100,
                "average_buy_price": 70_000,
                "total_buy_amount": 7_000_000,
                "high_watermark": 75_000,
                "sector_group": "반도체",
            }
        ]
    )
    pool = _FakePool(conn)
    redis_client: Any = _FakeRedis()
    payload = _bal([_kis_pos("005930", "삼성전자", 100, 70_000)])

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_BAL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            result = await sync_positions(
                pool, client, redis_client, GATEWAY, dry_run=False
            )

    assert result["actions"] == []
    assert "All positions matched" in result["summary"]
    assert not any(
        "INSERT" in c[0] or "UPDATE" in c[0] or "DELETE" in c[0]
        for c in conn.execute_calls
    )
