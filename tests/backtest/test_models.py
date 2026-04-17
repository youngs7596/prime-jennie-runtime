"""dataclass 기본 동작 + PriceCache 인덱싱."""

from __future__ import annotations

from datetime import date

from prime_jennie_runtime.backtest import (
    DailyOHLCV,
    MarketRegime,
    PriceCache,
    RiskParams,
    SimPosition,
    TradeTier,
)


def test_sim_position_watermark_defaults_to_buy_price():
    pos = SimPosition(
        stock_code="005930",
        stock_name="삼성전자",
        quantity=10,
        buy_price=70_000,
        buy_date=date(2026, 4, 1),
    )
    assert pos.high_watermark == 70_000
    assert pos.total_cost == 700_000


def test_sim_position_profit_pct():
    pos = SimPosition(
        stock_code="A", stock_name="A", quantity=1, buy_price=100, buy_date=date(2026, 4, 1)
    )
    assert pos.profit_pct(110) == 10.0
    assert pos.profit_pct(90) == -10.0


def test_sim_position_holding_days():
    pos = SimPosition(
        stock_code="A", stock_name="A", quantity=1, buy_price=100, buy_date=date(2026, 4, 1)
    )
    assert pos.holding_days(date(2026, 4, 5)) == 4


def test_sim_position_trade_tier_default():
    pos = SimPosition(
        stock_code="A", stock_name="A", quantity=1, buy_price=100, buy_date=date(2026, 4, 1)
    )
    assert pos.trade_tier == TradeTier.TIER1


def test_risk_params_cash_floor_by_regime():
    rp = RiskParams()
    assert rp.get_cash_floor(MarketRegime.STRONG_BULL) == 5
    assert rp.get_cash_floor(MarketRegime.STRONG_BEAR) == 50
    # 알 수 없는 regime 은 기본값 15
    assert rp.get_cash_floor(MarketRegime.SIDEWAYS) == 15


def test_price_cache_get_history_until():
    cache = PriceCache()
    for i in range(5):
        d = date(2026, 4, i + 1)
        ohlcv = DailyOHLCV(
            price_date=d,
            open_price=100 + i,
            high_price=110 + i,
            low_price=90 + i,
            close_price=105 + i,
            volume=1000,
        )
        cache.by_stock_date.setdefault("A", {})[d] = ohlcv
        cache.by_stock_sorted.setdefault("A", []).append(ohlcv)

    # 4/3 시점까지 3개 캡쳐 (4/1, 4/2, 4/3)
    hist = cache.get_history_until("A", date(2026, 4, 3), n=60)
    assert len(hist) == 3
    assert [p.price_date for p in hist] == [
        date(2026, 4, 1),
        date(2026, 4, 2),
        date(2026, 4, 3),
    ]

    # n 제한
    hist_last2 = cache.get_history_until("A", date(2026, 4, 5), n=2)
    assert [p.price_date for p in hist_last2] == [date(2026, 4, 4), date(2026, 4, 5)]


def test_price_cache_get_close_prices_until():
    cache = PriceCache()
    for i in range(3):
        d = date(2026, 4, i + 1)
        ohlcv = DailyOHLCV(
            price_date=d,
            open_price=100,
            high_price=100,
            low_price=100,
            close_price=100 + i,
            volume=0,
        )
        cache.by_stock_date.setdefault("A", {})[d] = ohlcv
        cache.by_stock_sorted.setdefault("A", []).append(ohlcv)

    closes = cache.get_close_prices_until("A", date(2026, 4, 3))
    assert closes == [100, 101, 102]


def test_price_cache_missing_returns_none():
    cache = PriceCache()
    assert cache.get("NONE", date(2026, 4, 1)) is None
    assert cache.get_history_until("NONE", date(2026, 4, 1)) == []
