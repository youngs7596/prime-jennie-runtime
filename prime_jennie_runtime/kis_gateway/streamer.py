"""KIS WebSocket Streamer — 실시간 체결가 수신 → Redis Stream 발행 (async).

KIS API WebSocket (H0STCNT0) 연결, 종목별 체결가 수신 후
`STREAM_PRICES` (kis:prices) 에 발행.

원본: prime_jennie/services/gateway/streamer.py
변경점:
  - websocket-client(sync, thread) → websockets(async coroutine)
  - threading.Lock → asyncio.Lock
  - background thread → asyncio.Task
  - redis sync client → redis.asyncio
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import redis.asyncio as aioredis
import websockets
from websockets.exceptions import ConnectionClosed

from prime_jennie_runtime.infra.redis_streams import STREAM_PRICES

from .market_hours import MarketCalendar

logger = logging.getLogger(__name__)

# KIS WebSocket URLs
WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"

# Redis Stream
PRICE_STREAM_MAXLEN = 10_000

# KIS TR ID — 실시간 체결(H0STCNT0) + 실시간 호가(H0STASP0)
TR_ID_STOCK_EXEC = "H0STCNT0"
TR_ID_STOCK_ASKP = "H0STASP0"

# Backoff constants
_BACKOFF_INITIAL = 60
_BACKOFF_MAX = 600
_STABLE_CONNECTION_SECS = 30

_KST = timezone(timedelta(hours=9))


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


class KISWebSocketStreamer:
    """KIS WebSocket → Redis Stream 비동기 스트리머.

    Gateway lifespan 에서 싱글턴으로 관리. 여러 서비스의 구독 요청을 통합.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        app_key: str,
        app_secret: str,
        *,
        is_paper: bool = False,
        calendar: MarketCalendar | None = None,
        ws_connect: Any = None,
        sink: Any = None,
    ):
        self._redis = redis_client
        self._app_key = app_key
        self._app_secret = app_secret
        self._ws_url = WS_URL_PAPER if is_paper else WS_URL_REAL
        self._calendar = calendar or MarketCalendar()
        # ws_connect 은 테스트용 — websockets.connect 대체 가능
        self._ws_connect = ws_connect or websockets.connect
        # sink 가 있으면 체결·호가를 적재 버퍼로 흘리고 호가 채널도 구독한다.
        self._sink = sink
        self._subscribe_orderbook = sink is not None

        self._subscription_codes: set[str] = set()
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._is_running = False
        self._base_url: str = ""
        self._approval_key: str | None = None
        self._approval_key_expires: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def subscription_count(self) -> int:
        return len(self._subscription_codes)

    @property
    def subscribed_codes(self) -> list[str]:
        return sorted(self._subscription_codes)

    def attach_sink(self, sink: Any) -> None:
        """적재 버퍼 연결. lifespan 에서 pg_pool 준비 후 호출. 호가 구독을 켠다."""
        self._sink = sink
        self._subscribe_orderbook = True

    async def get_approval_key(self, base_url: str) -> str:
        """WebSocket approval key 발급 (캐싱 30초)."""
        if self._approval_key and time.time() < self._approval_key_expires:
            return self._approval_key

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url}/oauth2/Approval",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self._app_key,
                    "secretkey": self._app_secret,
                },
            )
        resp.raise_for_status()
        data = resp.json()

        self._approval_key = data.get("approval_key", "")
        self._approval_key_expires = time.time() + 30
        return self._approval_key

    async def add_subscriptions(self, codes: list[str]) -> list[str]:
        """구독 종목 추가. 새로 추가된 코드 목록 반환. 재시작 없이 hot subscribe."""
        async with self._lock:
            new_codes = [c for c in codes if c not in self._subscription_codes]
            self._subscription_codes.update(new_codes)

        if self._is_running and new_codes and self._ws and self._approval_key:
            try:
                await self._send_subscribe(self._ws, new_codes, tr_type="1")
            except Exception as e:
                logger.warning("Hot subscribe failed (will retry on reconnect): %s", e)

        return new_codes

    async def remove_subscriptions(self, codes: list[str]) -> list[str]:
        """구독 종목 해제. 해제된 코드 목록 반환."""
        async with self._lock:
            removed = [c for c in codes if c in self._subscription_codes]
            self._subscription_codes -= set(removed)

        if self._is_running and removed and self._ws and self._approval_key:
            try:
                await self._send_subscribe(self._ws, removed, tr_type="2")
            except Exception as e:
                logger.warning("Unsubscribe send failed: %s", e)

        return removed

    async def start(self, base_url: str) -> None:
        """WebSocket 루프 시작 (asyncio.Task)."""
        if self._is_running:
            logger.warning("Streamer already running")
            return

        if not self._subscription_codes:
            logger.warning("No codes to subscribe")
            return

        self._base_url = base_url

        try:
            approval_key = await self.get_approval_key(base_url)
        except Exception as e:
            logger.error("Failed to get approval key: %s", e)
            return

        self._is_running = True
        self._task = asyncio.create_task(self._ws_loop(approval_key))
        logger.info(
            "Streamer started: %d codes, url=%s",
            len(self._subscription_codes),
            self._ws_url,
        )

    async def stop(self) -> None:
        """WebSocket 종료."""
        self._is_running = False
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        logger.info("Streamer stopped")

    async def _send_subscribe(self, ws: Any, codes: list[str], tr_type: str = "1") -> None:
        """종목별 구독/해제 요청 전송. tr_type '1'=구독, '2'=해제.

        sink 가 연결돼 있으면 종목마다 체결+호가 두 채널을 등록한다 (KIS 등록 2건/종목).
        """
        tr_ids = [TR_ID_STOCK_EXEC]
        if self._subscribe_orderbook:
            tr_ids.append(TR_ID_STOCK_ASKP)
        for code in codes:
            if not self._is_running:
                break
            for tr_id in tr_ids:
                msg = json.dumps(
                    {
                        "header": {
                            "approval_key": self._approval_key,
                            "custtype": "P",
                            "tr_type": tr_type,
                            "content-type": "utf-8",
                        },
                        "body": {
                            "input": {
                                "tr_id": tr_id,
                                "tr_key": code,
                            }
                        },
                    }
                )
                try:
                    await ws.send(msg)
                except Exception:
                    return
                await asyncio.sleep(0.05)  # 50ms 간격

    async def _ws_loop(self, approval_key: str) -> None:
        """WebSocket 메인 루프 (장외 시간 대기 + exponential backoff)."""
        backoff = _BACKOFF_INITIAL

        while self._is_running:
            # 장외 시간이면 60초 sleep 후 재확인
            if not self._calendar.is_streaming_hours():
                logger.debug("Outside streaming hours, waiting 60s...")
                await asyncio.sleep(60)
                backoff = _BACKOFF_INITIAL
                continue

            connected_at = 0.0
            try:
                async with self._ws_connect(self._ws_url) as ws:
                    self._ws = ws
                    connected_at = time.time()
                    logger.info("KIS WebSocket connected")

                    async with self._lock:
                        codes = list(self._subscription_codes)
                    await self._send_subscribe(ws, codes, tr_type="1")
                    logger.info("Subscribed to %d codes", len(codes))

                    async for message in ws:
                        if not self._is_running:
                            break
                        await self._handle_message(ws, message)
            except ConnectionClosed as e:
                logger.info("KIS WebSocket closed: %s", e)
            except Exception as e:
                logger.warning("KIS WebSocket error: %s", e)
            finally:
                self._ws = None

            if not self._is_running:
                break

            # Exponential backoff: 안정 연결(30초 이상) 후 끊기면 리셋
            elapsed = time.time() - connected_at if connected_at else 0
            backoff = (
                _BACKOFF_INITIAL
                if elapsed >= _STABLE_CONNECTION_SECS
                else min(backoff * 2, _BACKOFF_MAX)
            )
            logger.info("Reconnecting in %ds (connection lasted %.0fs)...", backoff, elapsed)
            await asyncio.sleep(backoff)

            # 재연결 시 approval_key 갱신
            if self._base_url:
                try:
                    self._approval_key_expires = 0.0
                    approval_key = await self.get_approval_key(self._base_url)
                    logger.info("Approval key refreshed for reconnect")
                except Exception as e:
                    logger.warning("Failed to refresh approval key, reusing old: %s", e)
        _ = approval_key  # 재사용 방지 경고 회피

    async def _handle_message(self, ws: Any, message: str | bytes) -> None:
        """WebSocket 메시지 파싱 → Redis XADD."""
        if not message:
            return

        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="ignore")

        # JSON 메시지: PINGPONG 또는 구독 응답
        if message.startswith("{"):
            try:
                data = json.loads(message)
                tr_id = data.get("header", {}).get("tr_id", "")
                if tr_id == "PINGPONG":
                    with contextlib.suppress(Exception):
                        await ws.send(message)
                    logger.debug("PINGPONG echoed")
                else:
                    logger.debug("KIS JSON msg: %s", message[:200])
            except json.JSONDecodeError:
                logger.debug("Non-JSON message starting with '{': %s", message[:100])
            return

        if message[0] not in ("0", "1"):
            return

        parts = message.split("|")
        if len(parts) < 4:
            return
        tr_id = parts[1]
        fields = parts[3].split("^")

        if tr_id == TR_ID_STOCK_EXEC:
            await self._handle_tick(fields)
        elif tr_id == TR_ID_STOCK_ASKP:
            self._handle_orderbook(fields)

    async def _handle_tick(self, fields: list[str]) -> None:
        """체결(H0STCNT0) → 기존 Redis 발행 (fast_loop) + 적재 sink push."""
        if len(fields) < 6:
            return
        try:
            stock_code = fields[0]
            price = fields[2]
            high = fields[5]
            # KIS H0STCNT0 표준: 10=ASKP1(매도1), 11=BIDP1(매수1), 13=ACML_VOL(누적거래량)
            ask = fields[10] if len(fields) > 10 else "0"
            bid = fields[11] if len(fields) > 11 else "0"
            volume = fields[13] if len(fields) > 13 else "0"

            await self._redis.xadd(
                STREAM_PRICES,
                {
                    "code": stock_code,
                    "price": price,
                    "high": high,
                    "ask": ask,
                    "bid": bid,
                    "vol": volume,
                },
                maxlen=PRICE_STREAM_MAXLEN,
                approximate=True,
            )
        except (IndexError, ValueError) as e:
            logger.debug("Tick parse error: %s", e)

        if self._sink is not None:
            self._push_tick_record(fields)

    def _push_tick_record(self, fields: list[str]) -> None:
        """체결 레코드를 적재 sink 로. 인덱스는 KIS H0STCNT0 표준 (스모크 확인 항목)."""
        if len(fields) < 14:
            return
        price = _to_int(fields[2])
        volume = _to_int(fields[12])  # 이 틱 체결량 (CNTG_VOL)
        if price is None or volume is None:
            return
        record = (
            fields[0],
            self._build_ts(fields[1]),
            price,
            volume,
            _to_int(fields[13]),  # 누적거래량
            _to_int(fields[21]) if len(fields) > 21 else None,  # 체결구분
            _to_float(fields[18]) if len(fields) > 18 else None,  # 체결강도
            _to_int(fields[10]),  # 최우선 매도호가
            _to_int(fields[11]),  # 최우선 매수호가
        )
        self._sink.add_tick(record)

    def _handle_orderbook(self, fields: list[str]) -> None:
        """호가(H0STASP0) → 적재 sink. 인덱스는 KIS H0STASP0 표준 (스모크 확인 항목)."""
        if self._sink is None or len(fields) < 45:
            return

        def _levels(start: int) -> list[int]:
            return [(_to_int(fields[start + i]) or 0) for i in range(10)]

        record = (
            fields[0],
            self._build_ts(fields[1]),
            _levels(3),  # 매도호가 1~10
            _levels(23),  # 매도호가 잔량 1~10
            _levels(13),  # 매수호가 1~10
            _levels(33),  # 매수호가 잔량 1~10
            _to_int(fields[43]),  # 총 매도호가 잔량
            _to_int(fields[44]),  # 총 매수호가 잔량
        )
        self._sink.add_orderbook(record)

    def _build_ts(self, hhmmss: str) -> datetime:
        """KIS 시각(HHMMSS) + 당일(KST) → tz-aware datetime. 파싱 실패 시 현재 시각."""
        now = datetime.now(_KST)
        try:
            return now.replace(
                hour=int(hhmmss[0:2]),
                minute=int(hhmmss[2:4]),
                second=int(hhmmss[4:6]),
                microsecond=0,
            )
        except (ValueError, IndexError):
            return now.replace(microsecond=0)

    def get_status(self) -> dict[str, Any]:
        """상태 조회."""
        codes = sorted(self._subscription_codes)
        return {
            "is_running": self._is_running,
            "subscription_count": len(codes),
            "codes": codes,
        }
