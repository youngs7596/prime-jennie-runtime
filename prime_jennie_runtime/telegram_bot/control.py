"""Control command 스키마 + Redis 키 / stream 상수.

`v3:control.commands` stream 을 Telegram bot 이 producer, fast/slow loop consumer
(Task 2.2) 가 consumer. State keys 는 consumer 가 writing, `/status` 명령이 read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# 명령 종류 — fast/slow loop consumer 가 해석하는 유일한 스위치
ControlKind = Literal[
    "emergency_stop",
    "pause",
    "resume",
    "set_dryrun",
    "liquidate_arm",
    "liquidate_disarm",
]


class ControlCommand(BaseModel):
    """Telegram / UI 가 발행하는 제어 명령."""

    kind: ControlKind
    issued_at: datetime
    issued_by: str  # e.g. "telegram:<chat_id>" 또는 "ui:<user>"
    reason: str | None = None
    # 명령별 부가 payload (set_dryrun=True/False 등). 과하게 쓰지 말 것.
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------
# Control state Redis keys (Task 2.2 consumer 가 쓰고 /status 가 읽음)
# ---------------------------------------------------------------------

# 긴급 정지 (모든 진입/청산 중단. 재개는 `/resume` 로만)
STATE_KEY_STOP = "control.state:stop"
# 일시 정지 (진입만 중단. 청산은 계속)
STATE_KEY_PAUSE = "control.state:pause"
# DRY_RUN: 실 주문 금지 (시뮬레이션만)
STATE_KEY_DRYRUN = "control.state:dryrun"
# 강제 청산 armed flag
STATE_KEY_LIQUIDATE_ARMED = "control.state:liquidate_armed"


def rate_limit_key(chat_id: str) -> str:
    return f"telegram:rl:{chat_id}"


# ---------------------------------------------------------------------
# 응답 템플릿 — 텔레그램 HTML 파싱 안전하게
# ---------------------------------------------------------------------

RESPONSE_HELP = (
    "<b>Prime Jennie v3 — 제어 명령</b>\n\n"
    "/status — 시스템 상태\n"
    "/pause [사유] — 진입 일시정지\n"
    "/resume — 재개\n"
    "/stop 확인 — 긴급 정지\n"
    "/dryrun on|off — 시뮬레이션 모드\n"
    "/liquidate arm|disarm|status — 강제 청산 준비\n"
    "/help — 이 도움말\n"
)

RESPONSE_UNKNOWN = "알 수 없는 명령입니다. /help"
RESPONSE_NOT_ALLOWED = "허용되지 않은 chat 입니다."
RESPONSE_RATE_LIMITED = "너무 빠릅니다. 잠시 후 다시 시도하세요."
RESPONSE_STOP_CONFIRM = "긴급 정지: <code>/stop 확인</code> 로만 실행됩니다."
RESPONSE_DRYRUN_USAGE = "사용법: <code>/dryrun on|off</code>"
RESPONSE_LIQUIDATE_USAGE = "사용법: <code>/liquidate arm|disarm|status</code>"

__all__ = [
    "RESPONSE_DRYRUN_USAGE",
    "RESPONSE_HELP",
    "RESPONSE_LIQUIDATE_USAGE",
    "RESPONSE_NOT_ALLOWED",
    "RESPONSE_RATE_LIMITED",
    "RESPONSE_STOP_CONFIRM",
    "RESPONSE_UNKNOWN",
    "STATE_KEY_DRYRUN",
    "STATE_KEY_LIQUIDATE_ARMED",
    "STATE_KEY_PAUSE",
    "STATE_KEY_STOP",
    "ControlCommand",
    "ControlKind",
    "rate_limit_key",
]
