"""G1 outcome 피드백 — prompt 섹션 포맷 테스트."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from prime_jennie_runtime.slow_loop.scout.prompts import (
    SCOUT_PROMPT_VERSION,
    _fmt_outcomes_section,
    build_user_prompt,
)
from prime_jennie_runtime.slow_loop.scout.schemas import (
    MacroStateForScout,
    MarketSummary,
    PreviousOutcome,
    ScoutContext,
)


def _mk_outcome(
    *, ticker: str, tag: str, exit_reason: str, pnl_pct: float, hours_ago: int
) -> PreviousOutcome:
    now = datetime.now(UTC)
    return PreviousOutcome(
        ticker=ticker,
        strategy_tag=tag,
        generated_at=now - timedelta(hours=hours_ago + 24),
        exit_at=now - timedelta(hours=hours_ago),
        exit_reason=exit_reason,
        pnl_pct=pnl_pct,
    )


def test_prompt_version_bumped_to_v0_8():
    """prompt 내용 변경 시 버전 동반 bump 를 lock — 현재 v0.8 (G6 Phase A)."""
    assert SCOUT_PROMPT_VERSION == "v0.8"


def test_outcomes_section_empty():
    out = _fmt_outcomes_section([])
    assert "데이터 누적 중" in out


def test_outcomes_section_loss_breakdown():
    outcomes = [
        _mk_outcome(
            ticker="009540",
            tag="EARNINGS_DRIFT",
            exit_reason="fixed_sl",
            pnl_pct=-0.0517,
            hours_ago=4,
        ),
        _mk_outcome(
            ticker="001440",
            tag="SECTOR_MOMENTUM",
            exit_reason="fixed_sl",
            pnl_pct=-0.0396,
            hours_ago=3,
        ),
        _mk_outcome(
            ticker="011070",
            tag="SECTOR_MOMENTUM",
            exit_reason="trailing_tp",
            pnl_pct=0.0366,
            hours_ago=2,
        ),
    ]
    out = _fmt_outcomes_section(outcomes)
    # 손절율 2/3
    assert "2/3 = 67%" in out
    # tag breakdown — SECTOR_MOMENTUM 1/2, EARNINGS_DRIFT 1/1
    assert "SECTOR_MOMENTUM 1/2" in out
    assert "EARNINGS_DRIFT 1/1" in out
    # 최근 손절 ticker 노출
    assert "001440" in out
    assert "009540" in out
    # 익절 종목 (011070) 은 손절 목록엔 없음
    losses_line = next(line for line in out.split("\n") if "최근 손절" in line)
    assert "011070" not in losses_line


def test_outcomes_section_recent_5_cap():
    """손절 6건이면 최근 5건만 표시."""
    outcomes = [
        _mk_outcome(
            ticker=f"00000{i}",
            tag="EARNINGS_DRIFT",
            exit_reason="fixed_sl",
            pnl_pct=-0.05,
            hours_ago=i,
        )
        for i in range(6)
    ]
    out = _fmt_outcomes_section(outcomes)
    losses_line = next(line for line in out.split("\n") if "최근 손절" in line)
    # 가장 오래된 000005 는 제외
    assert "000005" not in losses_line


def test_build_user_prompt_includes_outcomes_section():
    """build_user_prompt 가 outcomes 섹션을 prompt 에 포함하는지."""
    ctx = ScoutContext(
        as_of=date(2026, 5, 15),
        universe=["005930"],
        market_summary=MarketSummary(
            kospi_close=7981.0,
            kospi_change_pct=-0.0411,
            kosdaq_close=2400.0,
            kosdaq_change_pct=-0.03,
            up_count=100,
            down_count=800,
        ),
        macro_state=MacroStateForScout(gate="open", size_multiplier=0.75, gate_run_id="m1"),
        news_events={},
        sector_momentum={},
        strategy_tags_available=["EARNINGS_DRIFT"],
        previous_outcomes=[
            _mk_outcome(
                ticker="009540",
                tag="EARNINGS_DRIFT",
                exit_reason="fixed_sl",
                pnl_pct=-0.0517,
                hours_ago=4,
            ),
        ],
    )
    prompt = build_user_prompt(ctx)
    assert "직전 7일 추천 outcomes" in prompt
    assert "009540" in prompt
    # informational 안내 문구 (enforcement 는 후행 layer)
    assert "결정론 차단" in prompt or "후행 layer" in prompt
