"""Command Handler — Telegram 명령 → control.commands stream publish + 응답 문자열.

v2 ``services/telegram/handler.py`` 744줄을 v3 으로 포팅.

설계:
- 제어 명령 (pause/resume/stop/dryrun/liquidate) 은 control.commands stream
  으로 publish — fast/slow loop 의 ControlConsumer 가 적용.
- 조회 명령 (balance/portfolio/price/pnl 등) 은 KIS gateway HTTP + PG 직접
  쿼리. ``pool`` / ``kis_client`` 가 None 이면 degrade 응답.
- 매매 명령 (buy/sell/sellall) 은 manual_buy/manual_sell/manual_sellall
  ControlKind 로 publish.
- **Allowlist fail-safe**: ``config.allowed_chat_ids`` 가 비면 ``is_allowed``
  False 반환 (D4).
- **Rate limit**: chat 당 ``config.command_min_interval_s`` 초 간격.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import redis.asyncio as aioredis

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_CONTROL_COMMANDS,
    TypedStreamPublisher,
)

if TYPE_CHECKING:
    import asyncpg

    from prime_jennie_runtime.fast_loop.kis_client import KisClient

from .control import (
    KEY_MAX_BUY_COUNT,
    KEY_MUTE_UNTIL,
    KEY_PRICE_ALERTS,
    RESPONSE_DRYRUN_USAGE,
    RESPONSE_HELP,
    RESPONSE_LIQUIDATE_USAGE,
    RESPONSE_NOT_ALLOWED,
    RESPONSE_RATE_LIMITED,
    RESPONSE_STOP_CONFIRM,
    RESPONSE_UNKNOWN,
    STATE_KEY_DRYRUN,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    ControlCommand,
    ControlKind,
    rate_limit_key,
)
from .stock_resolver import resolve_stock

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """process_command 반환 — reply 문자열 + publish 여부."""

    reply: str
    published: ControlCommand | None = None


class CommandHandler:
    """Telegram 명령 처리기 — 파싱 + allowlist + rate limit + stream publish."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        config: TelegramConfig,
        now_fn: Callable[[], datetime] | None = None,
        *,
        pool: asyncpg.Pool | None = None,
        kis_client: KisClient | None = None,
    ) -> None:
        self._redis = redis_client
        self._config = config
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._pool = pool
        self._kis = kis_client
        self._publisher: TypedStreamPublisher[ControlCommand] = TypedStreamPublisher(
            redis_client, STREAM_CONTROL_COMMANDS, ControlCommand
        )
        self._handlers: dict[str, _Handler] = {
            "/help": self._handle_help,
            "/status": self._handle_status,
            "/pause": self._handle_pause,
            "/resume": self._handle_resume,
            "/stop": self._handle_stop,
            "/dryrun": self._handle_dryrun,
            "/liquidate": self._handle_liquidate,
            "/mute": self._handle_mute,
            "/unmute": self._handle_unmute,
            "/alert": self._handle_alert,
            "/alerts": self._handle_alerts,
            "/maxbuy": self._handle_maxbuy,
            "/config": self._handle_config,
        }

    def is_allowed(self, chat_id: str | int) -> bool:
        allowed = self._config.allowed_chat_ids
        if not allowed:  # fail-safe: 빈 allowlist → 거부
            return False
        return str(chat_id) in set(allowed)

    async def process_command(
        self,
        command: str,
        args: str,
        chat_id: str | int,
        username: str = "",
    ) -> CommandResult:
        """명령 라우팅. 알 수 없는 명령이면 unknown reply."""
        if not self.is_allowed(chat_id):
            logger.warning("Rejected chat_id=%s command=%s", chat_id, command)
            return CommandResult(reply=RESPONSE_NOT_ALLOWED)

        if await self._rate_limited(str(chat_id)):
            return CommandResult(reply=RESPONSE_RATE_LIMITED)

        handler = self._handlers.get(command)
        if handler is None:
            return CommandResult(reply=RESPONSE_UNKNOWN)

        try:
            return await handler(args, chat_id=str(chat_id), username=username)
        except Exception:
            logger.exception("command handler crashed: %s", command)
            return CommandResult(reply="명령 실행 실패 (로그 확인)")

    # ----- rate limit -----

    async def _rate_limited(self, chat_id: str) -> bool:
        key = rate_limit_key(chat_id)
        interval = self._config.command_min_interval_s
        if interval <= 0:
            return False
        # SET key 1 NX EX interval — key 존재 시 실패 → rate limited
        result = await self._redis.set(key, b"1", nx=True, ex=interval)
        return not result

    # ----- publish helper -----

    async def _publish(
        self,
        kind: ControlKind,
        chat_id: str,
        *,
        reason: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> ControlCommand:
        cmd = ControlCommand(
            kind=kind,
            issued_at=self._now(),
            issued_by=f"telegram:{chat_id}",
            reason=reason,
            payload=dict(payload or {}),
        )
        await self._publisher.publish(cmd)
        return cmd

    # ----- handlers -----

    async def _handle_help(self, args: str, **kwargs: object) -> CommandResult:
        return CommandResult(reply=RESPONSE_HELP)

    async def _handle_status(self, args: str, **kwargs: object) -> CommandResult:
        stop = await self._redis.get(STATE_KEY_STOP)
        pause = await self._redis.get(STATE_KEY_PAUSE)
        dry = await self._redis.get(STATE_KEY_DRYRUN)
        armed = await self._redis.get(STATE_KEY_LIQUIDATE_ARMED)

        def _fmt(v: bytes | str | None) -> str:
            return "—" if not v else (v.decode() if isinstance(v, bytes) else str(v))

        reply = (
            "<b>Prime Jennie v3 상태</b>\n"
            f"긴급정지: {_on_off(stop)}\n"
            f"일시정지: {_fmt(pause)}\n"
            f"DRY_RUN: {_on_off(dry)}\n"
            f"강제청산 armed: {_on_off(armed)}\n"
            f"시각: {self._now().strftime('%Y-%m-%d %H:%M:%SZ')}"
        )
        return CommandResult(reply=reply)

    async def _handle_pause(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        reason = args.strip() or "manual_pause"
        cmd = await self._publish("pause", chat_id, reason=reason)
        return CommandResult(
            reply=f"진입 일시정지 요청 발행됨 (사유: {reason})",
            published=cmd,
        )

    async def _handle_resume(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        cmd = await self._publish("resume", chat_id)
        return CommandResult(reply="재개 요청 발행됨", published=cmd)

    async def _handle_stop(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        if args.strip() != "확인":
            return CommandResult(reply=RESPONSE_STOP_CONFIRM)
        cmd = await self._publish("emergency_stop", chat_id, reason="telegram_emergency")
        return CommandResult(
            reply="<b>긴급 정지 요청 발행됨</b>\n재개: <code>/resume</code>",
            published=cmd,
        )

    async def _handle_dryrun(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        arg = args.strip().lower()
        if arg not in ("on", "off"):
            return CommandResult(reply=RESPONSE_DRYRUN_USAGE)
        enabled = arg == "on"
        cmd = await self._publish("set_dryrun", chat_id, payload={"enabled": enabled})
        return CommandResult(
            reply=f"DRY_RUN {'ON (시뮬레이션)' if enabled else 'OFF (실거래)'} 요청 발행됨",
            published=cmd,
        )

    async def _handle_liquidate(self, args: str, chat_id: str = "", **_: object) -> CommandResult:
        arg = args.strip().lower()
        if arg == "arm":
            cmd = await self._publish("liquidate_arm", chat_id)
            return CommandResult(reply="강제 청산 armed (실행은 fast loop 에서)", published=cmd)
        if arg == "disarm":
            cmd = await self._publish("liquidate_disarm", chat_id)
            return CommandResult(reply="강제 청산 disarm", published=cmd)
        if arg == "status":
            armed = await self._redis.get(STATE_KEY_LIQUIDATE_ARMED)
            return CommandResult(reply=f"강제 청산 armed: {_on_off(armed)}")
        return CommandResult(reply=RESPONSE_LIQUIDATE_USAGE)

    # ----- 알림 음소거 -----

    async def _handle_mute(self, args: str, **_: object) -> CommandResult:
        try:
            minutes = int(args.strip())
        except (ValueError, TypeError):
            return CommandResult(reply="사용법: <code>/mute 분</code> (예: /mute 30)")
        if minutes <= 0:
            return CommandResult(reply="분은 1 이상의 정수")
        until = int(time.time()) + minutes * 60
        await self._redis.set(KEY_MUTE_UNTIL, str(until).encode(), ex=minutes * 60 + 60)
        return CommandResult(reply=f"알림을 {minutes}분간 음소거합니다.")

    async def _handle_unmute(self, args: str, **_: object) -> CommandResult:
        await self._redis.delete(KEY_MUTE_UNTIL)
        return CommandResult(reply="알림이 재개됩니다.")

    # ----- 가격 알림 -----

    async def _handle_alert(self, args: str, **_: object) -> CommandResult:
        parts = args.strip().split()
        if len(parts) < 2:
            return CommandResult(reply="사용법: <code>/alert 종목 가격</code>")

        stock = await resolve_stock(self._pool, parts[0])
        if stock is None:
            return CommandResult(reply=f"종목을 찾을 수 없습니다: {parts[0]}")
        try:
            target_price = int(parts[1].replace(",", ""))
        except ValueError:
            return CommandResult(reply="가격은 숫자로 입력하세요.")
        if target_price <= 0:
            return CommandResult(reply="가격은 양수여야 합니다.")

        code, name = stock
        alert = {
            "stock_code": code,
            "stock_name": name,
            "target_price": target_price,
            "created_at": self._now().isoformat(),
        }
        await self._redis.hset(
            KEY_PRICE_ALERTS, f"{code}:{target_price}", json.dumps(alert).encode()
        )
        await self._redis.expire(KEY_PRICE_ALERTS, 7 * 86400)
        return CommandResult(reply=f"가격 알림 설정: {name}({code}) → {target_price:,}원")

    async def _handle_alerts(self, args: str, **_: object) -> CommandResult:
        raw = await self._redis.hgetall(KEY_PRICE_ALERTS)
        if not raw:
            return CommandResult(reply="설정된 알림이 없습니다.")
        lines = ["<b>가격 알림 목록</b>"]
        for _key, val in sorted(raw.items()):
            try:
                data = json.loads(val.decode() if isinstance(val, bytes) else str(val))
                lines.append(
                    f"  {data['stock_name']}({data['stock_code']}) → {data['target_price']:,}원"
                )
            except (ValueError, KeyError):
                continue
        return CommandResult(reply="\n".join(lines))

    # ----- 설정 -----

    async def _handle_maxbuy(self, args: str, **_: object) -> CommandResult:
        try:
            val = int(args.strip())
        except (ValueError, TypeError):
            return CommandResult(reply="사용법: <code>/maxbuy 횟수</code> (0~20)")
        if not 0 <= val <= 20:
            return CommandResult(reply="0~20 사이 값을 입력하세요.")
        await self._redis.set(KEY_MAX_BUY_COUNT, str(val).encode())
        return CommandResult(reply=f"일일 최대 매수: {val}회로 변경")

    async def _handle_config(self, args: str, **_: object) -> CommandResult:
        pause = await self._redis.get(STATE_KEY_PAUSE)
        stop = await self._redis.get(STATE_KEY_STOP)
        dry = await self._redis.get(STATE_KEY_DRYRUN)
        mute_until = await self._redis.get(KEY_MUTE_UNTIL)
        max_buy = await self._redis.get(KEY_MAX_BUY_COUNT)

        mute_str = "OFF"
        if mute_until:
            try:
                until_ts = int(mute_until.decode() if isinstance(mute_until, bytes) else mute_until)
                remaining = until_ts - int(time.time())
                if remaining > 0:
                    mute_str = f"{remaining // 60}분 남음"
            except ValueError:
                pass

        max_buy_str = (
            (max_buy.decode() if isinstance(max_buy, bytes) else str(max_buy))
            if max_buy
            else "기본값"
        )

        reply = (
            "<b>현재 설정</b>\n"
            f"긴급정지: {_on_off(stop)}\n"
            f"일시정지: {pause.decode() if isinstance(pause, bytes) else (pause or 'OFF')}\n"
            f"DRY_RUN: {_on_off(dry)}\n"
            f"알림 음소거: {mute_str}\n"
            f"일일 최대 매수: {max_buy_str}회"
        )
        return CommandResult(reply=reply)


def _on_off(v: bytes | str | None) -> str:
    if not v:
        return "OFF"
    return "ON"


# 핸들러 시그니처: (args, chat_id, username) → CommandResult
_Handler = Callable[..., Awaitable[CommandResult]]


def parse_command(text: str) -> tuple[str, str] | None:
    """``/command rest of args`` 를 (command, args) 튜플로.

    봇 이름 mention (``/status@MyBot``) 도 처리. 슬래시로 시작 안 하면 None.
    """
    text = text.strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    return cmd, rest.strip()


__all__ = ["CommandHandler", "CommandResult", "parse_command"]
