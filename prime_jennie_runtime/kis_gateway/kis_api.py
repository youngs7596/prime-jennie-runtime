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

from .schemas import DailyPrice, MinutePrice, StockSnapshot
from .token_manager import TokenRecord, load_token, save_token

logger = logging.getLogger(__name__)


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
        """KIS API 공통 요청. 연결 오류 1회 재시도, 인증 오류 시 토큰 재발급 후 재시도."""
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

    # ─── Trading ─────────────────────────────────────────────────

    async def place_order(
        self,
        *,
        order_type: str,
        stock_code: str,
        quantity: int,
        price: int = 0,
    ) -> dict[str, Any]:
        """주문 실행 (매수 TTTC0802U / 매도 TTTC0801U, 모의계좌는 V 접두)."""
        tr_id = "TTTC0802U" if order_type == "buy" else "TTTC0801U"
        if self._config.is_paper:
            tr_id = "VTTC0802U" if order_type == "buy" else "VTTC0801U"

        # 시장가: ORD_DVSN=01, 지정가: ORD_DVSN=00
        ord_dvsn = "01" if price == 0 else "00"

        data = await self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            json_data={
                "CANO": self._config.account_no,
                "ACNT_PRDT_CD": self._config.account_product_code,
                "PDNO": stock_code,
                "ORD_DVSN": ord_dvsn,
                "ORD_QTY": str(quantity),
                "ORD_UNPR": str(price),
            },
        )

        output = data.get("output", {})
        return {
            "order_no": output.get("ODNO", ""),
            "order_time": output.get("ORD_TMD", ""),
        }

    async def cancel_order(self, order_no: str) -> bool:
        """주문 취소 (TTTC0803U)."""
        tr_id = "VTTC0803U" if self._config.is_paper else "TTTC0803U"

        try:
            await self._request(
                "POST",
                "/uapi/domestic-stock/v1/trading/order-rvsecncl",
                tr_id=tr_id,
                json_data={
                    "CANO": self._config.account_no,
                    "ACNT_PRDT_CD": self._config.account_product_code,
                    "KRX_FWDG_ORD_ORGNO": "",
                    "ORGN_ODNO": order_no,
                    "ORD_DVSN": "00",
                    "RVSE_CNCL_DVSN_CD": "02",  # 취소
                    "ORD_QTY": "0",
                    "ORD_UNPR": "0",
                    "QTY_ALL_ORD_YN": "Y",
                },
            )
            return True
        except KISApiError as e:
            logger.error("Cancel order failed: %s", e)
            return False

    async def check_order_status(self, order_no: str) -> dict[str, Any] | None:
        """주문 체결 조회 (TTTC0081R).

        레거시 2단계 로직:
          Step 1: CCLD_DVSN="01" (체결만) → tot_ccld_qty > 0 이면 체결 존재
          Step 2: CCLD_DVSN="00" (전체) → rmn_qty == 0 이면 전량 체결
        """
        tr_id = "VTTC0081R" if self._config.is_paper else "TTTC0081R"

        today = date.today().strftime("%Y%m%d")
        base_params = {
            "CANO": self._config.account_no,
            "ACNT_PRDT_CD": self._config.account_product_code,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",  # 전체
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "",
            "ORD_GNO_BRNO": "",
            "ODNO": order_no,
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        try:
            # Step 1: 체결 건만 조회
            params_filled = {**base_params, "CCLD_DVSN": "01"}
            data1 = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=tr_id,
                params=params_filled,
            )

            filled_qty = 0
            avg_price = 0.0
            for row in data1.get("output1", []):
                if row.get("odno") == order_no:
                    filled_qty += int(row.get("tot_ccld_qty", 0))
                    price_val = float(row.get("avg_prvs", 0))
                    if price_val > 0:
                        avg_price = price_val

            if filled_qty == 0:
                return {"filled": False, "filled_qty": 0, "avg_price": 0.0}

            # Step 2: 전체 조회로 잔여 수량 확인
            params_all = {**base_params, "CCLD_DVSN": "00"}
            data2 = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=tr_id,
                params=params_all,
            )

            fully_filled = False
            for row in data2.get("output1", []):
                if row.get("odno") == order_no:
                    rmn_qty = int(row.get("rmn_qty", -1))
                    if rmn_qty == 0:
                        fully_filled = True
                    break

            return {
                "filled": fully_filled,
                "filled_qty": filled_qty,
                "avg_price": avg_price,
            }

        except Exception as e:
            logger.error("check_order_status failed for %s: %s", order_no, e)
            return None

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
