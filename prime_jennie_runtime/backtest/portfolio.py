"""시뮬레이션 포트폴리오 — 현금/포지션 관리, 매수/매도 실행, 리스크 가드.

포팅 원본: prime-jennie/prime_jennie/services/backtest/portfolio.py
v2 `get_config().risk` / `.sell` 전역 의존은 `BacktestConfig.risk` / `.sell` 에 흡수.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

from .domain import MarketRegime, SectorGroup, SellReason, SignalType, TradeTier
from .models import BacktestConfig, DailySnapshot, PriceCache, SimPosition, TradeLog

logger = logging.getLogger(__name__)


class SimulatedPortfolio:
    """가상 포트폴리오: 현금, 보유 포지션, 거래 기록, 리스크 관리."""

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.cash: int = config.initial_capital
        self.positions: dict[str, SimPosition] = {}
        self.trade_logs: list[TradeLog] = []
        self.daily_snapshots: list[DailySnapshot] = []

        self._daily_buy_count: int = 0
        self._stoploss_cooldown: dict[str, date] = {}

    # --- 일일 리셋 ---

    def reset_daily(self) -> None:
        self._daily_buy_count = 0

    # --- 포트폴리오 상태 ---

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def total_portfolio_value(self, price_cache: PriceCache, d: date) -> int:
        """종가 기준 보유 주식 평가액."""
        value = 0
        for pos in self.positions.values():
            ohlcv = price_cache.get(pos.stock_code, d)
            if ohlcv:
                value += ohlcv.close_price * pos.quantity
            else:
                value += pos.buy_price * pos.quantity
        return value

    def total_value(self, price_cache: PriceCache, d: date) -> int:
        return self.cash + self.total_portfolio_value(price_cache, d)

    def held_sector_groups(self) -> list[SectorGroup]:
        return [p.sector_group for p in self.positions.values() if p.sector_group]

    def sector_count(self, sector: SectorGroup | None) -> int:
        if sector is None:
            return 0
        return sum(1 for p in self.positions.values() if p.sector_group == sector)

    # --- 리스크 가드 ---

    def can_buy(
        self,
        stock_code: str,
        sector: SectorGroup | None,
        current_date: date,
        regime: MarketRegime,
    ) -> tuple[bool, str]:
        """매수 가능 여부 + 거부 사유."""
        risk = self.config.risk

        if stock_code in self.positions:
            return False, "already_held"
        if self._daily_buy_count >= risk.max_buy_count_per_day:
            return False, "daily_buy_limit"
        if self.position_count >= risk.max_portfolio_size:
            return False, "portfolio_full"
        if sector and self.sector_count(sector) >= risk.max_sector_stocks:
            return False, "sector_limit"

        cooldown_until = self._stoploss_cooldown.get(stock_code)
        if cooldown_until and current_date <= cooldown_until:
            return False, "stoploss_cooldown"

        cash_floor_pct = risk.get_cash_floor(regime) / 100
        min_cash = int(self.cash * cash_floor_pct)
        if self.cash <= min_cash:
            return False, "cash_floor"

        return True, ""

    # --- 매수 실행 ---

    def execute_buy(
        self,
        stock_code: str,
        stock_name: str,
        quantity: int,
        price: int,
        trade_date: date,
        signal_type: SignalType | None = None,
        trade_tier: TradeTier = TradeTier.TIER1,
        llm_score: float = 0.0,
        hybrid_score: float = 0.0,
        sector_group: SectorGroup | None = None,
        regime: MarketRegime = MarketRegime.SIDEWAYS,
    ) -> TradeLog | None:
        """매수 실행. 슬리피지+수수료 반영."""
        if quantity <= 0 or price <= 0:
            return None

        entry_price = int(math.ceil(price * (1 + self.config.slippage_pct / 100)))
        raw_amount = entry_price * quantity
        fee = int(math.ceil(raw_amount * self.config.buy_fee_pct / 100))
        total_cost = raw_amount + fee

        if total_cost > self.cash:
            max_affordable = int(self.cash / (entry_price * (1 + self.config.buy_fee_pct / 100)))
            if max_affordable <= 0:
                return None
            quantity = max_affordable
            raw_amount = entry_price * quantity
            fee = int(math.ceil(raw_amount * self.config.buy_fee_pct / 100))
            total_cost = raw_amount + fee

        self.cash -= total_cost
        self._daily_buy_count += 1

        pos = SimPosition(
            stock_code=stock_code,
            stock_name=stock_name,
            quantity=quantity,
            buy_price=entry_price,
            buy_date=trade_date,
            sector_group=sector_group,
            signal_type=signal_type,
            trade_tier=trade_tier,
            llm_score=llm_score,
            hybrid_score=hybrid_score,
            high_watermark=entry_price,
        )
        self.positions[stock_code] = pos

        log = TradeLog(
            trade_date=trade_date,
            stock_code=stock_code,
            stock_name=stock_name,
            trade_type="BUY",
            quantity=quantity,
            price=entry_price,
            total_amount=total_cost,
            fee=fee,
            signal_type=signal_type,
            trade_tier=trade_tier,
            llm_score=llm_score,
            hybrid_score=hybrid_score,
            regime=regime,
        )
        self.trade_logs.append(log)
        return log

    # --- 매도 실행 ---

    def execute_sell(
        self,
        stock_code: str,
        sell_price: int,
        sell_quantity: int,
        trade_date: date,
        sell_reason: SellReason,
        regime: MarketRegime = MarketRegime.SIDEWAYS,
    ) -> TradeLog | None:
        """매도 실행. 수수료+세금 반영. 부분 매도 지원."""
        pos = self.positions.get(stock_code)
        if not pos:
            return None

        sell_quantity = min(sell_quantity, pos.quantity)
        if sell_quantity <= 0:
            return None

        raw_amount = sell_price * sell_quantity
        fee = int(math.ceil(raw_amount * self.config.sell_fee_pct / 100))
        net_proceeds = raw_amount - fee

        self.cash += net_proceeds

        profit_pct = pos.profit_pct(sell_price)
        profit_amount = (sell_price - pos.buy_price) * sell_quantity - fee
        holding_days = pos.holding_days(trade_date)

        log = TradeLog(
            trade_date=trade_date,
            stock_code=stock_code,
            stock_name=pos.stock_name,
            trade_type="SELL",
            quantity=sell_quantity,
            price=sell_price,
            total_amount=net_proceeds,
            fee=fee,
            signal_type=pos.signal_type,
            trade_tier=pos.trade_tier,
            llm_score=pos.llm_score,
            hybrid_score=pos.hybrid_score,
            sell_reason=sell_reason,
            profit_pct=profit_pct,
            profit_amount=profit_amount,
            holding_days=holding_days,
            regime=regime,
        )
        self.trade_logs.append(log)

        remaining = pos.quantity - sell_quantity
        if remaining <= 0:
            del self.positions[stock_code]
        else:
            pos.quantity = remaining
            if sell_reason == SellReason.PROFIT_TARGET:
                pos.scale_out_level += 1
            if sell_reason == SellReason.RSI_OVERBOUGHT:
                pos.rsi_sold = True

        if sell_reason == SellReason.STOP_LOSS:
            cooldown_days = self.config.risk.stoploss_cooldown_days
            self._stoploss_cooldown[stock_code] = trade_date + timedelta(days=cooldown_days)

        return log

    # --- 워터마크 업데이트 ---

    def update_watermarks(self, price_cache: PriceCache, d: date) -> None:
        """당일 고가로 high_watermark 업데이트 + profit floor 체크."""
        sell = self.config.sell
        for pos in self.positions.values():
            ohlcv = price_cache.get(pos.stock_code, d)
            if not ohlcv:
                continue
            if ohlcv.high_price > pos.high_watermark:
                pos.high_watermark = ohlcv.high_price

            if not pos.profit_floor_active:
                hp = pos.high_profit_pct()
                if hp >= sell.profit_floor_activation:
                    pos.profit_floor_active = True
                    pos.profit_floor_level = sell.profit_floor_level

    # --- 스냅샷 ---

    def take_snapshot(
        self,
        price_cache: PriceCache,
        d: date,
        regime: MarketRegime = MarketRegime.SIDEWAYS,
    ) -> DailySnapshot:
        pv = self.total_portfolio_value(price_cache, d)
        tv = self.cash + pv

        prev_tv = (
            self.daily_snapshots[-1].total_value
            if self.daily_snapshots
            else self.config.initial_capital
        )
        daily_ret = (tv - prev_tv) / prev_tv * 100.0 if prev_tv > 0 else 0.0

        snap = DailySnapshot(
            snapshot_date=d,
            cash=self.cash,
            portfolio_value=pv,
            total_value=tv,
            position_count=self.position_count,
            daily_return_pct=daily_ret,
            regime=regime,
        )
        self.daily_snapshots.append(snap)
        return snap

    # --- 청산 ---

    def liquidate_all(
        self,
        price_cache: PriceCache,
        d: date,
        regime: MarketRegime = MarketRegime.SIDEWAYS,
    ) -> None:
        """백테스트 종료 시 전체 포지션 청산."""
        codes = list(self.positions.keys())
        for code in codes:
            ohlcv = price_cache.get(code, d)
            if not ohlcv:
                continue
            self.execute_sell(
                stock_code=code,
                sell_price=ohlcv.close_price,
                sell_quantity=self.positions[code].quantity,
                trade_date=d,
                sell_reason=SellReason.MANUAL,
                regime=regime,
            )


__all__ = ["SimulatedPortfolio"]
