"""Notifier — v3:notifications Redis Stream 발행기 + 선택적 Slack mirror.

Fast loop의 체결/exit/risk_level_change 이벤트를 publisher(얇은 래퍼)로 발행.
Telegram bot / Control UI가 consumer로 구독.

envelope 없이 각 notification 모델을 그대로 JSON dump. `kind` Literal 필드로
consumer가 타입 분기.

Slack 통합:
- ``SLACK_WEBHOOK_URL`` 환경변수가 설정되어 있으면 critical level (entry,
  exit, stop, liquidate, risk_level_change critical) 알림을 Slack incoming
  webhook 으로 함께 발송한다.
- 환경변수 없으면 no-op — 기존 동작 그대로.
- 실패 시 stream 발행에는 영향 주지 않는다 (best-effort mirror).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import redis.asyncio as aioredis
from pydantic import BaseModel

from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS

logger = logging.getLogger(__name__)

# Slack 으로 미러링할 notification kind. exit/entry 체결은 항상 보내고
# risk_level_change 는 severity 가 critical 일 때만 (모델에 severity 없으면
# 무조건 보냄 — 보수적).
_SLACK_CRITICAL_KINDS = frozenset({"entry_filled", "exit_filled", "risk_level_change", "alert"})


class Notifier:
    """v3:notifications에 알림 발행."""

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        stream: str = STREAM_NOTIFICATIONS,
        maxlen: int = 10000,
        slack_webhook_url: str | None = None,
        slack_client: httpx.AsyncClient | None = None,
    ):
        self._client = client
        self._stream = stream
        self._maxlen = maxlen
        # 명시 인자 우선, 없으면 env 에서 읽기 (둘 다 비어있으면 Slack 비활성).
        self._slack_webhook_url = (
            slack_webhook_url
            if slack_webhook_url is not None
            else os.environ.get("SLACK_WEBHOOK_URL", "").strip() or None
        )
        # 테스트에서 mock client 주입. 미주입이면 첫 호출 시 생성.
        self._slack_client = slack_client
        self._owns_slack_client = slack_client is None

    @property
    def client(self) -> aioredis.Redis:
        """Coordinator hook 등 redis client 재사용 (별도 wiring 회피)."""
        return self._client

    async def emit(self, notification: BaseModel) -> str:
        payload = notification.model_dump_json()
        msg_id = await self._client.xadd(
            self._stream, {"payload": payload}, maxlen=self._maxlen, approximate=True
        )
        if isinstance(msg_id, bytes):
            msg_id = msg_id.decode()
        kind = getattr(notification, "kind", "?")
        logger.debug(
            "notification emitted stream=%s id=%s kind=%s",
            self._stream,
            msg_id,
            kind,
        )
        if self._slack_webhook_url and kind in _SLACK_CRITICAL_KINDS:
            await self._mirror_to_slack(notification, kind)
        return msg_id

    async def close(self) -> None:
        if self._owns_slack_client and self._slack_client is not None:
            await self._slack_client.aclose()
            self._slack_client = None

    # ------------------------------------------------------------------ slack

    async def _mirror_to_slack(self, notification: BaseModel, kind: str) -> None:
        """Slack incoming webhook 으로 critical 알림 미러. 실패는 로그만."""
        if self._slack_webhook_url is None:
            return
        try:
            text = _format_slack_text(notification, kind)
        except Exception:
            logger.warning("slack format failed", exc_info=True)
            return

        if self._slack_client is None:
            self._slack_client = httpx.AsyncClient(timeout=5.0)
            self._owns_slack_client = True

        try:
            resp = await self._slack_client.post(self._slack_webhook_url, json={"text": text})
            if resp.status_code >= 400:
                logger.warning("slack webhook returned %s: %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.warning("slack webhook delivery failed", exc_info=True)


def _format_slack_text(notification: BaseModel, kind: str) -> str:
    """모델별 간단 Slack 메시지 포맷.

    Slack 의 mrkdwn 만 사용 — 추가 attachments/blocks 없이도 가독성 확보.
    """
    data: dict[str, Any] = notification.model_dump(mode="json")
    if kind in {"entry_filled", "exit_filled"}:
        side = data.get("side", "?")
        ticker = data.get("ticker", "?")
        qty = data.get("quantity", 0)
        price = data.get("price", 0.0)
        reason = data.get("reason") or ""
        partial = " *partial*" if data.get("is_partial") else ""
        title = "Entry" if kind == "entry_filled" else "Exit"
        msg = f":chart_with_upwards_trend: *{title}* `{ticker}` {side} {qty} @ {price:,.2f}"
        if reason:
            msg += f" ({reason})"
        return msg + partial
    if kind == "risk_level_change":
        prev = data.get("prev_level", "?")
        new = data.get("new_level", "?")
        mult = data.get("new_multiplier", 0.0)
        return f":warning: *Risk level* `{prev}` → `{new}` (size_multiplier={mult:.2f})"
    if kind == "alert":
        sev = data.get("severity", "info")
        title = data.get("title", "alert")
        body = data.get("body", "")
        icon = ":rotating_light:" if sev == "critical" else ":bell:"
        return f"{icon} *{title}* ({sev}) — {body}"
    # 알 수 없는 kind — 그래도 raw dump 보냄
    return f":mailbox_with_mail: *{kind}* {data}"
