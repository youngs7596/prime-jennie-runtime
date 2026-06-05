"""FastAPI Gateway 서버 엔드포인트 smoke 테스트."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
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
        self.balance_calls: int = 0
        self.balance_error: Exception | None = None
        self.buying_power_calls: int = 0
        # KIS chk-holiday 응답 (is-market-open 엔드포인트의 외부 판정 경로).
        self.trading_day_result: bool = True
        self.trading_day_calls: int = 0

    async def is_trading_day(self, target_date=None) -> bool:
        self.trading_day_calls += 1
        return self.trading_day_result

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
        self.balance_calls += 1
        if self.balance_error is not None:
            raise self.balance_error
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

    async def get_buying_power(self) -> int:
        self.buying_power_calls += 1
        return 1_500_000

    async def get_daily_executions(self, start_date, end_date, *, stock_code: str = ""):
        return [
            {
                "order_date": "20260514",
                "order_time": "103114",
                "stock_code": "241560",
                "stock_name": "두산밥캣",
                "side": "buy",
                "order_qty": 113,
                "filled_qty": 113,
                "avg_price": 69076.0,
                "filled_amount": 7805700,
                "order_no": "0018495800",
                "cancelled": False,
            }
        ]

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


def test_cash_uses_light_buying_power(kis_config):
    """/api/cash 는 신선한 잔고 캐시가 없으면 무거운 잔고 대신 매수가능조회만 부른다."""
    fake = FakeKisApi()
    client = _build_client(kis_config, fake)
    resp = client.get("/api/cash")
    assert resp.json()["cash_balance"] == 1_500_000
    assert fake.buying_power_calls == 1
    assert fake.balance_calls == 0


def test_cash_reuses_fresh_balance_cache(kis_config):
    """잔고가 방금 캐시됐으면 /api/cash 는 추가 KIS 호출 없이 그 값을 쓴다."""
    fake = FakeKisApi()
    client = _build_client(kis_config, fake)
    client.get("/api/balance")  # 잔고 캐시 채움 (balance_calls=1)
    resp = client.get("/api/cash")
    assert resp.json()["cash_balance"] == 1_500_000
    assert fake.balance_calls == 1
    assert fake.buying_power_calls == 0  # 캐시 재사용 — 매수가능조회 안 함


async def test_balance_returns_stale_on_failure(kis_config):
    """조회 거부 시 직전 정상값을 stale 로 반환 — 재조회 cascade 차단."""
    fake = FakeKisApi()
    state = GatewayState(
        config=kis_config,
        kis_api=fake,  # type: ignore[arg-type]
        calendar=MarketCalendar(trading_day_checker=lambda _d: True),
    )
    first = await state.get_balance_cached()
    assert first["cash_balance"] == 1_500_000

    state.invalidate_balance_cache()  # TTL 캐시 비우고
    fake.balance_error = RuntimeError("EGW00201 throttle")  # 다음 조회 실패
    stale = await state.get_balance_cached()
    assert stale["cash_balance"] == 1_500_000  # 직전 정상값


async def test_balance_raises_without_last_good(kis_config):
    """직전 정상값이 없으면 실패를 그대로 올린다 (stale 로 가릴 게 없음)."""
    fake = FakeKisApi()
    fake.balance_error = RuntimeError("EGW00201 throttle")
    state = GatewayState(
        config=kis_config,
        kis_api=fake,  # type: ignore[arg-type]
        calendar=MarketCalendar(trading_day_checker=lambda _d: True),
    )
    with pytest.raises(RuntimeError):
        await state.get_balance_cached()


def test_order_invalidates_balance_cache(kis_config):
    """주문 직후 잔고 캐시가 비어 다음 조회를 새로 받는다."""
    fake = FakeKisApi()
    client = _build_client(kis_config, fake)
    client.get("/api/balance")  # balance_calls=1, 캐시됨
    client.post(
        "/api/order/buy",
        json={"stock_code": "005930", "quantity": 5, "order_type": "market"},
    )
    client.get("/api/balance")  # 캐시 무효화됐으므로 다시 호출
    assert fake.balance_calls == 2


def test_balance_cached_within_ttl(kis_config):
    """TTL 안의 연속 잔고 조회는 KIS 호출 1번을 공유한다 — 원장 충돌 방지의 핵심.

    KIS 원장은 같은 계좌의 근접/동시 조회를 EGW00201 로 거부한다 (2026-06-03 실측).
    monitor/slow-loop/UI 가 각자 물어도 gateway 가 캐시로 합쳐 KIS 로는 1건만 나간다.
    """
    fake = FakeKisApi()
    client = _build_client(kis_config, fake)

    # 연속 3회 (balance 2 + cash 1) — 모두 같은 KIS 호출 결과를 공유.
    r1 = client.get("/api/balance")
    r2 = client.get("/api/balance")
    r3 = client.get("/api/cash")
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert fake.balance_calls == 1

    # 같은 데이터가 돌아온다.
    assert r1.json()["cash_balance"] == r3.json()["cash_balance"]


def test_balance_cache_expires_after_ttl(kis_config, monkeypatch):
    """TTL 이 지나면 KIS 를 다시 부른다."""
    fake = FakeKisApi()
    state = GatewayState(
        config=kis_config,
        kis_api=fake,  # type: ignore[arg-type]
        calendar=MarketCalendar(trading_day_checker=lambda _d: True),
    )
    app = create_app(state=state)
    client = TestClient(app)

    client.get("/api/balance")
    assert fake.balance_calls == 1

    # 캐시 시각을 TTL 너머로 되돌려 만료 상황 재현.
    fetched_at, data = state._balance_cache
    state._balance_cache = (fetched_at - state.balance_cache_ttl - 1.0, data)

    client.get("/api/balance")
    assert fake.balance_calls == 2


def test_executions_endpoint(kis_config):
    client = _build_client(kis_config, FakeKisApi())
    resp = client.get(
        "/api/executions",
        params={"start_date": "20260501", "end_date": "20260520", "stock_code": "241560"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["stock_code"] == "241560"
    assert body[0]["side"] == "buy"
    assert body[0]["filled_qty"] == 113
    assert body[0]["order_no"] == "0018495800"


def test_executions_endpoint_rejects_bad_date(kis_config):
    client = _build_client(kis_config, FakeKisApi())
    resp = client.get(
        "/api/executions", params={"start_date": "2026-05-01", "end_date": "20260520"}
    )
    assert resp.status_code == 422


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
    # 체커가 주입된 calendar 면 KIS chk-holiday 외부 판정을 호출하지 않는다.
    assert fake.trading_day_calls == 0


def test_is_market_open_kis_holiday_detected(kis_config):
    """체커 없는 calendar (운영 기본) 에서 KIS chk-holiday 가 휴장일을 잡아낸다.

    2026-06-03 지방선거일 사고 재발 방지 — 평일인데 KIS 휴장일이면 'holiday' 를
    반환해야 slow_loop/job_worker 의 휴장일 가드가 동작한다.
    """
    from datetime import timedelta, timezone

    kst = timezone(timedelta(hours=9))
    fake = FakeKisApi()
    fake.trading_day_result = False  # KIS: 오늘은 휴장일

    state = GatewayState(
        config=kis_config,
        kis_api=fake,  # type: ignore[arg-type]
        calendar=MarketCalendar(),  # 체커 없음 — 운영 lifespan 과 동일
    )
    # 2026-06-03 수요일 (지방선거 휴장일) 장중 시각
    state.calendar.set_clock(lambda: datetime(2026, 6, 3, 10, 0, tzinfo=kst))

    app = create_app(state=state)
    client = TestClient(app)

    resp = client.get("/api/market/is-market-open")
    assert resp.status_code == 200
    assert resp.json() == {"is_open": False, "session": "holiday"}
    assert fake.trading_day_calls == 1

    # 같은 날 두 번째 조회는 캐시 — KIS 재호출 없음.
    resp2 = client.get("/api/market/is-market-open")
    assert resp2.json() == {"is_open": False, "session": "holiday"}
    assert fake.trading_day_calls == 1
