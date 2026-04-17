"""FastAPI Gateway 서버 엔드포인트 smoke 테스트."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from prime_jennie_runtime.kis_gateway.kis_api import KISApiError
from prime_jennie_runtime.kis_gateway.market_hours import MarketCalendar
from prime_jennie_runtime.kis_gateway.schemas import StockSnapshot
from prime_jennie_runtime.kis_gateway.server import GatewayState, create_app


class FakeKisApi:
    """테스트용 KISApi 대체 — 원하는 반환값/예외를 설정 가능."""

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.snapshot_result: StockSnapshot | None = None
        self.snapshot_error: Exception | None = None
        self.balance_result: dict | None = None

    async def get_snapshot(self, stock_code: str) -> StockSnapshot:
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.snapshot_result or StockSnapshot(
            stock_code=stock_code,
            price=71200,
            high_price=71500,
            volume=1000,
            timestamp=datetime.now(UTC),
        )

    async def place_order(self, **kwargs) -> dict:
        self.orders.append(kwargs)
        return {"order_no": "ORD-TEST", "order_time": "090000"}

    async def cancel_order(self, order_no: str) -> bool:
        return True

    async def check_order_status(self, order_no: str) -> dict:
        return {"filled": True, "filled_qty": 10, "avg_price": 71000.0}

    async def get_balance(self) -> dict:
        return self.balance_result or {
            "positions": [
                {
                    "stock_code": "005930",
                    "stock_name": "삼성전자",
                    "quantity": 10,
                    "average_buy_price": 70000,
                    "total_buy_amount": 700000,
                    "current_price": 71000,
                    "current_value": 710000,
                    "profit_pct": 1.43,
                }
            ],
            "cash_balance": 1_500_000,
            "total_asset": 2_210_000,
            "stock_eval_amount": 710_000,
        }

    async def close(self) -> None:
        return None


def _build_client(kis_config, fake_api: FakeKisApi) -> TestClient:
    state = GatewayState(
        config=kis_config,
        kis_api=fake_api,  # type: ignore[arg-type]
        calendar=MarketCalendar(trading_day_checker=lambda _d: True),
    )
    app = create_app(state=state)
    return TestClient(app)


def test_health_returns_ok(kis_config):
    client = _build_client(kis_config, FakeKisApi())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_snapshot_endpoint(kis_config):
    client = _build_client(kis_config, FakeKisApi())
    resp = client.get("/api/snapshot/005930")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stock_code"] == "005930"
    assert body["price"] == 71200


def test_snapshot_502_on_kis_error(kis_config):
    fake = FakeKisApi()
    fake.snapshot_error = KISApiError("oops", rt_cd="1")
    client = _build_client(kis_config, fake)
    resp = client.get("/api/snapshot/005930")
    assert resp.status_code == 502


def test_order_buy_success(kis_config):
    fake = FakeKisApi()
    client = _build_client(kis_config, fake)
    resp = client.post(
        "/api/order/buy",
        json={"stock_code": "005930", "quantity": 5, "order_type": "market"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["order_no"] == "ORD-TEST"
    assert fake.orders[0]["order_type"] == "buy"


def test_balance_endpoint(kis_config):
    client = _build_client(kis_config, FakeKisApi())
    resp = client.get("/api/balance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cash_balance"] == 1_500_000
    assert len(body["positions"]) == 1


def test_cash_endpoint(kis_config):
    client = _build_client(kis_config, FakeKisApi())
    resp = client.get("/api/cash")
    assert resp.status_code == 200
    assert resp.json()["cash_balance"] == 1_500_000


def test_is_market_open_endpoint(kis_config):
    fake = FakeKisApi()
    state = GatewayState(
        config=kis_config,
        kis_api=fake,  # type: ignore[arg-type]
        calendar=MarketCalendar(trading_day_checker=lambda _d: True),
    )
    # 장 시간으로 고정
    from datetime import timedelta, timezone

    kst = timezone(timedelta(hours=9))
    state.calendar.set_clock(lambda: datetime(2026, 4, 17, 10, 0, tzinfo=kst))

    app = create_app(state=state)
    client = TestClient(app)
    resp = client.get("/api/market/is-market-open")
    assert resp.status_code == 200
    assert resp.json() == {"is_open": True, "session": "regular"}
