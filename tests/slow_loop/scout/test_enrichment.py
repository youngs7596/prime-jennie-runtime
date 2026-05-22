"""결정론 enrichment — 순수 헬퍼 테스트.

DB batch 로더는 fixture 없이 검증 불가 — sentiment 척도 변환 + snapshot 합성만.
"""

from __future__ import annotations

from datetime import date

from prime_jennie_runtime.slow_loop.scout.enrichment import (
    DailyPrice,
    _build_snapshot,
    _sentiment_to_0_100,
)


def _price(close: int, high: int, low: int) -> DailyPrice:
    return DailyPrice(
        stock_code="005930",
        price_date=date(2026, 5, 1),
        open_price=close,
        high_price=high,
        low_price=low,
        close_price=close,
        volume=1_000_000,
    )


class TestSentimentTo0100:
    def test_neutral_maps_to_50(self):
        assert _sentiment_to_0_100(0.0) == 50.0

    def test_max_positive_maps_to_100(self):
        assert _sentiment_to_0_100(1.0) == 100.0

    def test_max_negative_maps_to_0(self):
        assert _sentiment_to_0_100(-1.0) == 0.0

    def test_observed_positive_avg(self):
        """실측 평균 0.38 → ~69."""
        assert _sentiment_to_0_100(0.38) == 69.0

    def test_clamped_above_1(self):
        assert _sentiment_to_0_100(2.0) == 100.0

    def test_clamped_below_minus1(self):
        assert _sentiment_to_0_100(-3.0) == 0.0


class TestBuildSnapshot:
    def test_none_for_empty_prices(self):
        assert _build_snapshot("005930", []) is None

    def test_price_is_latest_close(self):
        prices = [_price(100, 110, 90), _price(120, 130, 115)]
        snap = _build_snapshot("005930", prices)
        assert snap is not None
        assert snap.price == 120

    def test_high_52w_is_window_max(self):
        prices = [_price(100, 150, 90), _price(120, 130, 115)]
        snap = _build_snapshot("005930", prices)
        assert snap is not None
        assert snap.high_52w == 150  # 윈도우 내 최고 high

    def test_change_pct_from_prev_close(self):
        prices = [_price(100, 110, 90), _price(110, 120, 105)]
        snap = _build_snapshot("005930", prices)
        assert snap is not None
        assert snap.change_pct == 10.0  # 110/100 - 1
