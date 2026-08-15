"""결정론 Quant Scorer 단위 테스트.

v2 `tests/unit/services/test_scout_quant.py` 의 포팅 — 팩터 수식이 동일 포팅이므로
테스트도 동일하게 옮긴다. v3 적응 2가지:
  - `market_regime: MarketRegime` → `is_bull: bool` (deterministic_scout 가 환산).
  - `StockMaster.sector_group` 가 str (v2 SectorGroup enum).
"""

from __future__ import annotations

from datetime import date

from prime_jennie_runtime.slow_loop.scout.enrichment import (
    DailyPrice,
    EnrichedCandidate,
    FinancialTrend,
    InvestorTradingSummary,
    StockMaster,
    StockSnapshot,
)
from prime_jennie_runtime.slow_loop.scout.quant import (
    V2_NEUTRAL,
    QuantScore,
    _compute_rsi,
    _linear_map,
    _momentum_score,
    _news_score,
    _pctile_to_score_5,
    _pctile_to_score_10,
    _quality_score,
    _sector_momentum_score,
    _supply_demand_score,
    _technical_score,
    _value_score,
    score_candidate,
    sector_momentum_baseline,
)
from prime_jennie_runtime.slow_loop.scout.schemas import NewsEventEntry

# ─── Fixtures ────────────────────────────────────────────────────


def _make_master(code: str = "005930", name: str = "삼성전자") -> StockMaster:
    return StockMaster(
        stock_code=code,
        stock_name=name,
        market="KOSPI",
        sector_group="반도체/IT",
    )


def _make_prices(n: int = 150, base: int = 70000, trend: float = 0.001) -> list[DailyPrice]:
    """n일치 일봉 생성 (상승 추세)."""
    prices = []
    for i in range(n):
        price = int(base * (1 + trend * i))
        prices.append(
            DailyPrice(
                stock_code="005930",
                price_date=date(2026, 2, 19),
                open_price=price - 200,
                high_price=price + 300,
                low_price=price - 400,
                close_price=price,
                volume=10000000 + i * 10000,
            )
        )
    return prices


def _make_candidate(
    prices: list[DailyPrice] | None = None,
    snapshot: StockSnapshot | None = None,
    ft: FinancialTrend | None = None,
    it: InvestorTradingSummary | None = None,
    news_event: NewsEventEntry | None = None,
) -> EnrichedCandidate:
    return EnrichedCandidate(
        master=_make_master(),
        snapshot=snapshot,
        daily_prices=prices or [],
        financial_trend=ft,
        investor_trading=it,
        news_event=news_event,
    )


def _news_entry(
    high_pos: int = 0,
    high_risk: int = 0,
    med_pos: int = 0,
    med_risk: int = 0,
    *,
    article_count: int | None = None,
    staleness_hours: float = 2.0,
) -> NewsEventEntry:
    """impact level 별 positive/risk event 수로 NewsEventEntry 합성 — 테스트 fixture."""
    events_by_impact: dict[str, dict[str, int]] = {}
    if high_pos:
        events_by_impact.setdefault("high", {})["earnings"] = high_pos
    if high_risk:
        events_by_impact.setdefault("high", {})["lawsuit"] = high_risk
    if med_pos:
        events_by_impact.setdefault("medium", {})["contract"] = med_pos
    if med_risk:
        events_by_impact.setdefault("medium", {})["regulation"] = med_risk
    total = high_pos + high_risk + med_pos + med_risk
    return NewsEventEntry(
        article_count=article_count if article_count is not None else max(total, 1),
        latest_at=None,
        staleness_hours=staleness_hours,
        events_by_impact=events_by_impact,
    )


# ─── Total Score ─────────────────────────────────────────────────


class TestScoreCandidate:
    def test_valid_score_has_subscores(self):
        candidate = _make_candidate(
            prices=_make_prices(150),
            ft=FinancialTrend(per=12.0, pbr=1.5, roe=12.0),
            it=InvestorTradingSummary(foreign_net_buy_sum=1e9, institution_net_buy_sum=5e8),
            news_event=_news_entry(high_pos=1),
        )
        result = score_candidate(candidate)

        assert isinstance(result, QuantScore)
        assert 0 <= result.total_score <= 100
        assert result.momentum_score >= 0
        assert result.quality_score >= 0
        assert result.value_score >= 0
        assert result.is_valid is True

    def test_total_equals_sum_of_subscores(self):
        candidate = _make_candidate(
            prices=_make_prices(150),
            ft=FinancialTrend(per=10.0, pbr=0.8, roe=15.0),
        )
        result = score_candidate(candidate)

        expected = (
            result.momentum_score
            + result.quality_score
            + result.value_score
            + result.technical_score
            + result.news_score
            + result.supply_demand_score
            + result.sector_momentum_score
        )
        assert abs(result.total_score - max(0.0, min(100.0, expected))) <= 1.5

    def test_insufficient_data_returns_neutral(self):
        candidate = _make_candidate(prices=_make_prices(5))
        result = score_candidate(candidate)

        assert result.is_valid is False
        assert result.total_score == sum(V2_NEUTRAL.values())

    def test_score_bounded_0_100(self):
        candidate = _make_candidate(
            prices=_make_prices(150, trend=0.005),
            ft=FinancialTrend(per=5.0, pbr=0.5, roe=20.0),
            it=InvestorTradingSummary(
                foreign_net_buy_sum=10e9,
                institution_net_buy_sum=10e9,
                foreign_ratio_trend=2.0,
            ),
            news_event=_news_entry(high_pos=3),
            snapshot=StockSnapshot(stock_code="005930", price=80000, high_52w=90000, low_52w=50000),
        )
        result = score_candidate(candidate)
        assert 0 <= result.total_score <= 100

    def test_bull_boosts_momentum(self):
        """is_bull=True 면 RSI 높은 종목이 is_bull=False 보다 같거나 높은 점수."""
        candidate = _make_candidate(
            prices=_make_prices(150, trend=0.008),
            ft=FinancialTrend(per=12.0, pbr=1.0, roe=15.0),
        )
        bull = score_candidate(candidate, is_bull=True)
        normal = score_candidate(candidate, is_bull=False)
        assert bull.total_score >= normal.total_score


# ─── Sub-factors ─────────────────────────────────────────────────


class TestMomentumScore:
    def test_returns_neutral_for_short_data(self):
        result = _momentum_score(_make_prices(10), None)
        assert result == V2_NEUTRAL["momentum"]

    def test_uptrend_gets_higher_score(self):
        result = _momentum_score(_make_prices(150, trend=0.003), None)
        assert result > 5.0

    def test_rsi_70_80_bull_no_penalty(self):
        prices = _make_prices(150, trend=0.008)
        bull_score = _momentum_score(prices, None, is_bull=True)
        normal_score = _momentum_score(prices, None, is_bull=False)
        assert bull_score >= normal_score


class TestQualityScore:
    def test_high_roe_high_score(self):
        candidate = _make_candidate(ft=FinancialTrend(roe=20.0, pbr=1.0, per=10.0))
        assert _quality_score(candidate) >= 15.0

    def test_negative_roe_low_score(self):
        candidate = _make_candidate(ft=FinancialTrend(roe=-5.0, pbr=3.0, per=50.0))
        assert _quality_score(candidate) < 5.0

    def test_no_data_returns_neutral(self):
        assert _quality_score(_make_candidate()) == V2_NEUTRAL["quality"]

    def test_low_pctile_gets_high_pbr_score(self):
        candidate = _make_candidate(ft=FinancialTrend(roe=10.0, pbr=8.0, per=40.0))
        candidate.sector_pbr_pctile = 15.0
        candidate.sector_per_pctile = 30.0
        assert _quality_score(candidate) >= 16.0


class TestValueScore:
    def test_low_per_high_score(self):
        candidate = _make_candidate(ft=FinancialTrend(per=6.0, pbr=0.5))
        assert _value_score(candidate) >= 15.0

    def test_high_per_low_score(self):
        candidate = _make_candidate(ft=FinancialTrend(per=100.0, pbr=5.0))
        assert _value_score(candidate) < 6.0

    def test_low_per_pctile_max_discount(self):
        candidate = _make_candidate(ft=FinancialTrend(per=50.0, pbr=8.0))
        candidate.sector_per_pctile = 8.0
        candidate.sector_pbr_pctile = 15.0
        assert _value_score(candidate) >= 14.0


class TestTechnicalScore:
    def test_returns_neutral_for_short_data(self):
        assert _technical_score(_make_prices(5)) == V2_NEUTRAL["technical"]

    def test_bullish_alignment_higher_score(self):
        assert _technical_score(_make_prices(30, trend=0.005)) > 5.0


class TestNewsScore:
    def test_no_data_returns_neutral(self):
        assert _news_score(_make_candidate()) == V2_NEUTRAL["news"]

    def test_zero_article_count_returns_neutral(self):
        entry = NewsEventEntry(article_count=0, staleness_hours=48.0)
        assert _news_score(_make_candidate(news_event=entry)) == V2_NEUTRAL["news"]

    def test_high_impact_positive_event_lifts_score(self):
        # high_pos=1 → +1.5 → 6.5
        candidate = _make_candidate(news_event=_news_entry(high_pos=1))
        assert _news_score(candidate) == 6.5

    def test_high_impact_positive_caps_at_three(self):
        # high_pos=3 → cap 3 → 8.0
        candidate = _make_candidate(news_event=_news_entry(high_pos=3))
        assert _news_score(candidate) == 8.0

    def test_high_impact_risk_drops_score(self):
        # high_risk=1 → -2 → 3.0
        candidate = _make_candidate(news_event=_news_entry(high_risk=1))
        assert _news_score(candidate) == 3.0

    def test_high_impact_risk_caps_at_four(self):
        # high_risk=3 → cap 4 → 1.0
        candidate = _make_candidate(news_event=_news_entry(high_risk=3))
        assert _news_score(candidate) == 1.0

    def test_medium_positive_modest_lift(self):
        # med_pos=1 → +0.5 → 5.5
        candidate = _make_candidate(news_event=_news_entry(med_pos=1))
        assert _news_score(candidate) == 5.5

    def test_positive_and_risk_offset(self):
        # high_pos=2 (+3) high_risk=2 (-4) → 4.0
        candidate = _make_candidate(news_event=_news_entry(high_pos=2, high_risk=2))
        assert _news_score(candidate) == 4.0

    def test_staleness_over_24h_decays(self):
        # high_pos=1 (+1.5) staleness=30h (-0.5) → 6.0
        candidate = _make_candidate(news_event=_news_entry(high_pos=1, staleness_hours=30.0))
        assert _news_score(candidate) == 6.0

    def test_score_clamped_to_zero(self):
        # high_risk=5 (-4 cap) + med_risk=5 (-1 cap) + stale 30h (-0.5) → 5 - 5.5 = -0.5 → 0
        candidate = _make_candidate(
            news_event=_news_entry(high_risk=5, med_risk=5, staleness_hours=30.0)
        )
        assert _news_score(candidate) == 0.0


class TestSectorMomentumScore:
    def test_hot_sector_high_score(self):
        candidate = _make_candidate()
        candidate.sector_avg_return_20d = 15.0
        assert _sector_momentum_score(candidate) >= 9.5

    def test_cool_sector_low_score(self):
        candidate = _make_candidate()
        candidate.sector_avg_return_20d = -5.0
        assert _sector_momentum_score(candidate) <= 0.5

    def test_none_returns_neutral(self):
        candidate = _make_candidate()
        candidate.sector_avg_return_20d = None
        assert _sector_momentum_score(candidate) == V2_NEUTRAL["sector_momentum"]


def _sector_universe(returns: list[float]) -> list[EnrichedCandidate]:
    """섹터 20일 수익률만 다른 후보 묶음 — 중심화 기준선 계산용."""
    out = []
    for r in returns:
        c = _make_candidate()
        c.sector_avg_return_20d = r
        out.append(c)
    return out


class TestSectorMomentumCentering:
    def test_baseline_none_when_sample_too_small(self):
        assert sector_momentum_baseline(_sector_universe([5.0] * 19)) is None

    def test_baseline_skips_missing_data(self):
        universe = _sector_universe([10.0] * 25)
        for c in universe[:5]:
            c.sector_avg_return_20d = None
        # 남은 20종목만 평균 — 결측이 0 으로 끌어내리지 않는다.
        assert sector_momentum_baseline(universe) == _sector_momentum_score(universe[-1])

    def test_market_wide_rally_does_not_lift_scores(self):
        """시장이 통째로 오르면 중심화 후 점수는 그대로여야 한다."""
        calm = _sector_universe([0.0] * 25)
        rally = _sector_universe([12.0] * 25)
        calm_score = _sector_momentum_score(calm[0], sector_momentum_baseline(calm))
        rally_score = _sector_momentum_score(rally[0], sector_momentum_baseline(rally))
        assert calm_score == rally_score == V2_NEUTRAL["sector_momentum"]

    def test_relative_sector_gap_survives(self):
        """섹터 간 우열은 남는다 — 강한 섹터가 약한 섹터보다 높다."""
        universe = _sector_universe([0.0] * 20 + [12.0] * 5)
        baseline = sector_momentum_baseline(universe)
        weak = _sector_momentum_score(universe[0], baseline)
        strong = _sector_momentum_score(universe[-1], baseline)
        assert strong > V2_NEUTRAL["sector_momentum"] > weak

    def test_centered_score_stays_in_range(self):
        universe = _sector_universe([-5.0] * 24 + [15.0])
        baseline = sector_momentum_baseline(universe)
        scores = [_sector_momentum_score(c, baseline) for c in universe]
        assert all(0.0 <= s <= 10.0 for s in scores)

    def test_baseline_none_keeps_absolute_score(self):
        candidate = _make_candidate()
        candidate.sector_avg_return_20d = 15.0
        assert _sector_momentum_score(candidate, None) == _sector_momentum_score(candidate)

    def test_score_candidate_passes_baseline_through(self):
        candidate = _make_candidate(prices=_make_prices(150))
        candidate.sector_avg_return_20d = 15.0
        absolute = score_candidate(candidate)
        centered = score_candidate(candidate, sector_baseline=9.0)
        assert absolute.sector_momentum_score > centered.sector_momentum_score
        assert absolute.total_score > centered.total_score


class TestSupplyDemandScore:
    def test_strong_buying_high_score(self):
        candidate = _make_candidate(
            it=InvestorTradingSummary(
                foreign_net_buy_sum=5e9,
                institution_net_buy_sum=3e9,
                foreign_ratio_trend=1.5,
            )
        )
        assert _supply_demand_score(candidate) >= 7.5

    def test_strong_selling_low_score(self):
        candidate = _make_candidate(
            it=InvestorTradingSummary(
                foreign_net_buy_sum=-5e9,
                institution_net_buy_sum=-3e9,
                foreign_ratio_trend=-1.0,
            )
        )
        assert _supply_demand_score(candidate) < 4.0

    def test_no_data_returns_neutral(self):
        assert _supply_demand_score(_make_candidate()) == V2_NEUTRAL["supply_demand"]


# ─── Helpers ─────────────────────────────────────────────────────


class TestComputeRSI:
    def test_uptrend_rsi_above_50(self):
        rsi = _compute_rsi([100 + i for i in range(30)])
        assert rsi is not None and rsi > 50

    def test_downtrend_rsi_below_50(self):
        rsi = _compute_rsi([200 - i for i in range(30)])
        assert rsi is not None and rsi < 50

    def test_insufficient_data_returns_none(self):
        assert _compute_rsi([100, 101, 102]) is None


class TestPctileToScore:
    def test_lowest_pctile_max_score_5(self):
        assert _pctile_to_score_5(10.0) == 5.0

    def test_highest_pctile_floor_score_5(self):
        assert _pctile_to_score_5(90.0) == 1.0
        assert _pctile_to_score_5(90.0, floor=0.5) == 0.5

    def test_lowest_pctile_max_score_10(self):
        assert _pctile_to_score_10(5.0) == 10.0

    def test_highest_pctile_floor_score_10(self):
        assert _pctile_to_score_10(90.0) == 1.5


class TestLinearMap:
    def test_midpoint(self):
        assert _linear_map(50, 0, 100, 0, 10) == 5.0

    def test_clamped_below(self):
        assert _linear_map(-10, 0, 100, 0, 10) == 0.0

    def test_clamped_above(self):
        assert _linear_map(200, 0, 100, 0, 10) == 10.0
