"""format_council_run_text / _html 출력 검증."""

from __future__ import annotations

from datetime import date, datetime

from prime_jennie_runtime.council_logging import (
    CouncilRunRecord,
    CouncilStepOutput,
    format_council_run_html,
    format_council_run_text,
)


def _full_record() -> CouncilRunRecord:
    return CouncilRunRecord(
        macro_run_id="macro_20260417_0800",
        generated_at=datetime(2026, 4, 17, 8, 0, 0),
        trigger_reason="scheduled_0800",
        gate="open",
        size_multiplier=1.15,
        reasoning="KOSPI 상승 추세 확인 + SOX 반등",
        confidence="high",
        cost_usd=0.21,
        latency_ms=3400,
        target_date=date(2026, 4, 17),
        steps=[
            CouncilStepOutput(name="strategist", output={"sentiment_score": 72}),
            CouncilStepOutput(name="chief_judge", output={"council_consensus": "agree"}),
        ],
        council_consensus="agree",
        trading_reasoning="반도체 리더십 복원, 외인 매도 흡수 중",
        strategies_to_favor=["MOMENTUM", "GOLDEN_CROSS"],
        strategies_to_avoid=["DIP_BUY"],
        sectors_to_favor=["반도체", "2차전지"],
        sectors_to_avoid=["건설", "유통"],
        opportunity_factors=["SOX 반등", "외인 순매수 전환"],
        risk_factors=["정치 리스크", "환율 변동"],
        final_sentiment="bullish",
        final_sentiment_score=72,
        final_regime_hint="uptrend_continuation",
        final_position_size_pct=110,
        final_stop_loss_adjust_pct=95,
        political_risk_level="medium",
    )


def test_format_text_contains_id_and_header():
    rec = _full_record()
    out = format_council_run_text(rec)
    assert "macro_20260417_0800" in out
    assert "gate=open" in out
    assert "size_x=1.15" in out
    assert "sentiment=bullish (72)" in out
    assert "position_pct=110" in out


def test_format_text_lists_sectors_and_strategies():
    out = format_council_run_text(_full_record())
    assert "반도체" in out and "2차전지" in out
    assert "MOMENTUM" in out
    assert "DIP_BUY" in out  # avoid 섹션에 들어감


def test_format_text_includes_reasoning_and_cost():
    out = format_council_run_text(_full_record())
    assert "판단 근거" not in out  # 텍스트는 한글 라벨 안 씀
    assert "trading_reasoning: 반도체 리더십" in out
    assert "cost_usd=0.2100" in out


def test_format_text_renders_error_when_not_success():
    rec = _full_record()
    rec.success = False
    rec.error = "LLM timeout"
    out = format_council_run_text(rec)
    assert "ERROR: LLM timeout" in out


def test_format_html_uses_html_tags_only():
    out = format_council_run_html(_full_record())
    # HTML only 태그
    assert "<b>" in out and "</b>" in out
    assert "<i>" in out
    assert "<code>" in out
    # 마크다운 금지
    assert "**" not in out
    assert "```" not in out


def test_format_html_escapes_special_chars():
    rec = _full_record()
    rec.trading_reasoning = "risk <script>alert(1)</script>"
    rec.sectors_to_favor = ["A&B", "<bad>"]
    out = format_council_run_html(rec)
    # 원본 위험 문자가 이스케이프되어 있어야
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "A&amp;B" in out
    assert "&lt;bad&gt;" in out


def test_format_html_renders_emoji_by_sentiment():
    rec = _full_record()
    out_bull = format_council_run_html(rec)
    assert "📈" in out_bull
    rec.final_sentiment = "bearish"
    out_bear = format_council_run_html(rec)
    assert "📉" in out_bear


def test_format_handles_minimal_record():
    rec = CouncilRunRecord(
        macro_run_id="macro_x",
        generated_at=datetime(2026, 4, 17, 8, 0),
        trigger_reason="manual",
        gate="closed",
        size_multiplier=0.0,
    )
    text_out = format_council_run_text(rec)
    html_out = format_council_run_html(rec)
    assert "macro_x" in text_out and "gate=closed" in text_out
    assert "macro_x" in html_out
    # 감정 없으면 기본 불릿 이모지
    assert "•" in html_out or "<b>" in html_out
