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
_UNSUB_RE = rf"{GATEWAY}/api/realtime/unsubscribe"
_STATUS_RE = rf"{GATEWAY}/api/realtime/status"


class _FakeConn:
    def __init__(
        self,
        positions: list[str],
        pending_sheet_codes: list[str],
        top_turnover: list[str],
    ) -> None:
        self._positions = positions
        # 측정 대기 시트 ticker — SQL 이 최신순 정렬로 주는 것을 그대로 흉내.
        self._pending_sheet_codes = pending_sheet_codes
        # 거래대금 상위 — SQL 이 내림차순으로 주는 것을 그대로 흉내.
        self._top_turnover = top_turnover
        self.turnover_queried = False

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM positions" in sql:
            return [{"stock_code": c} for c in self._positions]
        if "FROM position_sheets" in sql:
            return [{"stock_code": c} for c in self._pending_sheet_codes]
        if "FROM daily_prices" in sql:
            self.turnover_queried = True
            limit = int(args[0]) if args else len(self._top_turnover)
            return [{"stock_code": c} for c in self._top_turnover[:limit]]
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> Any:
        raise AssertionError(f"unexpected SQL: {sql}")


class _FakePool:
    def __init__(
        self,
        *,
        positions: list[str] | None = None,
        pending_sheets: list[str] | None = None,
        top_turnover: list[str] | None = None,
    ) -> None:
        self.conn = _FakeConn(positions or [], pending_sheets or [], top_turnover or [])

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
async def test_fills_empty_slots_with_top_turnover():
    """보유·시트가 적으면 남는 자리를 거래대금 상위로 채운다 (2026-08-05).

    실보유 0 · 시트 서너 종목이던 시기에 20자리 중 9개만 쓰고 있었다. 체결·호가는
    지나가면 못 받는 데이터라 빈자리가 곧 영구 손실이다.
    """
    turnover = [f"T{i:05d}" for i in range(40)]
    pool = _FakePool(positions=["005930"], pending_sheets=["035720"], top_turnover=turnover)

    codes = await load_subscription_codes(pool)

    assert len(codes) == MAX_SUBSCRIPTION_CODES
    assert "005930" in codes
    assert "035720" in codes
    # 거래대금 상위부터 채운다 — 뒤쪽은 자리가 없어 안 들어온다.
    assert "T00000" in codes
    assert "T00039" not in codes


@pytest.mark.asyncio
async def test_top_turnover_never_displaces_positions_or_sheets():
    """채움 종목은 우선순위 맨 뒤 — 보유·시트가 한도를 채우면 하나도 안 들어온다."""
    positions = [f"P{i:05d}" for i in range(5)]
    sheets = [f"S{i:05d}" for i in range(MAX_SUBSCRIPTION_CODES)]
    turnover = [f"T{i:05d}" for i in range(40)]
    pool = _FakePool(positions=positions, pending_sheets=sheets, top_turnover=turnover)

    codes = await load_subscription_codes(pool)

    assert len(codes) == MAX_SUBSCRIPTION_CODES
    assert not [c for c in codes if c.startswith("T")]
    for p in positions:
        assert p in codes
    # 한도가 이미 찼으면 거래대금 조회 자체를 건너뛴다.
    assert pool.conn.turnover_queried is False


@pytest.mark.asyncio
async def test_duplicate_between_sheet_and_turnover_counts_once():
    turnover = ["005930"] + [f"T{i:05d}" for i in range(40)]
    pool = _FakePool(positions=["005930"], pending_sheets=[], top_turnover=turnover)

    codes = await load_subscription_codes(pool)

    assert len(codes) == len(set(codes)) == MAX_SUBSCRIPTION_CODES


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
    assert result == {"codes": ["005930", "035720"], "noop": True, "dropped": []}


@pytest.mark.asyncio
async def test_ensure_subscribed_drops_stale_codes_only_over_limit():
    """구독 요청이 더하기만 하므로, 한도를 넘길 때만 대상 밖 종목을 덜어낸다.

    2026-08-05 이전엔 시트가 바뀌어도 옛 종목이 해제되지 않아 게이트웨이가 살아있는
    동안 등록이 계속 쌓였다. 빈자리 채움을 넣으면 대상이 날마다 바뀌므로 KIS 한도(41건)를
    넘기게 된다.
    """
    # 원하는 종목 20개 중 1개만 이미 구독돼 있고, 대상 밖 옛 종목 20개가 남아 있다.
    turnover = [f"T{i:05d}" for i in range(40)]
    pool = _FakePool(positions=["005930"], pending_sheets=[], top_turnover=turnover)
    stale = [f"OLD{i:03d}" for i in range(20)]

    with respx.mock(assert_all_called=True) as mock:
        mock.get(url__regex=_STATUS_RE).respond(
            200, json={"is_running": True, "codes": stale + ["005930"]}
        )
        unsub = mock.post(url__regex=_UNSUB_RE).respond(
            200, json={"removed": [], "total_subscriptions": 1, "is_running": True}
        )
        sub = mock.post(url__regex=_SUB_RE).respond(
            200, json={"added": [], "total_subscriptions": 20, "is_running": True}
        )
        result = await ensure_subscribed(pool, GATEWAY)

    assert unsub.call_count == 1
    assert sub.call_count == 1
    # 구독 21개 + 새로 붙일 19개 = 40 → 한도 20 을 20개 초과하므로 옛 종목 20개 전부 해제.
    assert len(result["dropped"]) == 20
    assert all(c.startswith("OLD") for c in result["dropped"])


@pytest.mark.asyncio
async def test_ensure_subscribed_keeps_extra_codes_under_limit():
    """한도 안이면 대상 밖 종목이 남아 있어도 해제하지 않는다 — 공짜로 더 받는 데이터다."""
    pool = _FakePool(positions=["005930"], pending_sheets=[])
    with respx.mock(assert_all_called=True) as mock:
        mock.get(url__regex=_STATUS_RE).respond(
            200, json={"is_running": True, "codes": ["005930", "999999"]}
        )
        result = await ensure_subscribed(pool, GATEWAY)

    assert result["noop"] is True
    assert result["dropped"] == []


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
