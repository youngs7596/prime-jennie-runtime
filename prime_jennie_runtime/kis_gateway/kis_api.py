"""KIS (한국투자증권) REST API 비동기 클라이언트.

실제 KIS OpenAPI와 통신. 토큰 관리, 공통 헤더, 에러 핸들링 포함.
Gateway FastAPI 서버가 이 모듈을 사용하여 KIS API 를 프록시.

원본: prime_jennie/services/gateway/kis_api.py (sync → async 변환)
  - httpx.Client        → httpx.AsyncClient
  - threading.Lock      → asyncio.Lock
  - 모든 메서드 async def
  - 토큰 파일 I/O 는 `token_manager` 모듈로 분리

Reference: https://apiportal.koreainvestment.com/
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime
from typing import Any

import httpx

from prime_jennie_runtime.infra.config import KISConfig

from .order_client import OrderClient
from .schemas import DailyPrice, MinutePrice, StockSnapshot
from .token_manager import TokenRecord, load_token, save_token

logger = logging.getLogger(__name__)

# 5xx 재시도 정책 (EGW00201 throttle 은 별도 1회 backoff 로 처리)
_RETRY_5XX_MAX = 3
_RETRY_5XX_BASE_SEC = 1.0


def _is_egw_throttle(resp: httpx.Response) -> bool:
    """500 + msg_cd=EGW00201 (초당 거래건수 초과) 응답인지 확인.

    이 케이스는 위 ``_request`` 에서 별도 backoff 분기로 이미 처리되므로,
    일반 5xx 재시도 루프가 같은 응답을 다시 재시도하지 않도록 분리.
    """
    if resp.status_code != 500:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return body.get("msg_cd") == "EGW00201"


class KISApiError(Exception):
    """KIS API 오류 (rt_cd != '0' 등)."""

    def __init__(self, message: str, rt_cd: str = "", msg_cd: str = ""):
        super().__init__(message)
        self.rt_cd = rt_cd
        self.msg_cd = msg_cd


class KISApi:
    """KIS OpenAPI 비동기 클라이언트."""

    def __init__(
        self,
        config: KISConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self._config = config
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=30.0,
        )
        self._owns_client = client is None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._load_cached_token()
        # 주문 로직은 별도 모듈 — KISApi 는 thin wrapper 로 위임 (단일 책임 분리)
        self._order_client = OrderClient(self)

    # ─── Authentication ──────────────────────────────────────────

    async def authenticate(self, *, force: bool = False) -> str:
        """접근 토큰 발급/갱신. 캐싱된 유효 토큰이 있으면 재사용.

        만료 60초 전 또는 ``force=True`` 인 경우 재발급.
        """
        async with self._token_lock:
            if not force and self._access_token and time.time() < self._token_expires_at - 60:
                return self._access_token

            resp = await self._client.post(
                "/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self._config.app_key,
                    "appsecret": self._config.app_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            self._access_token = data["access_token"]
            # KIS 토큰은 보통 24시간 유효
            expires_in = int(data.get("expires_in", 86400))
            self._token_expires_at = time.time() + expires_in
            save_token(
                self._config.token_file_path,
                TokenRecord(
                    access_token=self._access_token,
                    expires_at=self._token_expires_at,
                ),
            )

            logger.info("KIS token refreshed, expires in %ds", expires_in)
            return self._access_token

    def _load_cached_token(self) -> None:
        """파일에서 캐싱된 토큰 로드 (생성자에서만 호출, sync)."""
        record = load_token(self._config.token_file_path)
        if record is None:
            return
        if time.time() < record.expires_at - 60:
            self._access_token = record.access_token
            self._token_expires_at = record.expires_at
            logger.debug("Loaded cached KIS token")

    # ─── Common Request ──────────────────────────────────────────

    async def _headers(self, tr_id: str) -> dict[str, str]:
        """KIS API 공통 헤더 구성."""
        token = await self.authenticate()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._config.app_key,
            "appsecret": self._config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    async def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """KIS API 공통 요청.

        재시도 정책:
          - 연결 오류 (RemoteProtocol/Connect/Read): 1회 짧게 재시도
          - 500 EGW00201 (rate throttle): 1초 backoff 후 1회 재시도
          - 503 및 일반 5xx (서버 일시 장애): 지수 백오프 3회 (1s/2s/4s)
          - 401/403 (인증 오류): 토큰 재발급 후 1회 재시도

        Circuit breaker 와의 관계: ``_request`` 는 ``AsyncCircuitBreaker.call`` 안에서
        실행되므로 breaker 가 OPEN 이면 ``_request`` 가 호출되기 전 ``CircuitBreakerError``
        가 발생한다. 즉 여기서의 재시도는 breaker 가 CLOSED/HALF_OPEN 일 때만 일어나며,
        실패 누적이 ``fail_max`` 를 넘으면 breaker 가 OPEN 으로 전이해 후속 호출이
        즉시 차단된다 — retry 와 breaker 가 충돌하지 않음.
        """
        headers = await self._headers(tr_id)

        try:
            resp = await self._client.request(
                method, path, headers=headers, params=params, json=json_data
            )
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as e:
            stock = (params or {}).get("FID_INPUT_ISCD", "")
            logger.warning("KIS %s %s [%s] connection error: %s, retrying", method, path, stock, e)
            await asyncio.sleep(0.5)
            resp = await self._client.request(
                method, path, headers=headers, params=params, json=json_data
            )

        # 500 throttling (EGW00201 초당 거래건수 초과): 짧게 backoff 후 재시도, 재인증 금지.
        # Paper 는 초당 2건 제한이라 쉽게 걸림.
        if resp.status_code == 500:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if body.get("msg_cd") == "EGW00201":
                logger.warning("KIS %s %s -> 500 EGW00201 throttle, backoff 1s", method, path)
                await asyncio.sleep(1.0)
                resp = await self._client.request(
                    method, path, headers=headers, params=params, json=json_data
                )

        # 5xx (502/503/504 + EGW00201 이 아닌 500): 일시 장애로 간주하고 지수 백오프 재시도.
        # 2026-05-11 운영 중 503 (Service Unavailable) 다발 — KIS 점검 또는 자체 게이트웨이
        # 일시 과부하 — 단발 재시도로 회복되는 경우 대부분.
        if 500 <= resp.status_code < 600 and not _is_egw_throttle(resp):
            for attempt in range(_RETRY_5XX_MAX):
                delay = _RETRY_5XX_BASE_SEC * (2**attempt)
                logger.warning(
                    "KIS %s %s -> %d, backoff %.1fs (attempt %d/%d)",
                    method,
                    path,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    _RETRY_5XX_MAX,
                )
                await asyncio.sleep(delay)
                resp = await self._client.request(
                    method, path, headers=headers, params=params, json=json_data
                )
                if resp.status_code < 500 or _is_egw_throttle(resp):
                    break

        # 인증 오류 의심 → 토큰 재발급 후 재시도 (1회).
        # 401/403 만 대상. 500 은 throttle/서버 오류라 재인증이 도움 안 됨 (오히려
        # 토큰 1분/회 발급 제한 연쇄 실패).
        if resp.status_code in (401, 403):
            logger.warning(
                "KIS %s %s -> %d, retrying with fresh token",
                method,
                path,
                resp.status_code,
            )
            await self.authenticate(force=True)
            headers = await self._headers(tr_id)
            resp = await self._client.request(
                method, path, headers=headers, params=params, json=json_data
            )

        resp.raise_for_status()
        data = resp.json()

        rt_cd = data.get("rt_cd", "")
        if rt_cd != "0":
            msg = data.get("msg1", "Unknown KIS error")
            raise KISApiError(msg, rt_cd=rt_cd, msg_cd=data.get("msg_cd", ""))

        return data

    # ─── Market Data ─────────────────────────────────────────────

    async def get_snapshot(self, stock_code: str) -> StockSnapshot:
        """현재가 조회 (FHKST01010100)."""
        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
            },
        )
        output = data.get("output", {})

        return StockSnapshot(
            stock_code=stock_code,
            price=int(output.get("stck_prpr", 0)),
            open_price=int(output.get("stck_oprc", 0)),
            high_price=int(output.get("stck_hgpr", 0)),
            low_price=int(output.get("stck_lwpr", 0)),
            volume=int(output.get("acml_vol", 0)),
            change_pct=float(output.get("prdy_ctrt", 0)),
            per=_safe_float(output.get("per")),
            pbr=_safe_float(output.get("pbr")),
            market_cap=_safe_int(output.get("hts_avls")),
            high_52w=_safe_int(output.get("stck_dryy_hgpr")),
            low_52w=_safe_int(output.get("stck_dryy_lwpr")),
            timestamp=datetime.now(UTC),
        )

    async def get_daily_prices(self, stock_code: str, days: int = 150) -> list[DailyPrice]:
        """일봉 조회 (FHKST01010400)."""
        end_date = date.today().strftime("%Y%m%d")
        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            tr_id="FHKST01010400",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
                "FID_INPUT_DATE_1": "",
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )

        prices: list[DailyPrice] = []
        for row in data.get("output", [])[:days]:
            try:
                prices.append(
                    DailyPrice(
                        stock_code=stock_code,
                        price_date=datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date(),
                        open_price=int(row.get("stck_oprc", 0)),
                        high_price=int(row.get("stck_hgpr", 0)),
                        low_price=int(row.get("stck_lwpr", 0)),
                        close_price=int(row.get("stck_clpr", 0)),
                        volume=int(row.get("acml_vol", 0)),
                        change_pct=_safe_float(row.get("prdy_ctrt")),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed daily price row: %s", e)
                continue

        return prices

    async def get_minute_prices(self, stock_code: str) -> list[MinutePrice]:
        """분봉 조회 (FHKST03010200)."""
        now = datetime.now()
        time_str = now.strftime("%H%M%S")

        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            tr_id="FHKST03010200",
            params={
                "FID_ETC_CLS_CODE": "",
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
                "FID_INPUT_HOUR_1": time_str,
                "FID_PW_DATA_INCU_YN": "N",
            },
        )

        prices: list[MinutePrice] = []
        for row in data.get("output2", []):
            try:
                dt_str = row.get("stck_bsop_date", now.strftime("%Y%m%d"))
                tm_str = row.get("stck_cntg_hour", "000000")
                price_dt = datetime.strptime(f"{dt_str}{tm_str}", "%Y%m%d%H%M%S")

                prices.append(
                    MinutePrice(
                        stock_code=stock_code,
                        price_datetime=price_dt,
                        open_price=int(row.get("stck_oprc", 0)),
                        high_price=int(row.get("stck_hgpr", 0)),
                        low_price=int(row.get("stck_lwpr", 0)),
                        close_price=int(row.get("stck_prpr", 0)),
                        volume=int(row.get("cntg_vol", 0)),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed minute price row: %s", e)
                continue

        return prices

    # ─── Trading (delegated to OrderClient) ──────────────────────

    async def place_order(
        self,
        *,
        order_type: str,
        stock_code: str,
        quantity: int,
        price: int = 0,
    ) -> dict[str, Any]:
        """``OrderClient.place_order`` 위임 — 주문 로직은 별도 모듈."""
        return await self._order_client.place_order(
            order_type=order_type,
            stock_code=stock_code,
            quantity=quantity,
            price=price,
        )

    async def cancel_order(self, order_no: str) -> bool:
        """``OrderClient.cancel_order`` 위임."""
        return await self._order_client.cancel_order(order_no)

    async def check_order_status(self, order_no: str) -> dict[str, Any] | None:
        """``OrderClient.check_order_status`` 위임 — 2단계 체결 조회."""
        return await self._order_client.check_order_status(order_no)

    # ─── Account ─────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """잔고 조회 (TTTC8434R)."""
        tr_id = "VTTC8434R" if self._config.is_paper else "TTTC8434R"

        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params={
                "CANO": self._config.account_no,
                "ACNT_PRDT_CD": self._config.account_product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )

        positions: list[dict[str, Any]] = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty <= 0:
                continue
            positions.append(
                {
                    "stock_code": item.get("pdno", ""),
                    "stock_name": item.get("prdt_name", ""),
                    "quantity": qty,
                    "average_buy_price": int(float(item.get("pchs_avg_pric", 0))),
                    "total_buy_amount": int(item.get("pchs_amt", 0)),
                    "current_price": int(item.get("prpr", 0)),
                    "current_value": int(item.get("evlu_amt", 0)),
                    "profit_pct": float(item.get("evlu_pfls_rt", 0)),
                }
            )

        output2 = data.get("output2", [{}])
        summary = output2[0] if output2 else {}

        # 매수가능금액 조회 (TTTC8908R) — 실제 주문 가능한 정확한 금액
        try:
            cash_balance = await self.get_buying_power()
        except Exception:
            logger.warning("Buying power API failed, falling back to prvs_rcdl_excc_amt")
            cash_balance = int(summary.get("prvs_rcdl_excc_amt", 0))

        stock_eval = int(summary.get("scts_evlu_amt", 0))

        return {
            "positions": positions,
            "cash_balance": cash_balance,
            "total_asset": cash_balance + stock_eval,
            "stock_eval_amount": stock_eval,
        }

    async def get_buying_power(self) -> int:
        """매수가능금액 조회 (TTTC8908R)."""
        tr_id = "VTTC8908R" if self._config.is_paper else "TTTC8908R"

        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=tr_id,
            params={
                "CANO": self._config.account_no,
                "ACNT_PRDT_CD": self._config.account_product_code,
                "PDNO": "005930",
                "ORD_UNPR": "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "Y",
                "OVRS_ICLD_YN": "N",
            },
        )
        output = data.get("output", {})
        nrcvb = output.get("nrcvb_buy_amt", "")
        if nrcvb and nrcvb.strip():
            return int(nrcvb)
        ord_cash = output.get("ord_psbl_cash", "")
        if ord_cash and ord_cash.strip():
            return int(ord_cash)
        return 0

    async def get_daily_executions(
        self,
        start_date: date,
        end_date: date,
        *,
        stock_code: str = "",
    ) -> list[dict[str, Any]]:
        """일별 주문체결 조회 (TTTC8001R / VTTC8001R).

        ``start_date``~``end_date`` (KIS 제한 3개월 이내) 의 체결내역. ``stock_code``
        가 빈 문자열이면 전 종목. runtime state ↔ KIS drift 진단용 — reconcile 의
        qty_mismatch 가 떴을 때 해당 ticker 의 실제 KIS 체결을 대조한다.

        단일 페이지만 조회한다. 결과가 한 페이지를 넘으면 (ctx_area_nk100 비어있지
        않음) WARN 로깅 후 첫 페이지만 반환 — 종목 1개 진단 (주 용도) 은 거의 항상
        단일 페이지이며, 전 종목 장기 조회가 필요하면 기간을 좁혀 재호출한다.
        """
        tr_id = "VTTC8001R" if self._config.is_paper else "TTTC8001R"
        data = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id=tr_id,
            params={
                "CANO": self._config.account_no,
                "ACNT_PRDT_CD": self._config.account_product_code,
                "INQR_STRT_DT": start_date.strftime("%Y%m%d"),
                "INQR_END_DT": end_date.strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",  # 전체 (매수+매도)
                "INQR_DVSN": "01",  # 정순
                "PDNO": stock_code,
                "CCLD_DVSN": "00",  # 전체 (체결+미체결)
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        if (data.get("ctx_area_nk100") or "").strip():
            logger.warning(
                "get_daily_executions: 결과가 1페이지 초과 — 첫 페이지만 반환 "
                "(조회 기간을 좁혀 재호출 권장)"
            )

        executions: list[dict[str, Any]] = []
        for item in data.get("output1", []) or []:
            executions.append(
                {
                    "order_date": item.get("ord_dt", ""),
                    "order_time": item.get("ord_tmd", ""),
                    "stock_code": item.get("pdno", ""),
                    "stock_name": item.get("prdt_name", ""),
                    "side": "sell" if item.get("sll_buy_dvsn_cd") == "01" else "buy",
                    "order_qty": int(item.get("ord_qty", 0) or 0),
                    "filled_qty": int(item.get("tot_ccld_qty", 0) or 0),
                    "avg_price": _safe_float(item.get("avg_prvs")) or 0.0,
                    "filled_amount": int(item.get("tot_ccld_amt", 0) or 0),
                    "order_no": item.get("odno", ""),
                    "cancelled": (item.get("cncl_yn", "") or "").strip() == "Y",
                }
            )
        return executions

    async def search_info(self, stock_code: str) -> dict[str, Any] | None:
        """종목 기본 정보 조회 (CTPF1002R).

        반환 dict 의 핵심 필드:
          - ``scty_grp_id_cd``: ST/EF/EN/EW (주식/ETF/ETN/ELW)
          - ``mket_id_cd``: 시장 (STK/KSQ 등)
          - ``prdt_name``: 정식 종목명 (예: '삼성KODEX레버리지증권상장지수투자신탁')

        실패 시 None — 호출자가 'STOCK' default 로 fallback.
        """
        try:
            data = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/search-info",
                tr_id="CTPF1002R",
                params={"PDNO": stock_code, "PRDT_TYPE_CD": "300"},
            )
        except Exception:
            logger.warning("search_info failed for %s", stock_code, exc_info=True)
            return None
        output = data.get("output")
        if not output:
            return None
        return output

    async def is_trading_day(self, target_date: date | None = None) -> bool:
        """거래일 여부 확인 (CTCA0903R)."""
        target = target_date or date.today()
        try:
            data = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/chk-holiday",
                tr_id="CTCA0903R",
                params={
                    "BASS_DT": target.strftime("%Y%m%d"),
                    "CTX_AREA_NK": "",
                    "CTX_AREA_FK": "",
                },
            )
            for item in data.get("output", []):
                if item.get("bass_dt") == target.strftime("%Y%m%d"):
                    return item.get("opnd_yn", "N") == "Y"
            return True  # 기본적으로 영업일로 가정
        except Exception:
            # API 실패 시 주말만 체크
            return target.weekday() < 5

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# ─── Helpers ─────────────────────────────────────────────────────


def _safe_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None
