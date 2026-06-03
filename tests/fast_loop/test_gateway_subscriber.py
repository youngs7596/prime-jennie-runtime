"""fast_loop.gateway_subscriber 스모크."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from prime_jennie_runtime.fast_loop.gateway_subscriber import (
    KIS_WS_SUBSCRIPTION_LIMIT,
    load_subscription_codes,
    subscribe_on_startup,
)

GATEWAY = "http://kis-gateway:8080"
_SUB_RE = rf"{GATEWAY}/api/realtime/subscribe"


class _FakeConn:
    def __init__(
        self,
        positions: list[str],
        pending_sheet_codes: list[str],
    ) -> None:
        self._positions = positions
        # 측정 대기 시트 ticker — SQL 이 최신순 정렬로 주는 것을 그대로 흉내.
        self._pending_sheet_codes = pending_sheet_codes

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM positions" in sql:
            return [{"stock_code": c} for c in self._positions]
        if "FROM position_sheets" in sql:
            return [{"stock_code": c} for c in self._pending_sheet_codes]
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> Any:
        raise AssertionError(f"unexpected SQL: {sql}")


class _FakePool:
    def __init__(
        self,
        *,
        positions: list[str] | None = None,
        pending_sheets: list[str] | None = None,
    ) -> None:
        self.conn = _FakeConn(positions or [], pending_sheets or [])

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_load_subscription_codes_union_and_sorted():
    pool = _FakePool(
        positions=["005930", "000660"],
        pending_sheets=["005930", "035720", "068270"],
    )
    codes = await load_subscription_codes(pool)
    assert codes == ["000660", "005930", "035720", "068270"]


@pytest.mark.asyncio
async def test_load_subscription_codes_empty_everything():
    pool = _FakePool(positions=[], pending_sheets=[])
    codes = await load_subscription_codes(pool)
    assert codes == []


@pytest.mark.asyncio
async def test_load_subscription_codes_positions_only_when_no_sheets():
    pool = _FakePool(positions=["005930"], pending_sheets=[])
    codes = await load_subscription_codes(pool)
    assert codes == ["005930"]


@pytest.mark.asyncio
async def test_load_subscription_codes_caps_at_ws_limit():
    """KIS WebSocket 등록 한도 (41) 초과 시 positions 우선 + 최신 시트순으로 자른다."""
    positions = [f"P{i:05d}" for i in range(3)]
    # 시트 ticker 50개 — SQL 정렬 (최신순) 그대로 들어온다고 가정.
    sheets = [f"S{i:05d}" for i in range(50)]
    pool = _FakePool(positions=positions, pending_sheets=sheets)

    codes = await load_subscription_codes(pool)

    assert len(codes) == KIS_WS_SUBSCRIPTION_LIMIT
    # positions 전체 포함.
    for p in positions:
        assert p in codes
    # 시트는 최신순 (리스트 앞쪽) 우선 — 한도에 밀려난 뒤쪽은 제외.
    assert "S00000" in codes
    assert "S00049" not in codes


@pytest.mark.asyncio
async def test_subscribe_on_startup_posts_codes():
    pool = _FakePool(
        positions=["005930"],
        pending_sheets=["035720"],
    )
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url__regex=_SUB_RE).respond(
            200,
            json={"added": ["005930", "035720"], "total_subscriptions": 2, "is_running": True},
        )
        result = await subscribe_on_startup(pool, GATEWAY)

    assert route.call_count == 1
    sent = route.calls[0].request
    body = sent.content.decode()
    assert "005930" in body and "035720" in body
    assert result["codes"] == ["005930", "035720"]
    assert result["response"]["is_running"] is True


@pytest.mark.asyncio
async def test_subscribe_on_startup_skips_when_no_codes():
    pool = _FakePool(positions=[], pending_sheets=[])
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=_SUB_RE)
        result = await subscribe_on_startup(pool, GATEWAY)

    assert route.call_count == 0
    assert result == {"codes": [], "skipped": True}


@pytest.mark.asyncio
async def test_subscribe_on_startup_swallows_gateway_error():
    """gateway 가 다운되어 있어도 fast_loop 기동을 막지 않아야 함."""
    pool = _FakePool(positions=["005930"], pending_sheets=[])
    with respx.mock(assert_all_called=True) as mock:
        mock.post(url__regex=_SUB_RE).mock(side_effect=httpx.ConnectError("refused"))
        result = await subscribe_on_startup(pool, GATEWAY)

    assert result["codes"] == ["005930"]
    assert "error" in result
