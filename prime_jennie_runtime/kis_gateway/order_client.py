"""KIS 주문 전용 클라이언트.

``kis_api.py`` 가 토큰/시세/잔고/주문을 모두 들고 있어 단일 책임이 흐려진다.
주문 (place / cancel / check_status) 만 분리하여:

  - 2단계 체결 조회 (CCLD_DVSN 01 → 00) 의 복잡한 로직을 별도 모듈로 캡슐화
  - 향후 모의/실계좌 tr_id 매핑, 주문 검증, 재시도 정책 등을 한 곳에서 변경 가능
  - ``KISApi`` 는 ``OrderClient`` 를 위임으로 호출하는 thin wrapper

설계 원칙:
  - ``KISApi`` 인스턴스를 주입받아 ``_request`` 와 토큰 매니저를 재사용 (서킷
    브레이커/rate limiter 등은 위 계층에서 이미 적용)
  - 별도 인증/HTTP 클라이언트를 갖지 않는다
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .kis_api import KISApi

logger = logging.getLogger(__name__)


class OrderClient:
    """KIS 주문 API (place / cancel / check_order_status).

    ``KISApi`` 의 _request 를 사용 — 토큰/공통 헤더/재시도/EGW00201/5xx backoff
    모두 상속받는다.
    """

    def __init__(self, kis_api: KISApi) -> None:
        self._kis = kis_api
        self._cfg = kis_api._config  # noqa: SLF001 — 의도된 컴포지션

    # ─── 매수/매도 ───────────────────────────────────────────────

    async def place_order(
        self,
        *,
        order_type: str,
        stock_code: str,
        quantity: int,
        price: int = 0,
    ) -> dict[str, Any]:
        """주문 실행 (매수 TTTC0802U / 매도 TTTC0801U, 모의계좌는 V 접두).

        - 시장가: ``ORD_DVSN="01"``, ``price=0``
        - 지정가: ``ORD_DVSN="00"``, ``price>0``

        Returns: ``{"order_no": str, "order_time": str}``
        """
        tr_id = "TTTC0802U" if order_type == "buy" else "TTTC0801U"
        if self._cfg.is_paper:
            tr_id = "VTTC0802U" if order_type == "buy" else "VTTC0801U"

        ord_dvsn = "01" if price == 0 else "00"

        data = await self._kis._request(  # noqa: SLF001
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            json_data={
                "CANO": self._cfg.account_no,
                "ACNT_PRDT_CD": self._cfg.account_product_code,
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

    # ─── 취소 ────────────────────────────────────────────────────

    async def cancel_order(self, order_no: str) -> bool:
        """주문 취소 (TTTC0803U). 성공 여부를 bool 로 반환."""
        from .kis_api import KISApiError  # 순환 import 회피

        tr_id = "VTTC0803U" if self._cfg.is_paper else "TTTC0803U"

        try:
            await self._kis._request(  # noqa: SLF001
                "POST",
                "/uapi/domestic-stock/v1/trading/order-rvsecncl",
                tr_id=tr_id,
                json_data={
                    "CANO": self._cfg.account_no,
                    "ACNT_PRDT_CD": self._cfg.account_product_code,
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

    # ─── 체결 조회 (2단계) ───────────────────────────────────────

    async def check_order_status(self, order_no: str) -> dict[str, Any] | None:
        """주문 체결 조회 (TTTC0081R).

        레거시 2단계 로직 — KIS 가 한 번에 ``filled_qty + remaining`` 를
        주지 않아 두 번 조회해야 한다:

          Step 1: ``CCLD_DVSN="01"`` (체결만) → ``tot_ccld_qty > 0`` 이면 체결 존재
          Step 2: ``CCLD_DVSN="00"`` (전체)   → ``rmn_qty == 0`` 이면 전량 체결

        반환 ``None`` 은 조회 자체가 실패했음을 의미 (호출자가 retry/timeout 처리).
        체결 0건은 ``{"filled": False, "filled_qty": 0, "avg_price": 0.0}``.
        """
        tr_id = "VTTC0081R" if self._cfg.is_paper else "TTTC0081R"

        today = date.today().strftime("%Y%m%d")
        base_params = {
            "CANO": self._cfg.account_no,
            "ACNT_PRDT_CD": self._cfg.account_product_code,
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
            data1 = await self._kis._request(  # noqa: SLF001
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
            data2 = await self._kis._request(  # noqa: SLF001
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
