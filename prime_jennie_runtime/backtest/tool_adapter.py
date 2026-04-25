"""minyoung-mah `ToolAdapter` 구현 — 백테스트 엔진 래퍼.

Phase 3 meta 레이어의 Eval Pipeline 이 후보 변경(예: exit rule 파라미터, Scout
프롬프트 변형)을 평가할 때 호출하는 단일 도구. 내부적으로
`simulate_sheet()` 를 그대로 위임 — exit rule 로직은 `fast_loop.exit_evaluator`
하나만 (drift 방지, runner.AGENTS.md 핵심 원칙).

ToolAdapter 프로토콜
--------------------
``minyoung_mah.core.protocols.ToolAdapter`` 는 ``name``, ``description``,
``arg_schema`` 속성과 ``async call(args: BaseModel) -> ToolResult`` 를 요구한다.
실패는 raise 하지 않고 ``ToolResult(ok=False, error=..., error_category=...)`` 로
반환해야 한다.

직렬화 가능한 입력
------------------
meta 레이어가 LLM/orchestrator 를 통해 호출할 수 있어야 하므로 모든 입력은
JSON-친화 dict 로도 받을 수 있다. ``PositionSheet`` 인스턴스를 직접 넘겨도 되고,
schema v1.1 dict 를 넘겨도 Pydantic 검증을 거친다. ``DailyBar`` 도 동일.

비동기 호환
-----------
``simulate_sheet()`` 는 CPU 작업이지만 보통 단건은 빠르게 끝난다 (수 ms ~ 수십
ms). 그래도 이벤트 루프를 막지 않도록 ``asyncio.to_thread`` 로 감싼다.
대량 그리드 서치는 ``scripts/grid_search.py`` 가 process pool 로 처리한다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar

from minyoung_mah import ErrorCategory, ToolResult
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prime_jennie_runtime.position_sheet.schema import PositionSheet

from .domain import BacktestConfig, DailyBar, SheetBacktestResult
from .metrics import BacktestSummary, summarize
from .runner import simulate_sheet

ADAPTER_NAME = "backtest_simulate_sheet"
ADAPTER_DESCRIPTION = (
    "Simulate a single PositionSheet (schema v1.1) against a daily bar sequence. "
    "Wraps fast_loop.exit_evaluator via simulate_sheet() — production parity, "
    "no separate exit logic. Returns net PnL, exit reason, and trade ledger."
)


# ---------------------------------------------------------------------------
# 입력 스키마 — JSON 직렬화 가능한 dict 또는 도메인 객체 둘 다 허용
# ---------------------------------------------------------------------------


class DailyBarPayload(BaseModel):
    """`DailyBar` 의 Pydantic 표현. dict 입력을 검증해서 도메인 객체로 변환."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    price_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_domain(self) -> DailyBar:
        return DailyBar(
            ticker=self.ticker,
            price_date=self.price_date,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class BacktestConfigOverride(BaseModel):
    """선택적 BacktestConfig 오버라이드. 미지정 필드는 default 사용."""

    model_config = ConfigDict(frozen=True)

    buy_fee_pct: float | None = None
    sell_fee_pct: float | None = None
    slippage_pct: float | None = None

    def to_domain(self) -> BacktestConfig:
        defaults = BacktestConfig()
        return BacktestConfig(
            buy_fee_pct=self.buy_fee_pct if self.buy_fee_pct is not None else defaults.buy_fee_pct,
            sell_fee_pct=self.sell_fee_pct
            if self.sell_fee_pct is not None
            else defaults.sell_fee_pct,
            slippage_pct=self.slippage_pct
            if self.slippage_pct is not None
            else defaults.slippage_pct,
        )


class BacktestToolAdapterArgs(BaseModel):
    """`BacktestToolAdapter.call()` 의 입력.

    ``sheet`` 와 ``bars`` 는 dict 도 허용 (JSON-네이티브 호출). ``PositionSheet``
    인스턴스를 넘기면 그대로 통과시키고, dict 면 schema v1.1 검증을 거친다.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sheet: PositionSheet | dict[str, Any]
    bars: list[DailyBarPayload | dict[str, Any]] = Field(min_length=1)
    capital_krw: float = Field(gt=0)
    entry_index: int = Field(default=0, ge=0)
    config: BacktestConfigOverride | None = None

    def resolve_sheet(self) -> PositionSheet:
        if isinstance(self.sheet, PositionSheet):
            return self.sheet
        return PositionSheet.model_validate(self.sheet)

    def resolve_bars(self) -> list[DailyBar]:
        result: list[DailyBar] = []
        for b in self.bars:
            if isinstance(b, DailyBarPayload):
                result.append(b.to_domain())
            else:
                result.append(DailyBarPayload.model_validate(b).to_domain())
        return result

    def resolve_config(self) -> BacktestConfig:
        return (self.config or BacktestConfigOverride()).to_domain()


# ---------------------------------------------------------------------------
# 출력 스키마 — meta 레이어가 그대로 직렬화/저장할 수 있게 BaseModel 로
# ---------------------------------------------------------------------------


class TradeRecord(BaseModel):
    """`Trade` 의 직렬화 형태."""

    model_config = ConfigDict(frozen=True)

    sheet_id: str
    ticker: str
    side: str
    qty: int
    price: float
    fee: float
    ts: datetime
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SheetResultPayload(BaseModel):
    """`SheetBacktestResult` 의 직렬화 형태."""

    model_config = ConfigDict(frozen=True)

    sheet_id: str
    ticker: str
    strategy_tag: str
    filled: bool
    trades: list[TradeRecord]
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None
    entry_price: float = 0.0
    total_qty: int = 0
    gross_pnl_krw: float = 0.0
    fees_krw: float = 0.0
    net_pnl_krw: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str | None = None

    @classmethod
    def from_domain(cls, result: SheetBacktestResult) -> SheetResultPayload:
        return cls(
            sheet_id=result.sheet_id,
            ticker=result.ticker,
            strategy_tag=result.strategy_tag,
            filled=result.filled,
            trades=[
                TradeRecord(
                    sheet_id=t.sheet_id,
                    ticker=t.ticker,
                    side=t.side,
                    qty=t.qty,
                    price=t.price,
                    fee=t.fee,
                    ts=t.ts,
                    reason=t.reason,
                    metadata=dict(t.metadata),
                )
                for t in result.trades
            ],
            entry_ts=result.entry_ts,
            exit_ts=result.exit_ts,
            entry_price=result.entry_price,
            total_qty=result.total_qty,
            gross_pnl_krw=result.gross_pnl_krw,
            fees_krw=result.fees_krw,
            net_pnl_krw=result.net_pnl_krw,
            pnl_pct=result.pnl_pct,
            exit_reason=result.exit_reason,
        )


class SummaryPayload(BaseModel):
    """`BacktestSummary` 의 직렬화 형태 — 단일 시트 호출이라도 일관성 위해 항상 포함.

    by_exit_reason / by_strategy_tag 등 dict 필드는 ReasonStats 를 그대로 dict 화.
    """

    model_config = ConfigDict(frozen=True)

    n_total: int
    n_filled: int
    n_unfilled: int
    avg_pnl_pct: float
    median_pnl_pct: float
    std_pnl_pct: float
    hit_rate: float
    sharpe_like: float
    total_gross_pnl_krw: float
    total_fees_krw: float
    total_net_pnl_krw: float
    by_exit_reason: dict[str, dict[str, float | int]]
    by_strategy_tag: dict[str, dict[str, float | int]]
    by_unfilled_reason: dict[str, int]

    @classmethod
    def from_domain(cls, summary: BacktestSummary) -> SummaryPayload:
        def _stats(d: dict) -> dict[str, dict[str, float | int]]:
            return {
                k: {
                    "count": v.count,
                    "avg_pnl_pct": v.avg_pnl_pct,
                    "total_net_pnl_krw": v.total_net_pnl_krw,
                }
                for k, v in d.items()
            }

        return cls(
            n_total=summary.n_total,
            n_filled=summary.n_filled,
            n_unfilled=summary.n_unfilled,
            avg_pnl_pct=summary.avg_pnl_pct,
            median_pnl_pct=summary.median_pnl_pct,
            std_pnl_pct=summary.std_pnl_pct,
            hit_rate=summary.hit_rate,
            sharpe_like=summary.sharpe_like,
            total_gross_pnl_krw=summary.total_gross_pnl_krw,
            total_fees_krw=summary.total_fees_krw,
            total_net_pnl_krw=summary.total_net_pnl_krw,
            by_exit_reason=_stats(summary.by_exit_reason),
            by_strategy_tag=_stats(summary.by_strategy_tag),
            by_unfilled_reason=dict(summary.by_unfilled_reason),
        )


class BacktestToolAdapterValue(BaseModel):
    """`ToolResult.value` 페이로드 — `result` + `summary`."""

    model_config = ConfigDict(frozen=True)

    result: SheetResultPayload
    summary: SummaryPayload


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class BacktestToolAdapter:
    """minyoung-mah `ToolAdapter` 구현 — `simulate_sheet` 1회 호출 래퍼.

    Phase 3 meta Eval Pipeline 이 후보 변경의 과거 성과를 측정할 때 사용한다.
    그리드 서치(다수 조합) 는 호출자(meta 또는 ``scripts/grid_search.py``) 가
    이 어댑터를 N 번 호출 — 어댑터는 단일 시트 단위까지만 책임진다 (backtest
    AGENTS.md "grid search 2,800 조합 — Phase 3 meta 레이어가 담당").
    """

    name: ClassVar[str] = ADAPTER_NAME
    description: ClassVar[str] = ADAPTER_DESCRIPTION
    arg_schema: ClassVar[type[BaseModel]] = BacktestToolAdapterArgs

    async def call(self, args: BaseModel) -> ToolResult:
        start = time.perf_counter()

        # 프로토콜 상 타입은 BaseModel 이므로 런타임 분기로 narrow
        if not isinstance(args, BacktestToolAdapterArgs):
            try:
                args = BacktestToolAdapterArgs.model_validate(
                    args.model_dump() if isinstance(args, BaseModel) else args
                )
            except ValidationError as e:
                return ToolResult(
                    ok=False,
                    value=None,
                    error=f"invalid args: {e}",
                    error_category=ErrorCategory.PARSE_ERROR,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )

        try:
            sheet = args.resolve_sheet()
            bars = args.resolve_bars()
            cfg = args.resolve_config()
        except ValidationError as e:
            return ToolResult(
                ok=False,
                value=None,
                error=f"schema validation failed: {e}",
                error_category=ErrorCategory.PARSE_ERROR,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except (ValueError, TypeError) as e:
            return ToolResult(
                ok=False,
                value=None,
                error=f"invalid input: {e}",
                error_category=ErrorCategory.TOOL_ERROR,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        try:
            sheet_result = await asyncio.to_thread(
                simulate_sheet,
                sheet,
                bars,
                entry_index=args.entry_index,
                capital_krw=args.capital_krw,
                config=cfg,
            )
        except Exception as e:  # noqa: BLE001 — 어댑터는 raise 금지, 카테고리화 후 ToolResult 로
            return ToolResult(
                ok=False,
                value=None,
                error=f"simulate_sheet failed: {type(e).__name__}: {e}",
                error_category=ErrorCategory.TOOL_ERROR,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        summary = summarize([sheet_result])
        value = BacktestToolAdapterValue(
            result=SheetResultPayload.from_domain(sheet_result),
            summary=SummaryPayload.from_domain(summary),
        )
        return ToolResult(
            ok=True,
            value=value,
            duration_ms=int((time.perf_counter() - start) * 1000),
            metadata={
                "filled": sheet_result.filled,
                "exit_reason": sheet_result.exit_reason,
                "n_trades": len(sheet_result.trades),
            },
        )


__all__ = [
    "ADAPTER_DESCRIPTION",
    "ADAPTER_NAME",
    "BacktestConfigOverride",
    "BacktestToolAdapter",
    "BacktestToolAdapterArgs",
    "BacktestToolAdapterValue",
    "DailyBarPayload",
    "SheetResultPayload",
    "SummaryPayload",
    "TradeRecord",
]
