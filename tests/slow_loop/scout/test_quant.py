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
)

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
    news_avg: float | None = None,
) -> EnrichedCandidate:
    return EnrichedCandidate(
        master=_make_master(),
        snapshot=snapshot,
        daily_prices=prices or [],
        financial_trend=ft,
        investor_trading=it,
        news_sentiment_avg=news_avg,
    )


# ─── Total Score ─────────────────────────────────────────────────


class TestScoreCandidate:
    def test_valid_score_has_subscores(self):
        candidate = _make_candidate(
            prices=_make_prices(150),
            ft=FinancialTrend(per=12.0, pbr=1.5, roe=12.0),
            it=InvestorTradingSummary(foreign_net_buy_sum=1e9, institution_net_buy_sum=5e8),
            news_avg=60.0,
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
            news_avg=95.0,
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
    def test_positive_sentiment_high_score(self):
        assert _news_score(_make_candidate(news_avg=80.0)) >= 8.0

    def test_negative_sentiment_low_score(self):
        assert _news_score(_make_candidate(news_avg=20.0)) <= 2.0

    def test_no_data_returns_neutral(self):
        assert _news_score(_make_candidate()) == V2_NEUTRAL["news"]


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
