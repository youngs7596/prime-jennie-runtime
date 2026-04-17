"""SimulatedPortfolio — 매수/매도/리스크 가드/스냅샷."""

from __future__ import annotations

from datetime import date

from prime_jennie_runtime.backtest import (
    BacktestConfig,
    DailyOHLCV,
    MarketRegime,
    PriceCache,
    RiskParams,
    SellReason,
    SignalType,
    SimulatedPortfolio,
)


def _make_config(**overrides) -> BacktestConfig:
    cfg = BacktestConfig(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        initial_capital=10_000_000,
        buy_fee_pct=0.015,
        sell_fee_pct=0.195,
        slippage_pct=0.0,  # 테스트 단순화 — 슬리피지 제거
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _mk_price_cache(code: str, d: date, close: int) -> PriceCache:
    cache = PriceCache()
    cache.by_stock_date.setdefault(code, {})[d] = DailyOHLCV(
        price_date=d,
        open_price=close,
        high_price=close,
        low_price=close,
        close_price=close,
        volume=1000,
    )
    cache.by_stock_sorted.setdefault(code, []).append(cache.by_stock_date[code][d])
    return cache


def test_execute_buy_deducts_cash_and_records_position():
    pf = SimulatedPortfolio(_make_config())
    log = pf.execute_buy(
        stock_code="A",
        stock_name="A",
        quantity=10,
        price=1000,
        trade_date=date(2026, 4, 1),
        signal_type=SignalType.MOMENTUM,
    )
    assert log is not None
    assert log.trade_type == "BUY"
    assert log.quantity == 10
    # 매수 비용 = 10 * 1000 + 0.015% fee = 10_000 + 2 (ceil) = 10_002
    assert log.total_amount == 10_002
    assert pf.cash == 10_000_000 - 10_002
    assert "A" in pf.positions
    assert pf.position_count == 1


def test_execute_buy_shrinks_to_affordable_when_cash_low():
    cfg = _make_config(initial_capital=5_000)
    pf = SimulatedPortfolio(cfg)
    # 요청: 10주 @ 1000원 = 10,000 (현금 5,000 초과) → 4주로 축소
    log = pf.execute_buy(
        stock_code="A",
        stock_name="A",
        quantity=10,
        price=1000,
        trade_date=date(2026, 4, 1),
    )
    assert log is not None
    assert log.quantity == 4


def test_execute_buy_rejects_if_cash_insufficient_for_even_one_share():
    cfg = _make_config(initial_capital=10)
    pf = SimulatedPortfolio(cfg)
    log = pf.execute_buy(
        stock_code="A",
        stock_name="A",
        quantity=1,
        price=1000,
        trade_date=date(2026, 4, 1),
    )
    assert log is None


def test_execute_sell_full_position_removes_it():
    pf = SimulatedPortfolio(_make_config())
    pf.execute_buy(
        stock_code="A",
        stock_name="A",
        quantity=10,
        price=1000,
        trade_date=date(2026, 4, 1),
    )
    buy_cash_after = pf.cash

    log = pf.execute_sell(
        stock_code="A",
        sell_price=1100,
        sell_quantity=10,
        trade_date=date(2026, 4, 5),
        sell_reason=SellReason.PROFIT_TARGET,
    )
    assert log is not None
    assert log.trade_type == "SELL"
    assert "A" not in pf.positions
    # 매도 수수료 = 11_000 * 0.195% = 22 (ceil)
    assert log.fee == 22
    assert log.total_amount == 11_000 - 22
    assert pf.cash == buy_cash_after + (11_000 - 22)
    assert log.profit_pct == 10.0
    assert log.holding_days == 4


def test_execute_sell_partial_keeps_remaining_qty():
    pf = SimulatedPortfolio(_make_config())
    pf.execute_buy(
        stock_code="A",
        stock_name="A",
        quantity=10,
        price=1000,
        trade_date=date(2026, 4, 1),
    )
    pf.execute_sell(
        stock_code="A",
        sell_price=1100,
        sell_quantity=3,
        trade_date=date(2026, 4, 5),
        sell_reason=SellReason.PROFIT_TARGET,
    )
    assert pf.positions["A"].quantity == 7
    assert pf.positions["A"].scale_out_level == 1


def test_execute_sell_stop_loss_sets_cooldown():
    pf = SimulatedPortfolio(_make_config())
    pf.execute_buy(
        stock_code="A",
        stock_name="A",
        quantity=10,
        price=1000,
        trade_date=date(2026, 4, 1),
    )
    pf.execute_sell(
        stock_code="A",
        sell_price=900,
        sell_quantity=10,
        trade_date=date(2026, 4, 5),
        sell_reason=SellReason.STOP_LOSS,
    )
    can, reason = pf.can_buy(
        stock_code="A",
        sector=None,
        current_date=date(2026, 4, 7),
        regime=MarketRegime.SIDEWAYS,
    )
    assert can is False
    assert reason == "stoploss_cooldown"


def test_can_buy_rejects_already_held():
    pf = SimulatedPortfolio(_make_config())
    pf.execute_buy(
        stock_code="A",
        stock_name="A",
        quantity=10,
        price=1000,
        trade_date=date(2026, 4, 1),
    )
    can, reason = pf.can_buy("A", None, date(2026, 4, 2), MarketRegime.SIDEWAYS)
    assert not can
    assert reason == "already_held"


def test_can_buy_daily_limit():
    cfg = _make_config()
    cfg.risk = RiskParams(max_buy_count_per_day=1)
    pf = SimulatedPortfolio(cfg)
    pf.execute_buy("A", "A", 1, 100, date(2026, 4, 1))
    can, reason = pf.can_buy("B", None, date(2026, 4, 1), MarketRegime.SIDEWAYS)
    assert not can
    assert reason == "daily_buy_limit"
    pf.reset_daily()
    can2, _ = pf.can_buy("B", None, date(2026, 4, 2), MarketRegime.SIDEWAYS)
    assert can2


def test_update_watermarks_and_profit_floor_activation():
    pf = SimulatedPortfolio(_make_config())
    pf.execute_buy("A", "A", 10, 1000, date(2026, 4, 1))

    # 당일 고가 1100 (+10%) → profit_floor_activation 기본 5% 이상 → 가동
    cache = PriceCache()
    d = date(2026, 4, 2)
    cache.by_stock_date.setdefault("A", {})[d] = DailyOHLCV(
        price_date=d,
        open_price=1000,
        high_price=1100,
        low_price=990,
        close_price=1080,
        volume=500,
    )
    pf.update_watermarks(cache, d)
    assert pf.positions["A"].high_watermark == 1100
    assert pf.positions["A"].profit_floor_active is True
    assert pf.positions["A"].profit_floor_level == 2.0


def test_take_snapshot_computes_daily_return():
    pf = SimulatedPortfolio(_make_config())
    pf.execute_buy("A", "A", 10, 1000, date(2026, 4, 1))
    cache = _mk_price_cache("A", date(2026, 4, 1), close=1000)

    snap1 = pf.take_snapshot(cache, date(2026, 4, 1))
    assert snap1.position_count == 1
    # 첫 스냅샷의 daily_return_pct 는 initial_capital 기준 — 매수 직후라 수수료만큼만 음수
    assert snap1.cash + snap1.portfolio_value == snap1.total_value

    # 다음날 종가 1100 → 총자산 증가
    cache2 = _mk_price_cache("A", date(2026, 4, 2), close=1100)
    snap2 = pf.take_snapshot(cache2, date(2026, 4, 2))
    assert snap2.total_value > snap1.total_value
    assert snap2.daily_return_pct > 0


def test_liquidate_all_closes_all_positions():
    pf = SimulatedPortfolio(_make_config())
    pf.execute_buy("A", "A", 10, 1000, date(2026, 4, 1))
    pf.execute_buy("B", "B", 5, 2000, date(2026, 4, 1))
    assert pf.position_count == 2

    cache = PriceCache()
    d = date(2026, 4, 30)
    for code, close in [("A", 1050), ("B", 1900)]:
        cache.by_stock_date.setdefault(code, {})[d] = DailyOHLCV(
            price_date=d,
            open_price=close,
            high_price=close,
            low_price=close,
            close_price=close,
            volume=0,
        )
    pf.liquidate_all(cache, d)
    assert pf.position_count == 0
    sell_logs = [t for t in pf.trade_logs if t.trade_type == "SELL"]
    assert len(sell_logs) == 2
    assert all(t.sell_reason == SellReason.MANUAL for t in sell_logs)
