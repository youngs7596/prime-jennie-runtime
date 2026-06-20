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
    # 실보유 종목을 v3 PositionTracker 관리에 편입 (사람-승인 매매 §3-2).
    # fast-loop 만 처리 — payload: sheet_id/ticker/entry_price/quantity/entered_at
    "adopt_position",
    # 추적 종료 — 등록된 조건부 주문 취소 (사람-승인 매매 §4 조회·취소).
    # fast-loop 만 처리 — payload: sheet_id/ticker (ticker 는 오발행 검증용)
    "untrack_position",
    # 추천 수락 매수 — echo + "확인" 2단계를 거친 사람-승인 진입 (시나리오 B).
    # fast-loop 만 처리 — payload: sheet_id/ticker/quantity.
    # 정책 (2026-06-12): STOP 차단, PAUSE 통과 — PAUSE 는 "무확인 진입 차단".
    "approved_buy",
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

# ---------------------------------------------------------------------
# v2 호환 trading flag 키 (legacy)
# ---------------------------------------------------------------------
# v3 가 control.state:* 로 마이그레이션됐지만 v2 운영 중 수동 SET (REAL_MODE_
# MIGRATION_CHECKLIST 참조) 으로 남는 키가 있어, /resume 시 함께 정리한다.
# v3 fast_loop 본체는 control.state:* 만 읽으므로 미정리 상태로는 영향 없음 —
# v2 가 영구 종료되면 이 두 상수는 제거 예정 (Phase 2-6 공존 종료).
# 자세한 매핑은 docs/CONTROL_STATE_KEYS.md 참조.
V2_KEY_STOP = "trading_flags:stop"
V2_KEY_PAUSE = "trading_flags:pause"

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

# 추천 번호 ↔ 시트 매핑 (시나리오 B) — slow-loop 가 쓰고 텔레그램 봇이 읽는다.
# HSET {번호: sheet_id}, 당일 TTL. 키는 KST 날짜로 일 단위 분리.
KEY_RECO_PREFIX = "v3:reco:"
# 수락 확인 대기 상태 — echo 시점의 확정 수량을 "확인" 까지 보존 (10분 TTL).
KEY_ACCEPT_PENDING_PREFIX = "v3:accept:pending:"
ACCEPT_PENDING_TTL_SEC = 600


def reco_key(kst_date_str: str) -> str:
    """``v3:reco:YYYY-MM-DD`` — 추천 번호 매핑 키."""
    return f"{KEY_RECO_PREFIX}{kst_date_str}"


def rate_limit_key(chat_id: str) -> str:
    return f"telegram:rl:{chat_id}"


# ---------------------------------------------------------------------
# 응답 템플릿 — 텔레그램 HTML 파싱 안전하게
# ---------------------------------------------------------------------

RESPONSE_HELP = (
    "<b>Prime Jennie v3 — 명령어</b>\n\n"
    "<b>매매 제어</b>\n"
    "/pause [사유] — 진입 일시정지\n"
    "/resume 확인 — 재개\n"
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
    "/sellall 확인 — 전량 청산\n"
    "/accept 번호 [수량] — 오늘의 추천 수락 매수 (수량 지정 시 그 주식 수로 제한)\n"
    "/adopt 종목 회복선% — 보유 편입 + 조건부 매도\n"
    "/adopt list — 조건부 주문 조회\n"
    "/adopt cancel 종목 — 조건부 주문 취소\n"
    "/dca status|arm preset|cancel — 지정 종목 분할매수\n\n"
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
# /resume 도 /stop /sellall 과 같은 확인 단계 요구 — 봇 재시작 시 텔레그램이 미확인
# 메시지를 재전달해 과거 /resume 이 맥락 없이 재실행되는 경로 (2026-06-10 전수조사
# G11) 와, kill switch 해제가 단발 명령인 비대칭을 함께 막는다.
RESPONSE_RESUME_CONFIRM = (
    "재개: <code>/resume 확인</code> 으로만 실행됩니다.\n(긴급정지·일시정지 해제 → 자동 매매 재개)"
)
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
    "ACCEPT_PENDING_TTL_SEC",
    "KEY_ACCEPT_PENDING_PREFIX",
    "KEY_FORCED_LIQUIDATION",
    "KEY_MANUAL_TRADE_PREFIX",
    "KEY_MAX_BUY_COUNT",
    "KEY_MUTE_UNTIL",
    "KEY_PRICE_ALERTS",
    "KEY_RECO_PREFIX",
    "KEY_WATCHLIST_MANUAL",
    "MANUAL_TRADE_DAILY_LIMIT",
    "RESPONSE_DRYRUN_USAGE",
    "RESPONSE_HELP",
    "RESPONSE_LIQUIDATE_USAGE",
    "RESPONSE_NOT_ALLOWED",
    "RESPONSE_RATE_LIMITED",
    "RESPONSE_RESUME_CONFIRM",
    "RESPONSE_STOP_CONFIRM",
    "RESPONSE_UNKNOWN",
    "STATE_KEY_DRYRUN",
    "STATE_KEY_LIQUIDATE_ARMED",
    "STATE_KEY_PAUSE",
    "STATE_KEY_STOP",
    "V2_KEY_PAUSE",
    "V2_KEY_STOP",
    "ControlCommand",
    "ControlKind",
    "rate_limit_key",
    "reco_key",
]
