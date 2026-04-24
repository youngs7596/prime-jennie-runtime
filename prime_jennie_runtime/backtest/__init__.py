"""Prime Jennie v3 백테스트.

v2 engine (monolith SimulatedPortfolio) 은 제거됨. v3 는 실시간 운영에 쓰이는
`fast_loop.exit_evaluator` 를 **그대로** 일봉 시퀀스에 먹여서 시뮬한다 — 운영
로직과 백테스트 로직이 단일 소스 (drift 방지).

공개 API
    - simulate_sheet: 단일 PositionSheet → 체결/청산 시뮬
    - summarize / format_report: 다수 시트 결과 집계
    - load_daily_bars: v3 daily_prices → DailyBar 시퀀스
"""

from .data_loader import load_daily_bars
from .domain import BacktestConfig, DailyBar, SheetBacktestResult, Trade
from .metrics import BacktestSummary, ReasonStats, format_report, summarize
from .runner import simulate_sheet

__all__ = [
    "BacktestConfig",
    "BacktestSummary",
    "DailyBar",
    "ReasonStats",
    "SheetBacktestResult",
    "Trade",
    "format_report",
    "load_daily_bars",
    "simulate_sheet",
    "summarize",
]
