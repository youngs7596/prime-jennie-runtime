"""KIS Gateway FastAPI 서버 — KIS API 중앙 프록시.

모든 KIS API 호출을 중앙화하여 레이트 리밋, 서킷 브레이커 적용.
다른 서비스는 이 Gateway HTTP API 를 통해서만 KIS API 에 접근.

원본: prime_jennie/services/gateway/app.py (sync → async + 구조 정리)

변경점:
  - slowapi → 자체 AsyncRateLimiter (시세 19/sec, 매매 5/sec)
  - pybreaker → 자체 AsyncCircuitBreaker
  - DB 폴백(일봉 부재 시 DB 조회)은 Phase 2 로 연기 — 실패 시 502
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from minyoung_mah import Observer

from prime_jennie_runtime.infra.config import KISConfig

from .circuit_breaker import AsyncCircuitBreaker, CircuitBreakerError
from .db_fallback import FallbackPriceService
from .kis_api import KISApi, KISApiError
from .market_hours import MarketCalendar
from .mode_safety import enforce_mode_safety
from .poller import KISRestPoller
from .price_repo import PriceRepo
from .rate_limiter import AsyncRateLimiter
from .schemas import (
    CancelRequest,
    DailyExecution,
    DailyPrice,
    DailyPricesRequest,
    MinutePrice,
    MinutePricesRequest,
    OrderRequest,
    OrderResult,
    OrderStatusRequest,
    OrderStatusResult,
    PortfolioState,
    Position,
    SnapshotRequest,
    StockSnapshot,
    SubscribeRequest,
)
from .streamer import KISWebSocketStreamer

logger = logging.getLogger(__name__)

Streamer = KISWebSocketStreamer | KISRestPoller


class GatewayState:
    """Gateway 런타임 상태 컨테이너 — 테스트/프로덕션 모두에서 재사용."""

    def __init__(
        self,
        *,
        config: KISConfig,
        kis_api: KISApi,
        streamer: Streamer | None = None,
        calendar: MarketCalendar | None = None,
        price_repo: PriceRepo | None = None,
        observer: Observer | None = None,
    ):
        self.config = config
        self.kis_api = kis_api
        self.streamer = streamer
        self.calendar = calendar or MarketCalendar()
        self.market_limiter = AsyncRateLimiter(rate=config.rate_limit_market_per_sec)
        self.trade_limiter = AsyncRateLimiter(rate=config.rate_limit_trade_per_sec)
        self.circuit_breaker = AsyncCircuitBreaker(
            fail_max=config.circuit_fail_max,
            reset_sec=config.circuit_reset_sec,
        )
        self.request_history: deque = deque(maxlen=100)
        self.price_repo = price_repo
        self.observer = observer
        # Phase 2.3: price_repo 주입 시 fallback 서비스 활성화 (D2).
        self.fallback: FallbackPriceService | None = (
            FallbackPriceService(kis_api, self.circuit_breaker, price_repo, observer=observer)
            if price_repo is not None
            else None
        )
        # 잔고 (원장) 조회 캐시 + 단일 비행 — KIS 원장은 같은 계좌에 대한 근접/동시
        # 조회를 EGW00201 로 거부한다 (2026-06-03 직접 호출 실측: 동시 2건은 둘 다 거부,
        # 시차 있으면 통과). monitor(30s)·slow-loop(2min)·UI 폴링이 각자 잔고를 물으면
        # 충돌이 불가피하므로, gateway 가 응답을 짧게 캐싱해 KIS 로 가는 잔고 조회를
        # TTL 당 1건으로 합친다.
        self.balance_cache_ttl = config.balance_cache_ttl_sec
        self.balance_max_stale = config.balance_max_stale_sec
        self._balance_cache: tuple[float, dict[str, Any]] | None = None
        # 직전 정상 잔고 — 조회 거부 시 이 값을 stale 로 돌려 cascade 를 끊는다.
        self._balance_last_good: tuple[float, dict[str, Any]] | None = None
        self._balance_lock = asyncio.Lock()

    def record_request(self, endpoint: str, detail: str) -> None:
        self.request_history.append(
            {"endpoint": endpoint, "detail": detail, "timestamp": time.time()}
        )

    def invalidate_balance_cache(self) -> None:
        """주문 발생 등 잔고가 바뀐 직후 캐시 무효화 — 다음 조회를 새로 받게 한다."""
        self._balance_cache = None

    async def get_cash_light(self) -> int:
        """예수금(매수가능금액) — 신선한 잔고 캐시가 있으면 그 값을, 없으면 가벼운
        매수가능조회만 호출한다. 무거운 잔고조회(inquire-balance) 를 피한다 (KIS 권장,
        2026-06-05). cash_balance 는 잔고/매수가능 모두 buying-power 출처라 동일.
        """
        cached = self._balance_cache
        if cached is not None and time.monotonic() - cached[0] < self.balance_cache_ttl:
            return int(cached[1].get("cash_balance", 0))
        return await self.circuit_breaker.call(self.kis_api.get_buying_power)

    async def get_balance_cached(self) -> dict[str, Any]:
        """잔고 조회 — TTL 캐시 + 단일 비행 (동시 요청은 한 번의 KIS 호출을 공유).

        조회가 거부/실패하면 직전 정상값(``balance_max_stale`` 이내)을 stale 로
        돌려준다 — 일시 throttle 이 캐시 공백 → 재조회 폭풍으로 번지는 것을 막는다
        (2026-06-05). circuit breaker 는 실제 KIS 호출에만 적용.
        """
        now = time.monotonic()
        cached = self._balance_cache
        if cached is not None and now - cached[0] < self.balance_cache_ttl:
            return cached[1]

        async with self._balance_lock:
            # double-check — lock 대기 중에 다른 요청이 채웠으면 그것을 쓴다 (단일 비행).
            now = time.monotonic()
            cached = self._balance_cache
            if cached is not None and now - cached[0] < self.balance_cache_ttl:
                return cached[1]

            try:
                data = await self.circuit_breaker.call(self.kis_api.get_balance)
            except Exception:
                stale = self._balance_last_good
                if stale is not None and time.monotonic() - stale[0] < self.balance_max_stale:
                    logger.warning(
                        "잔고 조회 실패 — 직전 정상값 stale 반환 (나이 %.1fs)",
                        time.monotonic() - stale[0],
                    )
                    return stale[1]
                raise

            stamped = (time.monotonic(), data)
            self._balance_cache = stamped
            self._balance_last_good = stamped
            return data


def create_app(state: GatewayState | None = None, *, config: KISConfig | None = None) -> FastAPI:
    """FastAPI 앱 빌더.

    테스트에서는 미리 조립된 ``state`` 를 주입, 프로덕션에서는 lifespan 에서
    ``config`` 기반으로 초기화.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal state
        redis_client = None
        pg_pool = None
        realtime_buffer = None
        if state is None:
            cfg = config or KISConfig()
            # paper/real 모드 안전장치 — real 모드 시 KIS_REAL_CONFIRMED 누락이면 RuntimeError.
            enforce_mode_safety(cfg)
            api = KISApi(cfg)
            try:
                await api.authenticate()
                logger.info("KIS token pre-authenticated")
            except Exception as e:
                logger.warning("KIS pre-auth failed (will retry on first request): %s", e)

            calendar = MarketCalendar()

            streamer: Streamer | None = None
            try:
                import redis.asyncio as aioredis

                from prime_jennie_runtime.infra.config import RedisConfig

                redis_cfg = RedisConfig()
                redis_client = aioredis.from_url(redis_cfg.url, decode_responses=False)

                mode = cfg.streamer_mode.lower()
                if mode == "poller":
                    streamer = KISRestPoller(
                        redis_client=redis_client,
                        kis_api=api,
                        polling_interval=cfg.polling_interval_sec,
                        calendar=calendar,
                    )
                else:
                    streamer = KISWebSocketStreamer(
                        redis_client=redis_client,
                        app_key=cfg.app_key,
                        app_secret=cfg.app_secret,
                        is_paper=cfg.is_paper,
                        calendar=calendar,
                    )
            except Exception:
                logger.exception("streamer init failed — subscribe API will return 503 until fixed")

            # daily/minute prices fallback — KIS API 실패 시 PG read-through.
            price_repo = None
            try:
                import asyncpg

                from prime_jennie_runtime.infra.config import PostgresConfig

                pg_cfg = PostgresConfig()
                pg_pool = await asyncpg.create_pool(
                    host=pg_cfg.host,
                    port=pg_cfg.port,
                    user=pg_cfg.user,
                    password=pg_cfg.password,
                    database=pg_cfg.db,
                    min_size=1,
                    max_size=4,
                )
                from .price_repo import PostgresPriceRepo

                price_repo = PostgresPriceRepo(conn=pg_pool)
                logger.info("PostgresPriceRepo wired for KIS fallback")
            except Exception:
                logger.exception(
                    "price_repo init failed — daily/minute prices will not have PG fallback"
                )

            # 실시간 체결·호가 적재 버퍼 — 게이트웨이가 pg_pool 로 직접 기록.
            if pg_pool is not None and isinstance(streamer, KISWebSocketStreamer):
                try:
                    from .realtime_persist import RealtimeBuffer

                    realtime_buffer = RealtimeBuffer(pg_pool)
                    streamer.attach_sink(realtime_buffer)
                    await realtime_buffer.start()
                    logger.info("RealtimeBuffer wired — 체결·호가 적재 활성")
                except Exception:
                    logger.exception("realtime buffer init failed — 실시간 적재 비활성")

            state = GatewayState(
                config=cfg,
                kis_api=api,
                streamer=streamer,
                calendar=calendar,
                price_repo=price_repo,
            )

        app.state.gateway = state
        try:
            yield
        finally:
            if state is not None and state.streamer is not None:
                await state.streamer.stop()
            if state is not None:
                await state.kis_api.close()
            if realtime_buffer is not None:
                await realtime_buffer.stop()
            if redis_client is not None:
                await redis_client.aclose()
            if pg_pool is not None:
                await pg_pool.close()

    app = FastAPI(title="KIS Gateway", version="1.0.0", lifespan=lifespan)

    if state is not None:
        app.state.gateway = state

    _register_routes(app)
    return app


def _gw(app_state: Any) -> GatewayState:
    """app.state.gateway 를 꺼내는 타입 보정 헬퍼."""
    gw: GatewayState = app_state.gateway
    return gw


def _register_routes(app: FastAPI) -> None:  # noqa: C901 — 엔드포인트 다수
    """FastAPI 엔드포인트 등록."""

    # ─── Health ──────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, Any]:
        gw = _gw(app.state)
        streamer_running = gw.streamer.is_running if gw.streamer else False
        return {
            "status": "ok",
            "circuit_state": gw.circuit_breaker.state,
            "streamer_running": streamer_running,
        }

    @app.get("/api/stats/kis-calls")
    async def kis_call_stats() -> dict[str, Any]:
        """KIS 로 실제 나가는 초당 호출수 — EGW00201 진단용 (2026-06-05).

        재시도 포함 모든 실호출이 지나는 ``_send`` 길목에서 계측한 값.
        """
        gw = _gw(app.state)
        return gw.kis_api.call_stats()

    # ─── Market ──────────────────────────────────────────────────

    @app.post("/api/snapshot/{ticker}", response_model=StockSnapshot)
    @app.get("/api/snapshot/{ticker}", response_model=StockSnapshot)
    async def api_snapshot(ticker: str) -> StockSnapshot:
        gw = _gw(app.state)
        body = SnapshotRequest(stock_code=ticker)
        gw.record_request("snapshot", body.stock_code)
        await gw.market_limiter.acquire()
        try:
            return await gw.circuit_breaker.call(gw.kis_api.get_snapshot, body.stock_code)
        except CircuitBreakerError as err:
            raise HTTPException(503, "Circuit breaker open") from err
        except KISApiError as e:
            raise HTTPException(502, f"KIS API error: {e}") from e

    @app.post("/api/market/daily-prices", response_model=list[DailyPrice])
    async def api_daily_prices(body: DailyPricesRequest) -> list[DailyPrice]:
        gw = _gw(app.state)
        gw.record_request("daily_prices", body.stock_code)
        await gw.market_limiter.acquire()
        try:
            if gw.fallback is not None:
                return await gw.fallback.get_daily_prices(body.stock_code, body.days)
            return await gw.circuit_breaker.call(
                gw.kis_api.get_daily_prices, body.stock_code, body.days
            )
        except CircuitBreakerError as err:
            raise HTTPException(503, "Circuit breaker open") from err
        except KISApiError as e:
            raise HTTPException(502, f"KIS API error: {e}") from e

    @app.post("/api/market/minute-prices", response_model=list[MinutePrice])
    async def api_minute_prices(body: MinutePricesRequest) -> list[MinutePrice]:
        gw = _gw(app.state)
        gw.record_request("minute_prices", body.stock_code)
        await gw.market_limiter.acquire()
        try:
            if gw.fallback is not None:
                return await gw.fallback.get_minute_prices(body.stock_code)
            return await gw.circuit_breaker.call(gw.kis_api.get_minute_prices, body.stock_code)
        except CircuitBreakerError as err:
            raise HTTPException(503, "Circuit breaker open") from err
        except KISApiError as e:
            raise HTTPException(502, f"KIS API error: {e}") from e

    @app.get("/api/market/is-market-open")
    async def api_is_market_open() -> dict[str, Any]:
        gw = _gw(app.state)
        # MarketCalendar 는 체커 미주입 시 "평일=거래일" 로 가정해 선거일·임시공휴일을
        # 모른다 — 이 상태로는 slow_loop/job_worker 의 휴장일 가드 전체가 무력하다
        # (2026-06-03 지방선거일에 분봉 수집·scout 가 그대로 돌던 실측). 평일이고
        # 아직 외부 판정이 없으면 KIS chk-holiday 로 하루 1회 채운다.
        today_kst = gw.calendar.today_kst()
        if gw.calendar.needs_external_trading_day(today_kst):
            # KISApi.is_trading_day 는 내부에 실패 fallback (주말만 체크) 이 있어
            # KIS 장애 시에도 거래일 가정으로 안전하게 동작한다.
            is_trading = await gw.kis_api.is_trading_day(today_kst)
            gw.calendar.prime_trading_day(today_kst, is_trading)
        open_flag, session_str = gw.calendar.is_market_open()
        return {"is_open": open_flag, "session": session_str}

    @app.get("/api/quotations/search-info/{stock_code}")
    async def api_search_info(stock_code: str) -> dict[str, Any]:
        """종목 기본 정보 (CTPF1002R) — security_type 분류용.

        응답에서 ``scty_grp_id_cd`` 가 ST/EF/EN/EW 중 하나. seed_stock_masters 가
        이 값을 STOCK/ETF/ETN/ELW 로 매핑해 stock_masters.security_type 채운다.
        """
        gw = _gw(app.state)
        info = await gw.kis_api.search_info(stock_code)
        if info is None:
            raise HTTPException(404, f"종목 정보 없음: {stock_code}")
        return info

    # ─── Orders ──────────────────────────────────────────────────

    @app.post("/api/order/buy", response_model=OrderResult)
    async def api_order_buy(order: OrderRequest) -> OrderResult:
        return await _place_order(app, order, "buy")

    @app.post("/api/order/sell", response_model=OrderResult)
    async def api_order_sell(order: OrderRequest) -> OrderResult:
        return await _place_order(app, order, "sell")

    @app.post("/api/order/cancel")
    async def api_order_cancel(body: CancelRequest) -> dict[str, Any]:
        gw = _gw(app.state)
        gw.record_request("cancel", body.order_no)
        await gw.trade_limiter.acquire()
        try:
            success = await gw.circuit_breaker.call(gw.kis_api.cancel_order, body.order_no)
            return {"success": success}
        except CircuitBreakerError as err:
            raise HTTPException(503, "Circuit breaker open") from err
        except KISApiError as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/order/{order_no}", response_model=OrderStatusResult)
    async def api_order_status(order_no: str) -> OrderStatusResult:
        gw = _gw(app.state)
        body = OrderStatusRequest(order_no=order_no)
        gw.record_request("order_status", body.order_no)
        await gw.trade_limiter.acquire()
        try:
            result = await gw.circuit_breaker.call(gw.kis_api.check_order_status, body.order_no)
            if result is None:
                raise HTTPException(502, "KIS order status check failed")
            return OrderStatusResult(**result)
        except CircuitBreakerError as err:
            raise HTTPException(503, "Circuit breaker open") from err
        except KISApiError as e:
            raise HTTPException(502, f"KIS API error: {e}") from e

    # ─── Account ─────────────────────────────────────────────────

    @app.get("/api/balance", response_model=PortfolioState)
    async def api_balance() -> PortfolioState:
        gw = _gw(app.state)
        gw.record_request("balance", "account")
        await gw.trade_limiter.acquire()
        try:
            data = await gw.get_balance_cached()
            positions = [
                Position(
                    stock_code=p["stock_code"],
                    stock_name=p["stock_name"],
                    quantity=p["quantity"],
                    average_buy_price=p["average_buy_price"],
                    total_buy_amount=p["total_buy_amount"],
                    current_price=p.get("current_price"),
                    current_value=p.get("current_value"),
                    profit_pct=p.get("profit_pct"),
                )
                for p in data.get("positions", [])
            ]
            return PortfolioState(
                positions=positions,
                cash_balance=data.get("cash_balance", 0),
                total_asset=data.get("total_asset", 0),
                stock_eval_amount=data.get("stock_eval_amount", 0),
                position_count=len(positions),
                timestamp=datetime.now(UTC),
            )
        except CircuitBreakerError as err:
            raise HTTPException(503, "Circuit breaker open") from err
        except KISApiError as e:
            raise HTTPException(502, f"KIS API error: {e}") from e

    @app.get("/api/cash")
    async def api_cash() -> dict[str, Any]:
        gw = _gw(app.state)
        gw.record_request("cash", "account")
        await gw.trade_limiter.acquire()
        try:
            cash = await gw.get_cash_light()
            return {"cash_balance": cash}
        except CircuitBreakerError as err:
            raise HTTPException(503, "Circuit breaker open") from err
        except KISApiError as e:
            raise HTTPException(502, f"KIS API error: {e}") from e

    @app.get("/api/executions", response_model=list[DailyExecution])
    async def api_daily_executions(
        start_date: str,
        end_date: str,
        stock_code: str = "",
    ) -> list[DailyExecution]:
        """일별 주문체결 조회 — runtime state ↔ KIS drift 진단용.

        start_date / end_date 는 YYYYMMDD (KIS 제한 3개월 이내). stock_code 가
        빈 문자열이면 전 종목, 6자리 종목코드면 해당 종목만.
        """
        gw = _gw(app.state)
        gw.record_request("executions", stock_code or "all")
        try:
            sd = datetime.strptime(start_date, "%Y%m%d").date()
            ed = datetime.strptime(end_date, "%Y%m%d").date()
        except ValueError as e:
            raise HTTPException(422, "start_date/end_date must be YYYYMMDD") from e
        await gw.trade_limiter.acquire()
        try:
            rows = await gw.circuit_breaker.call(
                gw.kis_api.get_daily_executions, sd, ed, stock_code=stock_code
            )
            return [DailyExecution(**r) for r in rows]
        except CircuitBreakerError as err:
            raise HTTPException(503, "Circuit breaker open") from err
        except KISApiError as e:
            raise HTTPException(502, f"KIS API error: {e}") from e

    # ─── Realtime ────────────────────────────────────────────────

    @app.post("/api/realtime/subscribe")
    async def api_realtime_subscribe(body: SubscribeRequest) -> dict[str, Any]:
        gw = _gw(app.state)
        if gw.streamer is None:
            raise HTTPException(503, "Streamer not initialized")

        new_codes = await gw.streamer.add_subscriptions(body.codes)
        if not gw.streamer.is_running:
            await gw.streamer.start(gw.config.base_url)

        return {
            "added": new_codes,
            "total_subscriptions": gw.streamer.subscription_count,
            "is_running": gw.streamer.is_running,
        }

    @app.post("/api/realtime/unsubscribe")
    async def api_realtime_unsubscribe(body: SubscribeRequest) -> dict[str, Any]:
        gw = _gw(app.state)
        if gw.streamer is None:
            raise HTTPException(503, "Streamer not initialized")

        removed = await gw.streamer.remove_subscriptions(body.codes)
        return {
            "removed": removed,
            "total_subscriptions": gw.streamer.subscription_count,
            "is_running": gw.streamer.is_running,
        }

    @app.get("/api/realtime/status")
    async def api_realtime_status() -> dict[str, Any]:
        gw = _gw(app.state)
        if gw.streamer is None:
            return {"is_running": False, "subscription_count": 0, "codes": []}
        return gw.streamer.get_status()

    # ─── Error Handlers ──────────────────────────────────────────

    @app.exception_handler(CircuitBreakerError)
    async def _cb_handler(_request, exc) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": str(exc)})


async def _place_order(app: FastAPI, order: OrderRequest, side: str) -> OrderResult:
    """매수/매도 공통 로직."""
    gw = _gw(app.state)
    gw.record_request(side, order.stock_code)
    price = order.price if order.order_type == "limit" and order.price else 0
    await gw.trade_limiter.acquire()
    try:
        result = await gw.circuit_breaker.call(
            gw.kis_api.place_order,
            order_type=side,
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=price,
        )
        # 주문 직후 잔고가 바뀌므로 캐시를 비워 다음 조회를 새로 받게 한다.
        gw.invalidate_balance_cache()
        return OrderResult(
            success=True,
            order_no=result.get("order_no"),
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=price,
        )
    except CircuitBreakerError as err:
        raise HTTPException(503, "Circuit breaker open") from err
    except KISApiError as e:
        return OrderResult(
            success=False,
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=price,
            message=str(e),
        )
