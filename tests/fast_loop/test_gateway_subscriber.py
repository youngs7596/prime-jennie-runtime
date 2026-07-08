"""fast_loop.gateway_subscriber 스모크."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from prime_jennie_runtime.fast_loop.gateway_subscriber import (
    MAX_SUBSCRIPTION_CODES,
    ensure_subscribed,
    load_subscription_codes,
    run_subscription_maintainer,
    subscribe_on_startup,
)

GATEWAY = "http://kis-gateway:8080"
_SUB_RE = rf"{GATEWAY}/api/realtime/subscribe"
_STATUS_RE = rf"{GATEWAY}/api/realtime/status"


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
    """체결+호가 두 채널이라 실효 종목 한도(20) 초과 시 positions 우선 + 최신 시트순."""
    positions = [f"P{i:05d}" for i in range(3)]
    # 시트 ticker 50개 — SQL 정렬 (최신순) 그대로 들어온다고 가정.
    sheets = [f"S{i:05d}" for i in range(50)]
    pool = _FakePool(positions=positions, pending_sheets=sheets)

    codes = await load_subscription_codes(pool)

    assert len(codes) == MAX_SUBSCRIPTION_CODES
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


@pytest.mark.asyncio
async def test_ensure_subscribed_noop_when_running_and_complete():
    """streamer 가 살아있고 원하는 종목이 다 구독돼 있으면 재구독하지 않는다."""
    pool = _FakePool(positions=["005930"], pending_sheets=["035720"])
    with respx.mock(assert_all_called=True) as mock:
        status = mock.get(url__regex=_STATUS_RE).respond(
            200, json={"is_running": True, "codes": ["005930", "035720"]}
        )
        result = await ensure_subscribed(pool, GATEWAY)

    assert status.call_count == 1
    assert result == {"codes": ["005930", "035720"], "noop": True}


@pytest.mark.asyncio
async def test_ensure_subscribed_resubscribes_when_not_running():
    """streamer 가 죽어 있으면(is_running False) 재구독한다."""
    pool = _FakePool(positions=["005930"], pending_sheets=[])
    with respx.mock(assert_all_called=True) as mock:
        mock.get(url__regex=_STATUS_RE).respond(200, json={"is_running": False, "codes": []})
        sub = mock.post(url__regex=_SUB_RE).respond(
            200, json={"added": ["005930"], "total_subscriptions": 1, "is_running": True}
        )
        result = await ensure_subscribed(pool, GATEWAY)

    assert sub.call_count == 1
    assert result["response"]["is_running"] is True


@pytest.mark.asyncio
async def test_ensure_subscribed_resubscribes_when_codes_missing():
    """running 이어도 원하는 종목이 구독에서 빠져 있으면 재구독한다."""
    pool = _FakePool(positions=["005930", "000660"], pending_sheets=[])
    with respx.mock(assert_all_called=True) as mock:
        # 000660 이 구독 목록에서 빠졌다.
        mock.get(url__regex=_STATUS_RE).respond(200, json={"is_running": True, "codes": ["005930"]})
        sub = mock.post(url__regex=_SUB_RE).respond(
            200, json={"added": ["000660"], "total_subscriptions": 2, "is_running": True}
        )
        result = await ensure_subscribed(pool, GATEWAY)

    assert sub.call_count == 1
    body = sub.calls[0].request.content.decode()
    assert "000660" in body and "005930" in body
    assert result["response"]["total_subscriptions"] == 2


@pytest.mark.asyncio
async def test_ensure_subscribed_resubscribes_when_status_unreachable():
    """status 조회가 실패(게이트웨이 미준비)해도 일단 구독을 시도한다."""
    pool = _FakePool(positions=["005930"], pending_sheets=[])
    with respx.mock(assert_all_called=True) as mock:
        mock.get(url__regex=_STATUS_RE).mock(side_effect=httpx.ConnectError("refused"))
        sub = mock.post(url__regex=_SUB_RE).respond(
            200, json={"added": ["005930"], "total_subscriptions": 1, "is_running": True}
        )
        result = await ensure_subscribed(pool, GATEWAY)

    assert sub.call_count == 1
    assert result["response"]["is_running"] is True


@pytest.mark.asyncio
async def test_ensure_subscribed_skips_when_no_codes():
    """구독 대상이 없으면 status 조회도 subscribe 도 하지 않는다."""
    pool = _FakePool(positions=[], pending_sheets=[])
    with respx.mock(assert_all_called=False) as mock:
        status = mock.get(url__regex=_STATUS_RE)
        sub = mock.post(url__regex=_SUB_RE)
        result = await ensure_subscribed(pool, GATEWAY)

    assert status.call_count == 0
    assert sub.call_count == 0
    assert result == {"codes": [], "skipped": True}


@pytest.mark.asyncio
async def test_run_subscription_maintainer_ensures_at_least_once_then_stops():
    """기동 직후 1회 보증(do-while) 후 stop_event 로 종료한다."""
    pool = _FakePool(positions=["005930"], pending_sheets=[])
    stop_event = asyncio.Event()
    stop_event.set()  # 첫 iteration 을 돌고 곧장 종료
    with respx.mock(assert_all_called=True) as mock:
        status = mock.get(url__regex=_STATUS_RE).respond(
            200, json={"is_running": True, "codes": ["005930"]}
        )
        await asyncio.wait_for(
            run_subscription_maintainer(pool, GATEWAY, interval_sec=0.01, stop_event=stop_event),
            timeout=1.0,
        )

    assert status.call_count == 1  # stop 이 미리 걸려 있어도 1회는 실행
