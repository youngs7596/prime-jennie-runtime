"""Prime Jennie v3 백테스트 엔진.

Phase 2.10 에선 인프라 계층 (시뮬레이션/지표/데이터 로더) 만 포팅. 전략 결정 로직은
v3 `slow_loop/strategy/` 와의 정렬이 필요해 Phase 3 로 이관 (AGENTS.md 참조).
"""

from .data_loader import (
    get_trading_dates,
    load_macro_days,
    load_prices,
    load_quant_scores,
    load_watchlists,
)
from .domain import MarketRegime, SectorGroup, SellReason, SignalType, TradeTier
from .metrics import BacktestMetrics, calculate_metrics, export_csv, print_report
from .models import (
    BacktestConfig,
    DailyOHLCV,
    DailySnapshot,
    MacroDay,
    PriceCache,
    RiskParams,
    SellParams,
    SimPosition,
    TradeLog,
    WatchlistEntry,
)
from .portfolio import SimulatedPortfolio

__all__ = [
    "BacktestConfig",
    "BacktestMetrics",
    "DailyOHLCV",
    "DailySnapshot",
    "MacroDay",
    "MarketRegime",
    "PriceCache",
    "RiskParams",
    "SectorGroup",
    "SellParams",
    "SellReason",
    "SignalType",
    "SimPosition",
    "SimulatedPortfolio",
    "TradeLog",
    "TradeTier",
    "WatchlistEntry",
    "calculate_metrics",
    "export_csv",
    "get_trading_dates",
    "load_macro_days",
    "load_prices",
    "load_quant_scores",
    "load_watchlists",
    "print_report",
]
