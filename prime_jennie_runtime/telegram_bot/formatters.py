"""Notification → Telegram HTML 메시지 포매터.

v2 `services/telegram/app.py:_format_trade_message` 확장.
3종 알림 (체결 / 리스크 / 일반)을 각각 한국어 HTML로 변환.

HTML parse_mode 사용 시 `<`, `>`, `&`는 텔레그램이 태그로 해석하므로
사용자 제공 문자열(ticker, title, body, reason)은 반드시 escape 필요.
"""

from __future__ import annotations

from html import escape

from prime_jennie_runtime.fast_loop.schemas import (
    GenericAlertNotification,
    RiskLevelChangeNotification,
    TradeNotification,
)

# ─── 이모지 ────────────────────────────────────────────────
EMOJI_ENTRY = "\u2705"  # ✅ 매수/진입
EMOJI_EXIT = "\U0001f6d1"  # 🛑 매도/청산
EMOJI_RISK_WARNING = "\u26a0\ufe0f"  # ⚠️
EMOJI_RISK_CRITICAL = "\U0001f6a8"  # 🚨
EMOJI_INFO = "\u2139\ufe0f"  # ℹ️


def _fmt_price(value: float) -> str:
    """가격을 천단위 콤마 포함 문자열로 (소수점 2자리 유지)."""
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _fmt_ts(notif_ts) -> str:
    """datetime → HH:MM:SS (KST 가정, v2와 동일)."""
    try:
        return notif_ts.strftime("%H:%M:%S")
    except AttributeError:
        return str(notif_ts)


def format_trade(notif: TradeNotification) -> str:
    """체결 알림 포매팅."""
    ticker = escape(notif.ticker)
    reason = escape(notif.reason) if notif.reason else ""
    side_ko = "매수" if notif.side == "buy" else "매도"

    if notif.kind == "entry_filled":
        header = f"{EMOJI_ENTRY} <b>[진입 체결]</b>"
    else:
        header = f"{EMOJI_EXIT} <b>[청산 체결]</b>"

    if notif.is_partial:
        header += " (부분)"

    lines = [
        header,
        f"{ticker} ({side_ko})",
        f"수량: {notif.quantity:,}주 | 가격: {_fmt_price(notif.price)}",
    ]
    if reason:
        lines.append(f"사유: {reason}")
    lines.append(f"시각: {_fmt_ts(notif.ts)}")
    return "\n".join(lines)


def format_risk_level_change(notif: RiskLevelChangeNotification) -> str:
    """Intraday risk level 변화 알림."""
    prev = escape(notif.prev_level)
    new = escape(notif.new_level)

    severity_rank = {"NORMAL": 0, "CAUTION": 1, "WARNING": 2, "DANGER": 3, "CRITICAL": 4}
    new_rank = severity_rank.get(notif.new_level, 0)
    prev_rank = severity_rank.get(notif.prev_level, 0)
    is_worsening = new_rank > prev_rank

    if new_rank >= severity_rank["DANGER"]:
        emoji = EMOJI_RISK_CRITICAL
    elif new_rank >= severity_rank["CAUTION"]:
        emoji = EMOJI_RISK_WARNING
    else:
        emoji = EMOJI_INFO

    direction = "악화" if is_worsening else "회복"
    lines = [
        f"{emoji} <b>[리스크 단계 {direction}]</b>",
        f"{prev} → {new}",
        f"배수: {notif.prev_multiplier:.2f} → {notif.new_multiplier:.2f}",
    ]

    metrics: list[str] = []
    if notif.kospi_change_pct is not None:
        metrics.append(f"KOSPI {notif.kospi_change_pct:+.2f}%")
    if notif.vix is not None:
        metrics.append(f"VIX {notif.vix:.1f}")
    if notif.sox_change_pct is not None:
        metrics.append(f"SOX {notif.sox_change_pct:+.2f}%")
    if metrics:
        lines.append(" · ".join(metrics))

    lines.append(f"시각: {_fmt_ts(notif.ts)}")
    return "\n".join(lines)


def format_generic_alert(notif: GenericAlertNotification) -> str:
    """일반 알림 (auto_override, gate_closed, hallucination, stale 등)."""
    title = escape(notif.title)
    body = escape(notif.body)

    if notif.severity == "critical":
        emoji = EMOJI_RISK_CRITICAL
        label = "CRITICAL"
    elif notif.severity == "warning":
        emoji = EMOJI_RISK_WARNING
        label = "WARNING"
    else:
        emoji = EMOJI_INFO
        label = "INFO"

    lines = [
        f"{emoji} <b>[{label}] {title}</b>",
        body,
        f"시각: {_fmt_ts(notif.ts)}",
    ]
    return "\n".join(lines)
