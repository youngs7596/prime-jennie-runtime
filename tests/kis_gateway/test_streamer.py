"""KISWebSocketStreamer — FakeWebSocket 기반 단위 테스트.

실제 KIS WebSocket 은 연결하지 않고 `ws_connect` 를 주입하여
subscribe 송신, 메시지 파싱 → Redis XADD 흐름을 검증.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from prime_jennie_runtime.infra.redis_streams import STREAM_PRICES
from prime_jennie_runtime.kis_gateway.market_hours import MarketCalendar
from prime_jennie_runtime.kis_gateway.streamer import KISWebSocketStreamer

_KST = timezone(timedelta(hours=9))


class FakeWebSocket:
    """asyncio.Queue 기반 가짜 WS. async iterator 로 수신 메시지를 공급."""

    def __init__(self, incoming: list[str]):
        self._incoming = asyncio.Queue()
        for msg in incoming:
            self._incoming.put_nowait(msg)
        self.sent: list[str] = []
        self.closed = asyncio.Event()

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def close(self) -> None:
        self.closed.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._incoming.empty():
            # 메시지 소진 시 close 효과 — iterator 종료
            raise StopAsyncIteration
        return await self._incoming.get()


class FakeWsConnect:
    """`async with ws_connect(url) as ws:` 형태를 흉내낸 컨텍스트 매니저."""

    def __init__(self, ws: FakeWebSocket):
        self._ws = ws
        self.url: str | None = None

    def __call__(self, url: str) -> FakeWsConnect:
        self.url = url
        return self

    async def __aenter__(self) -> FakeWebSocket:
        return self._ws

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._ws.close()


def _always_streaming_calendar() -> MarketCalendar:
    cal = MarketCalendar(trading_day_checker=lambda _d: True)
    cal.set_clock(lambda: datetime(2026, 4, 17, 10, 0, tzinfo=_KST))
    return cal


async def test_add_subscriptions_tracks_codes(fake_redis):
    streamer = KISWebSocketStreamer(
        redis_client=fake_redis,
        app_key="k",
        app_secret="s",
        is_paper=True,
        calendar=_always_streaming_calendar(),
    )
    added = await streamer.add_subscriptions(["005930", "000660"])
    assert added == ["005930", "000660"]
    assert streamer.subscription_count == 2
    # 중복 방지
    added = await streamer.add_subscriptions(["005930"])
    assert added == []


async def test_remove_subscriptions(fake_redis):
    streamer = KISWebSocketStreamer(
        redis_client=fake_redis, app_key="k", app_secret="s", is_paper=True
    )
    await streamer.add_subscriptions(["005930", "000660"])
    removed = await streamer.remove_subscriptions(["000660"])
    assert removed == ["000660"]
    assert streamer.subscription_count == 1


async def test_handle_tick_writes_to_redis(fake_redis):
    streamer = KISWebSocketStreamer(
        redis_client=fake_redis, app_key="k", app_secret="s", is_paper=True
    )
    # KIS 실시간 체결가 포맷: 0|H0STCNT0|001|<fields>
    # H0STCNT0: [0]code [1]time [2]price [3]vrss_sign [4]vrss [5]ctrt
    #           [6]wgt_avg [7]open [8]high [9]low [10]ask1 [11]bid1
    #           [12]contract_vol [13]accumulated_vol
    fields = [
        "005930", "090000", "71200", "0", "100", "71500",
        "0", "0", "0", "0", "71210", "71190", "10", "50",
    ]
    msg = "0|H0STCNT0|001|" + "^".join(fields)

    await streamer._handle_message(ws=FakeWebSocket([]), message=msg)

    entries = await fake_redis.xrange(STREAM_PRICES, "-", "+")
    assert len(entries) == 1
    _mid, data = entries[0]
    assert data[b"code"] == b"005930"
    assert data[b"price"] == b"71200"
    assert data[b"ask"] == b"71210"
    assert data[b"bid"] == b"71190"
    assert data[b"vol"] == b"50"


async def test_handle_pingpong_echoes_back(fake_redis):
    streamer = KISWebSocketStreamer(
        redis_client=fake_redis, app_key="k", app_secret="s", is_paper=True
    )
    ping_msg = json.dumps({"header": {"tr_id": "PINGPONG"}, "body": {}})
    fake_ws = FakeWebSocket([])
    await streamer._handle_message(ws=fake_ws, message=ping_msg)
    assert fake_ws.sent == [ping_msg]


async def test_ws_loop_subscribes_and_publishes(fake_redis, monkeypatch):
    """실제 loop 실행 — 메시지 소진 후 연결 종료 → 재연결 backoff 진입."""
    # KIS tick 1건 + JSON(구독 응답) 1건
    tick_fields = ["005930", "090000", "70000", "0", "100", "70100"]
    tick_msg = "0|H0STCNT0|001|" + "^".join(tick_fields)
    sub_ack = json.dumps({"header": {"tr_id": "H0STCNT0", "rt_cd": "0"}, "body": {}})
    fake_ws = FakeWebSocket([sub_ack, tick_msg])
    fake_connect = FakeWsConnect(fake_ws)

    calendar = _always_streaming_calendar()
    streamer = KISWebSocketStreamer(
        redis_client=fake_redis,
        app_key="k",
        app_secret="s",
        is_paper=True,
        calendar=calendar,
        ws_connect=fake_connect,
    )
    await streamer.add_subscriptions(["005930"])
    streamer._approval_key = "APPROVAL"

    # sleep 은 backoff 때문에 호출됨 — 루프가 끝날 때까지 노출되지 않도록 짧게
    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        # 주기적인 50ms subscribe 간격만 허용, 그 외는 즉시 반환 후 루프 종료 유도
        if delay <= 0.1:
            await original_sleep(0)
        else:
            streamer._is_running = False

    monkeypatch.setattr("prime_jennie_runtime.kis_gateway.streamer.asyncio.sleep", fast_sleep)

    streamer._is_running = True
    await streamer._ws_loop(approval_key="APPROVAL")

    # 구독 메시지 송신 확인
    assert any(
        json.loads(m).get("body", {}).get("input", {}).get("tr_key") == "005930"
        for m in fake_ws.sent
    )
    # Redis 에 1건 이상 발행
    entries = await fake_redis.xrange(STREAM_PRICES, "-", "+")
    assert len(entries) >= 1


async def test_stop_flips_flag_and_cancels_task(fake_redis):
    streamer = KISWebSocketStreamer(
        redis_client=fake_redis, app_key="k", app_secret="s", is_paper=True
    )
    streamer._is_running = True

    async def _forever() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    streamer._task = asyncio.create_task(_forever())
    await streamer.stop()
    assert streamer._is_running is False
    assert streamer._task is None


# pytest 네임스페이스 워닝 억제
_ = pytest
