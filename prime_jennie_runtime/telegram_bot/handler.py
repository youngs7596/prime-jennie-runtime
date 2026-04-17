"""Command Handler — Telegram 명령 → control.commands stream publish + 응답 문자열.

v2 ``services/telegram/handler.py`` 744줄의 **제어 서브셋만** v3 로 포팅.
Phase 2.1 scope: 진입/청산 제어 + 상태 조회. 수동 매매/워치리스트/가격 조회는
Phase 2.x 에서 별도 handler 로 분리 (DB/KIS client 의존 때문).

설계:
- **Pure producer**: 이 모듈은 Redis 만 의존. KIS/DB 미접근.
- **상태는 control.state:* 키에서 read-only**: Task 2.2 consumer 가 write.
- **Allowlist fail-safe**: ``config.allowed_chat_ids`` 가 비면 ``False`` 반환 →
  bot 기동 코드가 명시적으로 거부해야 한다 (D4 결정).
- **Rate limit**: chat 당 ``config.command_min_interval_s`` 초 간격.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as aioredis

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_CONTROL_COMMANDS,
    TypedStreamPublisher,
)

from .control import (
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
    ) -> None:
        self._redis = redis_client
        self._config = config
        self._now = now_fn or (lambda: datetime.now(UTC))
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
