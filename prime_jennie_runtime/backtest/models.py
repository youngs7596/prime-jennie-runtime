"""백테스트 데이터 모델 — 설정, 시뮬레이션 포지션, 트레이드 로그, 스냅샷.

포팅 원본: prime-jennie/prime_jennie/services/backtest/models.py
v2 와 1:1. v2 `get_config().risk` / `.sell` 전역 의존은 `BacktestConfig` 에 편입.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .domain import MarketRegime, SectorGroup, SellReason, SignalType, TradeTier

# =====================================================================
# BacktestConfig — 실행 파라미터 전부 (v2 Risk/Sell config 흡수)
# =====================================================================


@dataclass
class RiskParams:
    """v2 `get_config().risk` 동치."""

    max_buy_count_per_day: int = 5
    max_portfolio_size: int = 10
    max_sector_stocks: int = 3
    stoploss_cooldown_days: int = 5
    cash_floor_by_regime: dict[MarketRegime, int] = field(
        default_factory=lambda: {
            MarketRegime.STRONG_BULL: 5,
            MarketRegime.BULL: 10,
            MarketRegime.SIDEWAYS: 15,
            MarketRegime.BEAR: 30,
            MarketRegime.STRONG_BEAR: 50,
        }
    )

    def get_cash_floor(self, regime: MarketRegime) -> int:
        """국면별 최소 현금 비율 (%)."""
        return self.cash_floor_by_regime.get(regime, 15)


@dataclass
class SellParams:
    """v2 `get_config().sell` 동치 (부분집합)."""

    profit_floor_activation: float = 5.0  # 최고 수익률이 N% 이상일 때 profit floor 가동
    profit_floor_level: float = 2.0  # profit floor 발동 시 최소 보전 수익률


@dataclass
class BacktestConfig:
    """백테스트 실행 설정."""

    start_date: date
    end_date: date
    initial_capital: int = 50_000_000  # 5천만 원
    buy_fee_pct: float = 0.015  # 매수 수수료 0.015%
    sell_fee_pct: float = 0.195  # 매도 수수료+세금 0.195%
    slippage_pct: float = 0.1  # 슬리피지 0.1%
    export_csv_dir: str | None = None
    overextension_filter: bool = False  # 과열 필터 ON/OFF
    overextension_thresholds: dict | None = None  # {MarketRegime: float} 커스텀 임계값
    risk: RiskParams = field(default_factory=RiskParams)
    sell: SellParams = field(default_factory=SellParams)


# =====================================================================
# SimPosition — 보유 포지션
# =====================================================================


@dataclass
class SimPosition:
    """시뮬레이션 보유 포지션."""

    stock_code: str
    stock_name: str
    quantity: int
    buy_price: int  # 평균 매수가 (수수료+슬리피지 반영 전 원가)
    buy_date: date
    sector_group: SectorGroup | None = None
    signal_type: SignalType | None = None
    trade_tier: TradeTier = TradeTier.TIER1
    llm_score: float = 0.0
    hybrid_score: float = 0.0
    # 동적 상태
    high_watermark: int = 0  # 보유 중 최고가
    scale_out_level: int = 0
    rsi_sold: bool = False
    profit_floor_active: bool = False
    profit_floor_level: float = 0.0

    def __post_init__(self) -> None:
        if self.high_watermark == 0:
            self.high_watermark = self.buy_price

    @property
    def total_cost(self) -> int:
        return self.buy_price * self.quantity

    def holding_days(self, current_date: date) -> int:
        return (current_date - self.buy_date).days

    def profit_pct(self, current_price: int) -> float:
        if self.buy_price <= 0:
            return 0.0
        return (current_price - self.buy_price) / self.buy_price * 100.0

    def high_profit_pct(self) -> float:
        if self.buy_price <= 0:
            return 0.0
        return (self.high_watermark - self.buy_price) / self.buy_price * 100.0


# =====================================================================
# TradeLog / DailySnapshot
# =====================================================================


@dataclass
class TradeLog:
    """백테스트 거래 기록."""

    trade_date: date
    stock_code: str
    stock_name: str
    trade_type: str  # "BUY" | "SELL"
    quantity: int
    price: int
    total_amount: int  # 수수료 포함 실거래 금액
    fee: int = 0
    signal_type: SignalType | None = None
    trade_tier: TradeTier | None = None
    llm_score: float | None = None
    hybrid_score: float | None = None
    sell_reason: SellReason | None = None
    profit_pct: float | None = None
    profit_amount: int | None = None
    holding_days: int | None = None
    regime: MarketRegime | None = None


@dataclass
class DailySnapshot:
    """일별 포트폴리오 스냅샷."""

    snapshot_date: date
    cash: int
    portfolio_value: int  # 보유 주식 평가액 (종가 기준)
    total_value: int  # cash + portfolio_value
    position_count: int
    daily_return_pct: float = 0.0
    regime: MarketRegime = MarketRegime.SIDEWAYS


# =====================================================================
# 입력 데이터 모델
# =====================================================================


@dataclass
class WatchlistEntry:
    """일별 워치리스트 항목."""

    stock_code: str
    stock_name: str
    snapshot_date: date
    hybrid_score: float
    llm_score: float
    trade_tier: TradeTier
    risk_tag: str = "NEUTRAL"
    rank: int = 99
    sector_group: SectorGroup | None = None


@dataclass
class DailyOHLCV:
    """일봉 가격 데이터."""

    price_date: date
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int


@dataclass
class MacroDay:
    """일별 매크로 데이터."""

    insight_date: date
    sentiment: str
    regime: MarketRegime
    position_size_pct: int = 100
    stop_loss_adjust_pct: int = 100


@dataclass
class PriceCache:
    """종목별 가격 데이터 캐시 — O(1) 날짜 lookup."""

    by_stock_date: dict[str, dict[date, DailyOHLCV]] = field(default_factory=dict)
    by_stock_sorted: dict[str, list[DailyOHLCV]] = field(default_factory=dict)

    def get(self, stock_code: str, d: date) -> DailyOHLCV | None:
        return self.by_stock_date.get(stock_code, {}).get(d)

    def get_history_until(self, stock_code: str, d: date, n: int = 60) -> list[DailyOHLCV]:
        """d 이전(포함) 최대 n개 가격 반환 (oldest first)."""
        prices = self.by_stock_sorted.get(stock_code, [])
        end = 0
        for i, p in enumerate(prices):
            if p.price_date <= d:
                end = i + 1
            else:
                break
        return prices[max(0, end - n) : end]

    def get_close_prices_until(self, stock_code: str, d: date, n: int = 60) -> list[float]:
        """d 이전(포함) 최대 n개 종가 반환 (oldest first)."""
        return [p.close_price for p in self.get_history_until(stock_code, d, n)]


__all__ = [
    "BacktestConfig",
    "DailyOHLCV",
    "DailySnapshot",
    "MacroDay",
    "PriceCache",
    "RiskParams",
    "SellParams",
    "SimPosition",
    "TradeLog",
    "WatchlistEntry",
]
