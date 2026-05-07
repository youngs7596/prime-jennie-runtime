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
    "liquidate_add",
    "liquidate_remove",
    "liquidate_clear",
    "manual_buy",
    "manual_sell",
    "manual_sellall",
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

# v2 호환 키 — 알림 음소거 / 가격 알림 / 최대 매수 횟수 / 워치리스트
KEY_MUTE_UNTIL = "notification:mute_until"
KEY_PRICE_ALERTS = "price_alerts"
KEY_MAX_BUY_COUNT = "config:max_buy_count"
KEY_WATCHLIST_MANUAL = "watchlist:manual"

# 강제 청산 대상 Redis Set (v2 호환). fast-loop 의 ControlConsumer 가 멤버 추가/제거.
KEY_FORCED_LIQUIDATION = "forced_liquidation:stocks"

# 일일 수동 매매 카운터 prefix — telegram:manual_trades:{YYYY-MM-DD}:{chat_id}
KEY_MANUAL_TRADE_PREFIX = "telegram:manual_trades:"
MANUAL_TRADE_DAILY_LIMIT = 20


def rate_limit_key(chat_id: str) -> str:
    return f"telegram:rl:{chat_id}"


# ---------------------------------------------------------------------
# 응답 템플릿 — 텔레그램 HTML 파싱 안전하게
# ---------------------------------------------------------------------

RESPONSE_HELP = (
    "<b>Prime Jennie v3 — 명령어</b>\n\n"
    "<b>매매 제어</b>\n"
    "/pause [사유] — 진입 일시정지\n"
    "/resume — 재개\n"
    "/stop 확인 — 긴급 정지\n"
    "/dryrun on|off — 시뮬레이션 모드\n\n"
    "<b>조회</b>\n"
    "/status — 시스템 상태\n"
    "/portfolio — 보유 종목\n"
    "/pnl — 오늘 손익\n"
    "/balance — 현금 잔고\n"
    "/price 종목 — 현재가\n\n"
    "<b>워치리스트</b>\n"
    "/watchlist — 목록\n"
    "/watch 종목 — 추가\n"
    "/unwatch 종목 — 제거\n\n"
    "<b>알림</b>\n"
    "/mute 분 — 음소거\n"
    "/unmute — 재개\n"
    "/alert 종목 가격 — 가격 알림\n"
    "/alerts — 알림 목록\n\n"
    "<b>설정</b>\n"
    "/config — 현재 설정\n"
    "/maxbuy N — 일일 최대 매수 (0~20)\n\n"
    "<b>매매</b>\n"
    "/buy 종목 [수량] — 수동 매수\n"
    "/sell 종목 [수량|전량] — 수동 매도\n"
    "/sellall 확인 — 전량 청산\n\n"
    "<b>강제 청산</b>\n"
    "/liquidate add|remove|list|clear|arm|disarm|status\n\n"
    "<b>진단</b>\n"
    "/diagnose — 시스템 진단\n"
    "/help — 이 도움말\n"
)

RESPONSE_UNKNOWN = "알 수 없는 명령입니다. /help"
RESPONSE_NOT_ALLOWED = "허용되지 않은 chat 입니다."
RESPONSE_RATE_LIMITED = "너무 빠릅니다. 잠시 후 다시 시도하세요."
RESPONSE_STOP_CONFIRM = "긴급 정지: <code>/stop 확인</code> 로만 실행됩니다."
RESPONSE_DRYRUN_USAGE = "사용법: <code>/dryrun on|off</code>"
RESPONSE_LIQUIDATE_USAGE = (
    "사용법:\n"
    "<code>/liquidate add 종목</code> — 대상 추가\n"
    "<code>/liquidate remove 종목</code> — 대상 제거\n"
    "<code>/liquidate list</code> — 대상 목록\n"
    "<code>/liquidate clear</code> — 전체 초기화\n"
    "<code>/liquidate arm</code> — 스위치 ON\n"
    "<code>/liquidate disarm</code> — 스위치 OFF\n"
    "<code>/liquidate status</code> — 상태 조회"
)

__all__ = [
    "KEY_FORCED_LIQUIDATION",
    "KEY_MANUAL_TRADE_PREFIX",
    "KEY_MAX_BUY_COUNT",
    "KEY_MUTE_UNTIL",
    "KEY_PRICE_ALERTS",
    "KEY_WATCHLIST_MANUAL",
    "MANUAL_TRADE_DAILY_LIMIT",
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
