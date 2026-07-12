"""KIS Gateway 요청/응답 스키마.

v2 `prime_jennie.domain.stock`/`trading`/`portfolio` + v2 gateway app.py 의 inline
BaseModel 들을 하나로 통합. Gateway 독립 구동을 위해 외부 도메인 모듈에 의존하지 않음.

원본:
  - prime_jennie/domain/stock.py (StockSnapshot, DailyPrice, MinutePrice)
  - prime_jennie/domain/trading.py (OrderRequest, OrderResult)
  - prime_jennie/domain/portfolio.py (Position, PortfolioState)
  - prime_jennie/services/gateway/app.py (SnapshotRequest, SubscribeRequest 등)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# ─── Market Data ─────────────────────────────────────────────────


class StockSnapshot(BaseModel):
    """실시간 스냅샷 — KIS FHKST01010100 응답."""

    stock_code: str
    price: int
    open_price: int = 0
    high_price: int = 0
    low_price: int = 0
    volume: int = 0
    change_pct: float = 0.0
    per: float | None = None
    pbr: float | None = None
    market_cap: int | None = None
    high_52w: int | None = None
    low_52w: int | None = None
    timestamp: datetime


class DailyPrice(BaseModel):
    """일별 OHLCV."""

    stock_code: str
    price_date: date
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int
    change_pct: float | None = None


class MinutePrice(BaseModel):
    """분봉 OHLCV."""

    stock_code: str
    price_datetime: datetime
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int


# ─── Orders ──────────────────────────────────────────────────────


class OrderRequest(BaseModel):
    """Gateway 주문 요청."""

    stock_code: str = Field(pattern=r"^\d{6}$")
    quantity: int = Field(gt=0)
    order_type: Literal["market", "limit"] = "market"
    price: int | None = None  # limit 주문 시 필수


class OrderResult(BaseModel):
    """Gateway 주문 결과."""

    success: bool
    order_no: str | None = None
    stock_code: str
    quantity: int
    price: int
    message: str | None = None


class CancelRequest(BaseModel):
    order_no: str


class OrderStatusRequest(BaseModel):
    order_no: str


class OrderStatusResult(BaseModel):
    """체결 상태 — check_order_status 반환."""

    filled: bool
    filled_qty: int
    avg_price: float


# ─── Account ─────────────────────────────────────────────────────


class Position(BaseModel):
    """보유 종목."""

    stock_code: str
    stock_name: str
    quantity: int
    average_buy_price: int
    total_buy_amount: int
    current_price: int | None = None
    current_value: int | None = None
    profit_pct: float | None = None


class PortfolioState(BaseModel):
    """잔고 + 현금 종합 상태."""

    positions: list[Position]
    cash_balance: int
    total_asset: int
    stock_eval_amount: int
    position_count: int
    timestamp: datetime


class DailyExecution(BaseModel):
    """일별 주문체결 1건 — KIS inquire-daily-ccld output1.

    runtime state ↔ KIS drift 진단용 (positions_reconcile 의 qty_mismatch 후속).
    """

    order_date: str  # 주문일자 YYYYMMDD
    order_time: str  # 주문시각 HHMMSS
    stock_code: str
    stock_name: str
    side: Literal["buy", "sell"]
    order_qty: int  # 주문수량
    filled_qty: int  # 총체결수량
    avg_price: float  # 체결평균가
    filled_amount: int  # 총체결금액
    order_no: str  # 주문번호
    cancelled: bool  # 취소 여부


class FuturesQuote(BaseModel):
    """KOSPI200 지수선물 시세 (FHMIF10000000).

    미결제약정(open_interest)·베이시스가 핵심 — 투자자별 선물 수급은 KIS 가 주지
    않으므로(2026-07-12 실측, 선물 시장구분 넣으면 전 항목 0), 위조 불가능한
    집계치인 OI·베이시스로 대체한다.
    """

    contract_code: str  # A01609 = 정규 KOSPI200 선물 2026-09월물
    contract_name: str  # "F 202609"
    price: float
    open_interest: int  # hts_otst_stpl_qty — 미결제약정 수량
    oi_change: int  # otst_stpl_qty_icdc — 미결제약정 증감 (전일 대비)
    volume: int
    basis: float | None = None  # 선물 - 현물
    market_basis: float | None = None
    theoretical_price: float | None = None
    disparity: float | None = None  # 괴리율 (선물 - 이론가)
    remaining_days: int | None = None  # 잔존일수 — 롤오버 판정용
    kospi_index: float | None = None  # 코스피 종합지수 (output2)
    timestamp: datetime


# ─── Gateway request bodies ──────────────────────────────────────


class SnapshotRequest(BaseModel):
    stock_code: str = Field(pattern=r"^\d{6}$")


class DailyPricesRequest(BaseModel):
    stock_code: str = Field(pattern=r"^\d{6}$")
    days: int = Field(default=150, ge=1, le=500)


class MinutePricesRequest(BaseModel):
    stock_code: str = Field(pattern=r"^\d{6}$")


class SubscribeRequest(BaseModel):
    codes: list[str] = Field(min_length=1)
