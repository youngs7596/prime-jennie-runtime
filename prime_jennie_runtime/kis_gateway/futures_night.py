"""KRX 야간선물 미결제약정 상시 수집 (웹소켓 H0MFCNT0 → futures_night_oi).

왜 별도 경로인가
----------------
REST(FHMIF10000000)는 야간장을 못 본다. 2026-07-26 에 9거래일치로 판정했는데
night_open(18:10) 과 night_close(익일 05:05) 가 모든 필드에서 한 자리도 다르지 않았고,
설계 간판 지표였던 `close.OI − night_close.OI` 가 구조적으로 항상 0 이었다. 시장구분
후보를 전수로 넣어 봐도 주간(`F`) 외엔 통로가 없다 — **재조사 금지**.

2026-08-04 야간장 스모크에서 웹소켓 `H0MFCNT0` 이 살아 있음을 확인했다. 10분에 근월물
프레임 325개가 왔고, 같은 시간 REST 는 완전히 얼어 있었다(대조 증명). 프레임 49필드 중
**[18]=야간 미결제약정, [19]=주간마감 대비 증감**이며, [19] 가 곧 우리가 빼서 만들려던
그 지표다(산술 2회 독립 검증). 필드 인덱스는 전부 실측이고 추측이 없다.

주간 스트리머(`streamer.py`)에 얹지 않는다
------------------------------------------
그쪽은 매매 경로(fast_loop 가격 스트림)와 실시간 체결·호가 적재가 물려 있는 공용
자원이다. 선물 구독을 끼워 넣으면 장애 반경이 매매까지 넓어진다. 야간장(18:00~05:00)과
주간 스트리밍 시간(08:50~15:35)은 겹치지 않으므로, 같은 계정으로 시간을 나눠 쓰는
별도 연결이 더 안전하다.

적재 규약
---------
- `trade_date` 는 **야간장이 시작된 거래일**(18:00 쪽 날짜). 자정을 넘긴 프레임은 하루를
  당겨 적재해 같은 trade_date 안에서 주간 마감과 야간 관측이 나란히 놓인다(028 과 동일).
- 휴장일엔 행을 아예 안 남긴다. 세션 시작 때 그 날짜에 `futures_oi_snapshots` 의 close
  행이 있는지로 거래일을 판정한다 — 새벽 05시의 '오늘' 기준 판정은 금요일 밤 세션이
  토요일 새벽에 끝나는 구조와 어긋나기 때문이고, 028 이 이미 쓰는 가드다.
- 분 단위 스냅샷. 끝난 분만 적재하므로 같은 분을 두 번 쓰지 않는다.

설계 근거 전문: `.ai/analyses/2026-07-12-futures-flow-source-audit.md`
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)

WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"

# KIS TR — KRX 야간선물 실시간체결
TR_ID_NIGHT_EXEC = "H0MFCNT0"

_KST = timezone(timedelta(hours=9))

# 야간장 운영 시간 (HHMM 정수). 18:00 개장, 익일 05:00 마감.
NIGHT_OPEN = 1800
NIGHT_CLOSE = 500

# 프레임 필드 인덱스 — 2026-08-04 야간장 실측 (49필드). 추측 금지.
_F_CODE = 0
_F_TIME = 1
_F_PRICE = 5
_F_VOLUME = 10  # 야간 세션 누적거래량 (주간과 별도 카운터)
_F_OI = 18
_F_OI_CHANGE = 19
_MIN_FIELDS = 20

_TABLE = "futures_night_oi"

_BACKOFF_INITIAL = 30
_BACKOFF_MAX = 300
_MAX_BUCKETS = 10_000


def _to_int(s: str) -> int | None:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def session_trade_date(now: datetime) -> date_cls | None:
    """야간장 안이면 그 세션이 시작된 거래일, 밖이면 None.

    18:00~23:59 는 당일, 00:00~04:59 는 전일 세션의 연장이다.
    """
    t = now.hour * 100 + now.minute
    if t >= NIGHT_OPEN:
        return now.date()
    if t < NIGHT_CLOSE:
        return (now - timedelta(days=1)).date()
    return None


class FuturesNightCollector:
    """야간장 동안 선물 체결 프레임을 받아 분 단위 미결제약정을 적재한다."""

    def __init__(
        self,
        pool: Any,
        kis_api: Any,
        app_key: str,
        app_secret: str,
        *,
        is_paper: bool = False,
        ws_connect: Any = None,
        flush_interval: float = 30.0,
        idle_poll: float = 60.0,
    ):
        self._pool = pool
        self._kis_api = kis_api
        self._app_key = app_key
        self._app_secret = app_secret
        self._ws_url = WS_URL_PAPER if is_paper else WS_URL_REAL
        self._ws_connect = ws_connect or websockets.connect
        self._flush_interval = flush_interval
        self._idle_poll = idle_poll

        self._task: asyncio.Task | None = None
        self._running = False
        self._base_url = ""
        # (contract_code, minute_ts) → 그 분의 마지막 값 + 프레임 수
        self._buckets: dict[tuple[str, datetime], dict[str, Any]] = {}
        self._front: dict[str, bool] = {}
        self._session_date: date_cls | None = None
        self._frames = 0
        self._rows = 0
        self._now = lambda: datetime.now(_KST)

    def set_clock(self, now_fn: Any) -> None:
        """테스트 전용 — KST datetime 을 돌려주는 함수를 주입."""
        self._now = now_fn

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self, base_url: str) -> None:
        if self._running:
            return
        self._base_url = base_url
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("FuturesNightCollector started (url=%s)", self._ws_url)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        with contextlib.suppress(Exception):
            await self._flush(force=True)
        logger.info("FuturesNightCollector stopped (frames=%d rows=%d)", self._frames, self._rows)

    def get_status(self) -> dict[str, Any]:
        return {
            "is_running": self._running,
            "session_trade_date": str(self._session_date) if self._session_date else None,
            "contracts": sorted(self._front),
            "frames_received": self._frames,
            "rows_written": self._rows,
            "pending_minutes": len(self._buckets),
        }

    # ─── 세션 게이트 ────────────────────────────────────────────

    async def _loop(self) -> None:
        backoff = _BACKOFF_INITIAL
        while self._running:
            trade_date = session_trade_date(self._now())
            if trade_date is None:
                self._session_date = None
                await asyncio.sleep(self._idle_poll)
                backoff = _BACKOFF_INITIAL
                continue

            if not await self._is_trading_day(trade_date):
                logger.info("야간 수집 건너뜀: %s 는 거래일이 아니다", trade_date)
                self._session_date = None
                await asyncio.sleep(self._idle_poll)
                continue

            try:
                await self._run_session(trade_date)
                backoff = _BACKOFF_INITIAL
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("야간 세션 오류 (%ds 뒤 재시도): %s", backoff, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _is_trading_day(self, trade_date: date_cls) -> bool:
        """그 날짜에 주간 마감 스냅샷이 있으면 거래일이었다는 뜻 (028 과 같은 가드)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT 1 FROM futures_oi_snapshots WHERE trade_date=$1 AND slot='close' LIMIT 1",
                trade_date,
            )
        return bool(row)

    # ─── 세션 본체 ──────────────────────────────────────────────

    async def _run_session(self, trade_date: date_cls) -> None:
        contracts = await self._resolve_contracts()
        if not contracts:
            raise RuntimeError("야간 수집 대상 계약을 못 찾았다")

        self._session_date = trade_date
        approval_key = await self._approval_key()

        async with self._ws_connect(self._ws_url) as ws:
            for code in contracts:
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "approval_key": approval_key,
                                "custtype": "P",
                                "tr_type": "1",
                                "content-type": "utf-8",
                            },
                            "body": {"input": {"tr_id": TR_ID_NIGHT_EXEC, "tr_key": code}},
                        }
                    )
                )
                await asyncio.sleep(0.05)
            logger.info("야간선물 구독 %s (trade_date=%s)", contracts, trade_date)

            flusher = asyncio.create_task(self._flush_loop())
            try:
                async for message in ws:
                    if not self._running:
                        break
                    if session_trade_date(self._now()) != trade_date:
                        logger.info("야간장 종료 — 세션 %s 수집 마감", trade_date)
                        break
                    await self._handle_message(ws, message, trade_date)
            finally:
                flusher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await flusher
                await self._flush(force=True)

    async def _resolve_contracts(self) -> list[str]:
        """근월·차월 두 계약. 롤오버 중화를 위해 주간 수집기와 같은 규율로 둘 다 본다."""
        quotes = await self._kis_api.get_kospi200_quotes()
        self._front = {q.contract_code: bool(q.is_front) for q in quotes}
        return sorted(self._front)

    async def _approval_key(self) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base_url}/oauth2/Approval",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self._app_key,
                    "secretkey": self._app_secret,
                },
            )
        resp.raise_for_status()
        key = resp.json().get("approval_key", "")
        if not key:
            raise RuntimeError("approval_key 발급 실패")
        return key

    async def _handle_message(self, ws: Any, message: str | bytes, trade_date: date_cls) -> None:
        if not message:
            return
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="ignore")

        if message.startswith("{"):
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(message)
                if data.get("header", {}).get("tr_id") == "PINGPONG":
                    with contextlib.suppress(Exception):
                        await ws.send(message)
            return

        parts = message.split("|")
        if len(parts) < 4 or parts[1] != TR_ID_NIGHT_EXEC:
            return
        self._collect(parts[3].split("^"), trade_date)

    def _collect(self, fields: list[str], trade_date: date_cls) -> None:
        """체결 프레임 → 분 단위 버킷. 인덱스는 2026-08-04 야간장 실측."""
        if len(fields) < _MIN_FIELDS:
            return
        oi = _to_int(fields[_F_OI])
        if oi is None or oi <= 0:
            return

        code = fields[_F_CODE]
        minute_ts = self._minute_ts(fields[_F_TIME])
        self._frames += 1

        key = (code, minute_ts)
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= _MAX_BUCKETS:
                return
            bucket = {"trade_date": trade_date, "frames": 0}
            self._buckets[key] = bucket

        bucket["frames"] += 1
        bucket["price"] = _to_float(fields[_F_PRICE])
        bucket["open_interest"] = oi
        bucket["oi_change"] = _to_int(fields[_F_OI_CHANGE])
        bucket["night_volume"] = _to_int(fields[_F_VOLUME])

    def _minute_ts(self, hhmmss: str) -> datetime:
        """체결시각(HHMMSS) → 분 단위로 절삭한 KST datetime. 파싱 실패 시 현재 분."""
        now = self._now()
        try:
            return now.replace(
                hour=int(hhmmss[0:2]),
                minute=int(hhmmss[2:4]),
                second=0,
                microsecond=0,
            )
        except (ValueError, IndexError):
            return now.replace(second=0, microsecond=0)

    # ─── 적재 ───────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self._flush()
            except Exception as e:
                logger.warning("야간 OI 적재 실패 (다음 주기 재시도): %s", e)

    async def _flush(self, *, force: bool = False) -> None:
        """끝난 분만 적재한다. 진행 중인 분을 쓰면 같은 행을 두 번 건드리게 된다."""
        if not self._buckets:
            return
        cutoff = self._now().replace(second=0, microsecond=0)
        ready = [(code, ts, b) for (code, ts), b in self._buckets.items() if force or ts < cutoff]
        if not ready:
            return

        async with self._pool.acquire() as conn:
            for code, ts, b in ready:
                await conn.execute(
                    f"INSERT INTO {_TABLE} "  # noqa: S608 — 상수 테이블명
                    "(trade_date, contract_code, minute_ts, is_front, futures_price, "
                    " open_interest, oi_change, night_volume, frames, source) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'kis_ws') "
                    "ON CONFLICT (trade_date, contract_code, minute_ts) DO UPDATE SET "
                    "is_front=EXCLUDED.is_front, futures_price=EXCLUDED.futures_price, "
                    "open_interest=EXCLUDED.open_interest, oi_change=EXCLUDED.oi_change, "
                    "night_volume=EXCLUDED.night_volume, frames=EXCLUDED.frames",
                    b["trade_date"],
                    code,
                    ts,
                    self._front.get(code, False),
                    b.get("price"),
                    b["open_interest"],
                    b.get("oi_change"),
                    b.get("night_volume"),
                    b["frames"],
                )
        for code, ts, _ in ready:
            self._buckets.pop((code, ts), None)
        self._rows += len(ready)
        logger.debug("야간 OI 적재: %d행 (누적 %d)", len(ready), self._rows)


__all__ = ["FuturesNightCollector", "TR_ID_NIGHT_EXEC", "session_trade_date"]
