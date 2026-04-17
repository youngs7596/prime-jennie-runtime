"""Council 레코드 → 텔레그램/대시보드용 텍스트·HTML 포매터.

텔레그램은 parse_mode=HTML 로 송출되므로 <b>/<i>/<code>/<pre> 만 사용.
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape

from .records import CouncilRunRecord


def _sentiment_emoji(sentiment: str | None) -> str:
    """v2 대시보드와 동일한 매핑."""
    return {
        "bullish": "📈",
        "neutral_to_bullish": "↗️",
        "neutral": "→",
        "neutral_to_bearish": "↘️",
        "bearish": "📉",
    }.get(sentiment or "", "•")


def _fmt_list(items: Iterable[str], *, sep: str = ", ", limit: int = 8) -> str:
    seq = [str(x) for x in items if x]
    if not seq:
        return "-"
    if len(seq) > limit:
        seq = seq[:limit] + [f"… (+{len(seq) - limit})"]
    return sep.join(seq)


def format_council_run_text(record: CouncilRunRecord) -> str:
    """콘솔/로그/CSV 주석용 플레인 텍스트."""
    lines: list[str] = []
    lines.append(f"[Council] {record.macro_run_id} @ {record.generated_at.isoformat()}")
    lines.append(
        f"  trigger={record.trigger_reason}  gate={record.gate}"
        f"  size_x={record.size_multiplier:.2f}"
    )
    if record.final_sentiment:
        score = record.final_sentiment_score if record.final_sentiment_score is not None else "-"
        lines.append(
            f"  sentiment={record.final_sentiment} ({score})"
            f"  regime={record.final_regime_hint or '-'}"
        )
    if record.final_position_size_pct is not None or record.final_stop_loss_adjust_pct is not None:
        lines.append(
            f"  position_pct={record.final_position_size_pct or '-'}  "
            f"stop_loss_pct={record.final_stop_loss_adjust_pct or '-'}"
        )
    if record.council_consensus:
        lines.append(f"  consensus={record.council_consensus}")
    if record.sectors_to_favor:
        lines.append(f"  favor_sectors: {_fmt_list(record.sectors_to_favor)}")
    if record.sectors_to_avoid:
        lines.append(f"  avoid_sectors: {_fmt_list(record.sectors_to_avoid)}")
    if record.strategies_to_favor:
        lines.append(f"  favor_strategies: {_fmt_list(record.strategies_to_favor)}")
    if record.strategies_to_avoid:
        lines.append(f"  avoid_strategies: {_fmt_list(record.strategies_to_avoid)}")
    if record.opportunity_factors:
        lines.append(f"  opportunities: {_fmt_list(record.opportunity_factors, limit=5)}")
    if record.risk_factors:
        lines.append(f"  risks: {_fmt_list(record.risk_factors, limit=5)}")
    if record.reasoning:
        lines.append(f"  reasoning: {record.reasoning}")
    if record.trading_reasoning:
        lines.append(f"  trading_reasoning: {record.trading_reasoning}")
    cost = record.total_cost_usd()
    if cost:
        lines.append(f"  cost_usd={cost:.4f}  latency_ms={record.latency_ms or '-'}")
    if not record.success and record.error:
        lines.append(f"  ERROR: {record.error}")
    return "\n".join(lines)


def format_council_run_html(record: CouncilRunRecord) -> str:
    """텔레그램/대시보드용 HTML. parse_mode=HTML 전용 태그만 사용."""
    emoji = _sentiment_emoji(record.final_sentiment)
    parts: list[str] = []
    parts.append(f"<b>{emoji} Macro Council</b> — <code>{escape(record.macro_run_id)}</code>")
    parts.append(f"<i>{escape(record.generated_at.strftime('%Y-%m-%d %H:%M'))}</i>")

    header_lines: list[str] = []
    header_lines.append(f"gate: <b>{escape(record.gate)}</b> | size×{record.size_multiplier:.2f}")
    if record.final_sentiment:
        score = record.final_sentiment_score if record.final_sentiment_score is not None else "-"
        header_lines.append(f"sentiment: <b>{escape(record.final_sentiment)}</b> ({score})")
    if record.final_regime_hint:
        header_lines.append(f"regime: {escape(record.final_regime_hint)}")
    if record.final_position_size_pct is not None:
        header_lines.append(
            f"position: {record.final_position_size_pct}% | "
            f"stop: {record.final_stop_loss_adjust_pct or '-'}%"
        )
    if record.council_consensus:
        header_lines.append(f"consensus: <i>{escape(record.council_consensus)}</i>")
    if header_lines:
        parts.append("\n".join(header_lines))

    if record.sectors_to_favor:
        parts.append(f"<b>📈 favor sectors</b>\n{escape(_fmt_list(record.sectors_to_favor))}")
    if record.sectors_to_avoid:
        parts.append(f"<b>📉 avoid sectors</b>\n{escape(_fmt_list(record.sectors_to_avoid))}")
    if record.strategies_to_favor:
        parts.append(f"<b>✅ strategies</b>\n{escape(_fmt_list(record.strategies_to_favor))}")
    if record.strategies_to_avoid:
        parts.append(f"<b>⛔ avoid strategies</b>\n{escape(_fmt_list(record.strategies_to_avoid))}")
    if record.opportunity_factors:
        items = "\n".join(f"• {escape(x)}" for x in record.opportunity_factors[:5])
        parts.append(f"<b>기회 요인</b>\n{items}")
    if record.risk_factors:
        items = "\n".join(f"• {escape(x)}" for x in record.risk_factors[:5])
        parts.append(f"<b>리스크</b>\n{items}")
    if record.trading_reasoning:
        parts.append(f"<b>판단 근거</b>\n{escape(record.trading_reasoning)}")
    elif record.reasoning:
        parts.append(f"<b>판단 근거</b>\n{escape(record.reasoning)}")

    cost = record.total_cost_usd()
    if cost:
        parts.append(f"<i>cost ${cost:.4f} • latency {record.latency_ms or '-'}ms</i>")
    if not record.success and record.error:
        parts.append(f"<b>⚠ error</b>: {escape(record.error)}")

    return "\n\n".join(parts)
