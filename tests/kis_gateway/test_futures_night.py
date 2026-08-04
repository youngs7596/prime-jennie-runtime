"""FuturesNightCollector — 가짜 WebSocket/DB 기반 단위 테스트.

실제 KIS 에 붙지 않고 `ws_connect` 와 pool 을 주입해 프레임 파싱 → 분 단위 버킷 →
적재 흐름을 검증한다. 프레임 필드 배치는 2026-08-04 야간장 실측값을 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from prime_jennie_runtime.kis_gateway.futures_night import (
    TR_ID_NIGHT_EXEC,
    FuturesNightCollector,
    session_trade_date,
)

_KST = timezone(timedelta(hours=9))


def _frame(
    code: str = "A01609",
    *,
    hhmmss: str = "203355",
    price: str = "1015.55",
    volume: str = "6580",
    oi: str = "163845",
    oi_change: str = "-369",
) -> str:
    """H0MFCNT0 체결 프레임 한 건 (49필드, 실측 인덱스만 채우고 나머지는 0)."""
    fields = ["0"] * 49
    fields[0] = code
    fields[1] = hhmmss
    fields[5] = price
    fields[10] = volume
    fields[18] = oi
    fields[19] = oi_change
    return "0|" + TR_ID_NIGHT_EXEC + "|001|" + "^".join(fields)


class FakeWebSocket:
    def __init__(self, incoming: list[str]):
        self._incoming = list(incoming)
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


class FakeWsConnect:
    def __init__(self, ws: FakeWebSocket):
        self._ws = ws
        self.url: str | None = None
        self.connected = False

    def __call__(self, url: str) -> FakeWsConnect:
        self.url = url
        return self

    async def __aenter__(self) -> FakeWebSocket:
        self.connected = True
        return self._ws

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeConn:
    def __init__(self, has_close_row: bool):
        self._has_close_row = has_close_row
        self.rows: list[tuple] = []

    async def fetchval(self, _sql: str, *args):
        return 1 if self._has_close_row else None

    async def execute(self, _sql: str, *args) -> None:
        self.rows.append(args)


class FakePool:
    def __init__(self, has_close_row: bool = True):
        self.conn = FakeConn(has_close_row)

    def acquire(self):
        return _AcquireCtx(self.conn)


class _AcquireCtx:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeQuote:
    def __init__(self, code: str, is_front: bool):
        self.contract_code = code
        self.is_front = is_front


class FakeKisApi:
    def __init__(self, codes: list[tuple[str, bool]] | None = None):
        self._codes = codes if codes is not None else [("A01609", True), ("A01612", False)]

    async def get_kospi200_quotes(self):
        return [FakeQuote(c, f) for c, f in self._codes]


def _collector(
    pool: FakePool,
    ws_connect: FakeWsConnect | None = None,
    *,
    now: datetime | None = None,
    kis_api: FakeKisApi | None = None,
) -> FuturesNightCollector:
    c = FuturesNightCollector(
        pool=pool,
        kis_api=kis_api or FakeKisApi(),
        app_key="k",
        app_secret="s",
        ws_connect=ws_connect,
        flush_interval=3600.0,  # 테스트가 직접 flush 를 부른다
    )
    if now is not None:
        c.set_clock(lambda: now)

    async def _fake_key() -> str:
        return "approval-key"

    c._approval_key = _fake_key  # type: ignore[method-assign]
    return c


# ─── 세션 시각 판정 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 8, 4, 18, 0, tzinfo=_KST), date(2026, 8, 4)),
        (datetime(2026, 8, 4, 23, 59, tzinfo=_KST), date(2026, 8, 4)),
        (datetime(2026, 8, 5, 0, 30, tzinfo=_KST), date(2026, 8, 4)),
        (datetime(2026, 8, 5, 4, 59, tzinfo=_KST), date(2026, 8, 4)),
        (datetime(2026, 8, 5, 5, 0, tzinfo=_KST), None),
        (datetime(2026, 8, 5, 12, 0, tzinfo=_KST), None),
        (datetime(2026, 8, 5, 17, 59, tzinfo=_KST), None),
    ],
)
def test_session_trade_date(moment: datetime, expected: date | None) -> None:
    """자정을 넘긴 프레임은 세션이 시작된 거래일로 당겨 붙는다."""
    assert session_trade_date(moment) == expected


# ─── 프레임 파싱 ────────────────────────────────────────────────


def test_collect_uses_measured_field_indices() -> None:
    """[18]=OI, [19]=주간마감 대비 증감, [10]=야간 누적거래량, [5]=현재가."""
    c = _collector(FakePool(), now=datetime(2026, 8, 4, 20, 33, tzinfo=_KST))
    fields = _frame().split("|")[3].split("^")
    c._collect(fields, date(2026, 8, 4))

    (bucket,) = list(c._buckets.values())
    assert bucket["open_interest"] == 163845
    assert bucket["oi_change"] == -369
    assert bucket["night_volume"] == 6580
    assert bucket["price"] == pytest.approx(1015.55)
    assert bucket["frames"] == 1


def test_collect_buckets_by_minute_and_keeps_last_value() -> None:
    """같은 분의 여러 체결은 한 행으로 접히고 마지막 값이 남는다."""
    c = _collector(FakePool(), now=datetime(2026, 8, 4, 20, 33, tzinfo=_KST))
    for oi, chg in (("163821", "-393"), ("163830", "-384"), ("163845", "-369")):
        c._collect(
            _frame(hhmmss="203312", oi=oi, oi_change=chg).split("|")[3].split("^"), date(2026, 8, 4)
        )

    assert len(c._buckets) == 1
    (bucket,) = list(c._buckets.values())
    assert bucket["frames"] == 3
    assert bucket["open_interest"] == 163845
    assert bucket["oi_change"] == -369


def test_collect_separates_contracts_and_minutes() -> None:
    c = _collector(FakePool(), now=datetime(2026, 8, 4, 20, 33, tzinfo=_KST))
    c._collect(_frame(code="A01609", hhmmss="203312").split("|")[3].split("^"), date(2026, 8, 4))
    c._collect(_frame(code="A01612", hhmmss="203312").split("|")[3].split("^"), date(2026, 8, 4))
    c._collect(_frame(code="A01609", hhmmss="203412").split("|")[3].split("^"), date(2026, 8, 4))
    assert len(c._buckets) == 3


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_collect_ignores_nonpositive_open_interest(bad: str) -> None:
    """OI 가 0 이하인 프레임은 버린다 — 조용한 0 을 적재하지 않는다."""
    c = _collector(FakePool(), now=datetime(2026, 8, 4, 20, 33, tzinfo=_KST))
    c._collect(_frame(oi=bad).split("|")[3].split("^"), date(2026, 8, 4))
    assert c._buckets == {}


def test_collect_ignores_short_frames() -> None:
    c = _collector(FakePool(), now=datetime(2026, 8, 4, 20, 33, tzinfo=_KST))
    c._collect(["A01609", "203355", "0"], date(2026, 8, 4))
    assert c._buckets == {}


# ─── 적재 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_writes_only_completed_minutes() -> None:
    """진행 중인 분은 남겨 둔다 — 같은 행을 두 번 쓰지 않기 위해서."""
    pool = FakePool()
    c = _collector(pool, now=datetime(2026, 8, 4, 20, 34, 10, tzinfo=_KST))
    c._front = {"A01609": True}
    c._collect(_frame(hhmmss="203312").split("|")[3].split("^"), date(2026, 8, 4))  # 끝난 분
    c._collect(_frame(hhmmss="203405").split("|")[3].split("^"), date(2026, 8, 4))  # 진행 중

    await c._flush()

    assert len(pool.conn.rows) == 1
    assert len(c._buckets) == 1  # 진행 중인 분은 그대로
    row = pool.conn.rows[0]
    assert row[0] == date(2026, 8, 4)  # trade_date
    assert row[1] == "A01609"
    assert row[2] == datetime(2026, 8, 4, 20, 33, tzinfo=_KST)  # 분 단위 절삭
    assert row[3] is True  # is_front
    assert row[5] == 163845  # open_interest
    assert row[6] == -369  # oi_change


@pytest.mark.asyncio
async def test_flush_force_writes_current_minute() -> None:
    """세션 종료 시엔 진행 중인 분까지 비운다."""
    pool = FakePool()
    c = _collector(pool, now=datetime(2026, 8, 4, 20, 34, 10, tzinfo=_KST))
    c._collect(_frame(hhmmss="203405").split("|")[3].split("^"), date(2026, 8, 4))

    await c._flush(force=True)

    assert len(pool.conn.rows) == 1
    assert c._buckets == {}


@pytest.mark.asyncio
async def test_flush_noop_when_empty() -> None:
    pool = FakePool()
    c = _collector(pool, now=datetime(2026, 8, 4, 20, 34, tzinfo=_KST))
    await c._flush(force=True)
    assert pool.conn.rows == []


# ─── 거래일 가드 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_holiday_skipped_by_close_row_guard() -> None:
    """그 날짜에 주간 마감 스냅샷이 없으면 야간장도 없었다고 본다."""
    pool = FakePool(has_close_row=False)
    c = _collector(pool)
    assert await c._is_trading_day(date(2026, 8, 4)) is False

    pool_ok = FakePool(has_close_row=True)
    c_ok = _collector(pool_ok)
    assert await c_ok._is_trading_day(date(2026, 8, 4)) is True


# ─── 세션 전체 흐름 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_session_subscribes_both_contracts_and_persists() -> None:
    """근월·차월 둘 다 구독하고(롤오버 중화), 받은 프레임이 행으로 남는다."""
    pool = FakePool()
    ws = FakeWebSocket(
        [
            _frame(code="A01609", hhmmss="200100", oi="163900", oi_change="-314"),
            _frame(code="A01609", hhmmss="200230", oi="163845", oi_change="-369"),
        ]
    )
    connect = FakeWsConnect(ws)
    c = _collector(pool, connect, now=datetime(2026, 8, 4, 20, 3, tzinfo=_KST))
    c._running = True

    await c._run_session(date(2026, 8, 4))

    subscribed = [msg for msg in ws.sent if TR_ID_NIGHT_EXEC in msg]
    assert len(subscribed) == 2
    assert any("A01609" in m for m in subscribed)
    assert any("A01612" in m for m in subscribed)

    assert len(pool.conn.rows) == 2
    assert {r[6] for r in pool.conn.rows} == {-314, -369}
    assert c.get_status()["frames_received"] == 2


@pytest.mark.asyncio
async def test_pingpong_is_echoed() -> None:
    """PINGPONG 응답이 늦으면 KIS 가 연결을 끊는다."""
    pool = FakePool()
    ping = '{"header":{"tr_id":"PINGPONG"}}'
    ws = FakeWebSocket([ping])
    connect = FakeWsConnect(ws)
    c = _collector(pool, connect, now=datetime(2026, 8, 4, 20, 3, tzinfo=_KST))
    c._running = True

    await c._run_session(date(2026, 8, 4))

    assert ping in ws.sent


@pytest.mark.asyncio
async def test_session_stops_when_night_window_closes() -> None:
    """야간장이 끝나면 남은 프레임을 읽지 않고 세션을 닫는다."""
    pool = FakePool()
    ws = FakeWebSocket([_frame(), _frame(hhmmss="203400")])
    connect = FakeWsConnect(ws)
    # 세션은 8-04 밤인데 시계는 이미 다음날 아침 — 첫 메시지에서 창이 닫힌 걸 본다.
    c = _collector(pool, connect, now=datetime(2026, 8, 5, 9, 0, tzinfo=_KST))
    c._running = True

    await c._run_session(date(2026, 8, 4))

    assert pool.conn.rows == []
    assert c.get_status()["frames_received"] == 0


@pytest.mark.asyncio
async def test_status_reports_frames_not_just_ack() -> None:
    """ACK 만으로 '붙었다'고 판단하지 않도록 상태에 프레임 수를 노출한다."""
    pool = FakePool()
    c = _collector(pool, now=datetime(2026, 8, 4, 20, 33, tzinfo=_KST))
    status = c.get_status()
    assert status["frames_received"] == 0
    assert status["rows_written"] == 0
    assert status["is_running"] is False


@pytest.mark.asyncio
async def test_stop_flushes_pending_buckets() -> None:
    pool = FakePool()
    c = _collector(pool, now=datetime(2026, 8, 4, 20, 33, tzinfo=_KST))
    c._collect(_frame().split("|")[3].split("^"), date(2026, 8, 4))
    c._running = True
    c._task = asyncio.create_task(asyncio.sleep(3600))

    await c.stop()

    assert len(pool.conn.rows) == 1
    assert c.is_running is False
