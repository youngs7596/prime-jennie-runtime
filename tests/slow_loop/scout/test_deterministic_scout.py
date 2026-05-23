"""결정론 scout 오케스트레이터 — 순수 헬퍼 + db 없는 경로 테스트.

DB 의존 부분(enrich_universe / 점수 이력)은 fixture 없이 검증 불가하므로
strategy_tag 배정·candidate 매핑·섹터 백분위 등 순수 로직 + db_engine=None 경로만.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from prime_jennie_runtime.slow_loop.scout.deterministic_scout import (
    _assign_strategy_tag,
    _compute_sector_pctiles,
    _effective_per,
    _percentile_rank,
    _to_screening_candidate,
    run_deterministic_scout,
)
from prime_jennie_runtime.slow_loop.scout.enrichment import (
    ConsensusInfo,
    DailyPrice,
    EnrichedCandidate,
    FinancialTrend,
    StockMaster,
)
from prime_jennie_runtime.slow_loop.scout.quant import QuantScore
from prime_jennie_runtime.slow_loop.scout.schemas import (
    MacroStateForScout,
    MarketSummary,
    ScoutContext,
)

# ─── fixtures ────────────────────────────────────────────────────


def _master(code: str = "005930", sector: str | None = "반도체/IT") -> StockMaster:
    return StockMaster(stock_code=code, stock_name=f"종목{code}", sector_group=sector)


def _prices(closes: list[int]) -> list[DailyPrice]:
    return [
        DailyPrice(
            stock_code="005930",
            price_date=date(2026, 5, 1),
            open_price=c,
            high_price=c + 100,
            low_price=c - 100,
            close_price=c,
            volume=1_000_000,
        )
        for c in closes
    ]


def _candidate(
    code: str = "005930",
    closes: list[int] | None = None,
    consensus: ConsensusInfo | None = None,
    ft: FinancialTrend | None = None,
) -> EnrichedCandidate:
    return EnrichedCandidate(
        master=_master(code),
        daily_prices=_prices(closes) if closes else [],
        consensus=consensus,
        financial_trend=ft,
    )


def _quant(code: str = "005930", total: float = 55.0) -> QuantScore:
    """검증 통과하는 QuantScore — 서브점수 합 55 (V2_NEUTRAL)."""
    return QuantScore(
        stock_code=code,
        stock_name=f"종목{code}",
        total_score=total,
        momentum_score=10.0,
        quality_score=10.0,
        value_score=10.0,
        technical_score=5.0,
        news_score=5.0,
        supply_demand_score=10.0,
        sector_momentum_score=5.0,
    )


# ─── _assign_strategy_tag ────────────────────────────────────────


class TestAssignStrategyTag:
    def test_oversold_rsi_to_mean_revert(self):
        """강한 하락추세(RSI≤30) → MEAN_REVERT_RSI."""
        closes = [300 - i * 3 for i in range(30)]  # 단조 하락
        assert _assign_strategy_tag(_candidate(closes=closes)) == "MEAN_REVERT_RSI"

    def test_eps_revision_to_earnings_drift(self):
        """RSI 정상 + eps_revision≥5 → EARNINGS_DRIFT."""
        closes = [100 + i for i in range(30)]  # 상승추세 (RSI 높음, 과매도 아님)
        cand = _candidate(closes=closes, consensus=ConsensusInfo(eps_revision_pct=8.0))
        assert _assign_strategy_tag(cand) == "EARNINGS_DRIFT"

    def test_default_sector_momentum(self):
        """과매도도 실적상향도 아니면 → SECTOR_MOMENTUM."""
        closes = [100 + i for i in range(30)]
        assert _assign_strategy_tag(_candidate(closes=closes)) == "SECTOR_MOMENTUM"

    def test_oversold_wins_over_eps(self):
        """과매도가 eps_revision 보다 우선."""
        closes = [300 - i * 3 for i in range(30)]
        cand = _candidate(closes=closes, consensus=ConsensusInfo(eps_revision_pct=10.0))
        assert _assign_strategy_tag(cand) == "MEAN_REVERT_RSI"

    def test_rsi_between_30_and_35_not_mean_revert(self, monkeypatch):
        """RSI 30 < x ≤ 35 구간은 MEAN_REVERT_RSI 아님 (2026-05-24 정정).

        옛 prompt 가이드의 RSI ≤ 30 임계로 정렬.
        """
        from prime_jennie_runtime.slow_loop.scout import deterministic_scout

        monkeypatch.setattr(deterministic_scout, "_compute_rsi", lambda *a, **kw: 32.0)
        cand = _candidate(closes=[100 + i for i in range(30)])
        assert _assign_strategy_tag(cand) == "SECTOR_MOMENTUM"

    def test_rsi_30_boundary_is_mean_revert(self, monkeypatch):
        """RSI = 30 boundary 는 MEAN_REVERT_RSI (inclusive)."""
        from prime_jennie_runtime.slow_loop.scout import deterministic_scout

        monkeypatch.setattr(deterministic_scout, "_compute_rsi", lambda *a, **kw: 30.0)
        cand = _candidate(closes=[100 + i for i in range(30)])
        assert _assign_strategy_tag(cand) == "MEAN_REVERT_RSI"

    def test_short_data_no_rsi_default(self):
        """일봉 15일 미만이면 RSI 미적용 → 기본 SECTOR_MOMENTUM."""
        assert _assign_strategy_tag(_candidate(closes=[100, 101])) == "SECTOR_MOMENTUM"

    def test_never_gap_up_rebound(self):
        """결정론 일봉 스코어러는 GAP_UP_REBOUND 를 배정하지 않는다."""
        for closes in ([300 - i * 3 for i in range(30)], [100 + i for i in range(30)]):
            assert _assign_strategy_tag(_candidate(closes=closes)) != "GAP_UP_REBOUND"


# ─── _to_screening_candidate ─────────────────────────────────────


class TestToScreeningCandidate:
    def test_conviction_is_ma_over_100(self):
        cand = _candidate(closes=[100 + i for i in range(30)])
        sc = _to_screening_candidate(cand, _quant(), ma_score=72.0)
        assert sc.conviction == pytest.approx(0.72)
        assert sc.ticker == "005930"
        assert sc.entry_hint.trigger == "market"
        assert sc.exit_hint is None

    def test_conviction_clamped_high(self):
        cand = _candidate(closes=[100 + i for i in range(30)])
        sc = _to_screening_candidate(cand, _quant(), ma_score=150.0)
        assert sc.conviction == 1.0

    def test_conviction_clamped_low(self):
        cand = _candidate(closes=[100 + i for i in range(30)])
        sc = _to_screening_candidate(cand, _quant(), ma_score=-10.0)
        assert sc.conviction == 0.0

    def test_factors_carry_subscores(self):
        cand = _candidate(closes=[100 + i for i in range(30)])
        sc = _to_screening_candidate(cand, _quant(total=55.0), ma_score=60.0)
        assert sc.factors["quant_total"] == 55.0
        assert sc.factors["ma_score"] == 60.0
        assert "momentum" in sc.factors


# ─── 섹터 백분위 ─────────────────────────────────────────────────


class TestPercentileRank:
    def test_lowest_value_low_pctile(self):
        assert _percentile_rank(1.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == 10.0

    def test_highest_value_high_pctile(self):
        assert _percentile_rank(5.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == 90.0

    def test_empty_returns_50(self):
        assert _percentile_rank(3.0, []) == 50.0


class TestEffectivePer:
    def test_forward_per_preferred(self):
        cand = _candidate(ft=FinancialTrend(per=20.0), consensus=ConsensusInfo(forward_per=12.0))
        assert _effective_per(cand) == 12.0

    def test_trailing_per_fallback(self):
        assert _effective_per(_candidate(ft=FinancialTrend(per=15.0))) == 15.0

    def test_none_when_no_data(self):
        assert _effective_per(_candidate()) is None


class TestComputeSectorPctiles:
    def test_pctiles_set_when_5plus_samples(self):
        """섹터 내 5종목 이상 → 백분위 산출."""
        enriched = {
            f"00000{i}": EnrichedCandidate(
                master=_master(f"00000{i}", sector="반도체/IT"),
                financial_trend=FinancialTrend(per=float(10 + i), pbr=float(1 + i)),
            )
            for i in range(5)
        }
        _compute_sector_pctiles(enriched)
        assert all(c.sector_per_pctile is not None for c in enriched.values())
        assert all(c.sector_pbr_pctile is not None for c in enriched.values())

    def test_pctiles_none_when_under_5_samples(self):
        """섹터 내 5종목 미만 → 백분위 미산출 (None 유지)."""
        enriched = {
            f"00000{i}": EnrichedCandidate(
                master=_master(f"00000{i}", sector="바이오/헬스케어"),
                financial_trend=FinancialTrend(per=float(10 + i), pbr=float(1 + i)),
            )
            for i in range(3)
        }
        _compute_sector_pctiles(enriched)
        assert all(c.sector_per_pctile is None for c in enriched.values())


# ─── run_deterministic_scout — db 없는 경로 ──────────────────────


def _scout_ctx(universe: list[str]) -> ScoutContext:
    return ScoutContext(
        as_of=date(2026, 5, 22),
        universe=universe,
        market_summary=MarketSummary(
            kospi_close=2600.0,
            kospi_change_pct=0.01,
            kosdaq_close=850.0,
            kosdaq_change_pct=0.0,
            up_count=400,
            down_count=400,
        ),
        macro_state=MacroStateForScout(gate="open", size_multiplier=1.0, gate_run_id="macro_test"),
        news_events={},
        sector_momentum={},
        strategy_tags_available=["SECTOR_MOMENTUM"],
    )


@pytest.mark.asyncio
async def test_run_deterministic_scout_no_engine_returns_empty():
    """db_engine=None → 빈 후보, 정상 ScreeningResult."""
    result = await run_deterministic_scout(
        db_engine=None,
        scout_ctx=_scout_ctx(["005930"]),
        as_of=date(2026, 5, 22),
        as_of_dt=datetime(2026, 5, 22, 8, 30),
        scout_run_id="sr_test",
    )
    assert result.result.ok is True
    assert result.result.candidates == []
    assert result.scout_out.expected_candidates == 0


@pytest.mark.asyncio
async def test_run_deterministic_scout_empty_universe_returns_empty():
    result = await run_deterministic_scout(
        db_engine=None,
        scout_ctx=_scout_ctx([]),
        as_of=date(2026, 5, 22),
        as_of_dt=datetime(2026, 5, 22, 8, 30),
        scout_run_id="sr_test",
    )
    assert result.result.candidates == []
