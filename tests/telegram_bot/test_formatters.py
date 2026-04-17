"""Formatter 테스트 — 3종 알림 → HTML."""

from __future__ import annotations

from datetime import datetime

from prime_jennie_runtime.fast_loop.schemas import (
    GenericAlertNotification,
    RiskLevelChangeNotification,
    TradeNotification,
)
from prime_jennie_runtime.telegram_bot.formatters import (
    format_generic_alert,
    format_risk_level_change,
    format_trade,
)

TS = datetime(2026, 4, 17, 10, 30, 45)


def test_format_trade_entry_filled():
    notif = TradeNotification(
        kind="entry_filled",
        sheet_id="sheet-1",
        ticker="005930",
        side="buy",
        quantity=10,
        price=71200.0,
        ts=TS,
    )
    msg = format_trade(notif)
    assert "\u2705" in msg  # ✅
    assert "[진입 체결]" in msg
    assert "005930" in msg
    assert "71,200" in msg
    assert "10" in msg


def test_format_trade_exit_filled_with_reason():
    notif = TradeNotification(
        kind="exit_filled",
        sheet_id="sheet-1",
        ticker="AAPL",
        side="sell",
        quantity=5,
        price=189.53,
        ts=TS,
        reason="stop_loss",
    )
    msg = format_trade(notif)
    assert "\U0001f6d1" in msg  # 🛑
    assert "[청산 체결]" in msg
    assert "stop_loss" in msg
    assert "189.53" in msg


def test_format_trade_partial_shows_tag():
    notif = TradeNotification(
        kind="exit_filled",
        sheet_id="sheet-1",
        ticker="AAPL",
        side="sell",
        quantity=2,
        price=189.0,
        ts=TS,
        reason="trailing",
        is_partial=True,
    )
    msg = format_trade(notif)
    assert "(부분)" in msg


def test_format_risk_level_change_worsening_caution():
    notif = RiskLevelChangeNotification(
        prev_level="NORMAL",
        new_level="CAUTION",
        prev_multiplier=1.0,
        new_multiplier=0.8,
        kospi_change_pct=-1.5,
        vix=22.0,
        ts=TS,
    )
    msg = format_risk_level_change(notif)
    assert "\u26a0\ufe0f" in msg  # ⚠️
    assert "NORMAL" in msg
    assert "CAUTION" in msg
    assert "악화" in msg
    assert "KOSPI -1.50%" in msg
    assert "VIX 22.0" in msg


def test_format_risk_level_change_critical_uses_siren():
    notif = RiskLevelChangeNotification(
        prev_level="DANGER",
        new_level="CRITICAL",
        prev_multiplier=0.3,
        new_multiplier=0.0,
        sox_change_pct=-3.2,
        ts=TS,
    )
    msg = format_risk_level_change(notif)
    assert "\U0001f6a8" in msg  # 🚨
    assert "CRITICAL" in msg


def test_format_risk_level_change_recovery():
    notif = RiskLevelChangeNotification(
        prev_level="WARNING",
        new_level="CAUTION",
        prev_multiplier=0.6,
        new_multiplier=0.8,
        ts=TS,
    )
    msg = format_risk_level_change(notif)
    assert "회복" in msg


def test_format_generic_alert_severity_mapping():
    critical = GenericAlertNotification(
        severity="critical",
        title="Gate Closed",
        body="position gate halted",
        ts=TS,
    )
    warning = GenericAlertNotification(
        severity="warning",
        title="Stale",
        body="price stale 30s",
        ts=TS,
    )
    info = GenericAlertNotification(
        severity="info",
        title="Info",
        body="startup",
        ts=TS,
    )
    critical_msg = format_generic_alert(critical)
    warning_msg = format_generic_alert(warning)
    info_msg = format_generic_alert(info)

    assert "\U0001f6a8" in critical_msg  # 🚨
    assert "[CRITICAL]" in critical_msg
    assert "\u26a0\ufe0f" in warning_msg  # ⚠️
    assert "[WARNING]" in warning_msg
    assert "\u2139\ufe0f" in info_msg  # ℹ️
    assert "[INFO]" in info_msg


def test_format_escapes_html_special_chars():
    """title/body/ticker/reason의 <, >, & 가 escape 되어야 함 (HTML parse_mode 안전)."""
    trade = TradeNotification(
        kind="exit_filled",
        sheet_id="sheet-1",
        ticker="A<B>&C",
        side="sell",
        quantity=1,
        price=100.0,
        ts=TS,
        reason="<injection>",
    )
    msg = format_trade(trade)
    assert "A&lt;B&gt;&amp;C" in msg
    assert "&lt;injection&gt;" in msg
    assert "<injection>" not in msg

    alert = GenericAlertNotification(
        severity="warning",
        title="a<b>",
        body="x&y",
        ts=TS,
    )
    alert_msg = format_generic_alert(alert)
    assert "a&lt;b&gt;" in alert_msg
    assert "x&amp;y" in alert_msg
