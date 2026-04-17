"""KISApi — respx 로 KIS REST 응답 mocking."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from prime_jennie_runtime.kis_gateway.kis_api import KISApi, KISApiError
from prime_jennie_runtime.kis_gateway.token_manager import TokenRecord, save_token


def _token_route(mock: respx.Router, base_url: str, token: str = "tok_new") -> respx.Route:
    return mock.post(f"{base_url}/oauth2/tokenP").mock(
        return_value=httpx.Response(200, json={"access_token": token, "expires_in": 86400})
    )


async def test_authenticate_first_time_fetches_and_saves(kis_config):
    with respx.mock(base_url=kis_config.base_url, assert_all_called=True) as mock:
        _token_route(mock, kis_config.base_url)
        api = KISApi(kis_config)
        try:
            token = await api.authenticate()
        finally:
            await api.close()
    assert token == "tok_new"
    cached = json.loads(Path(kis_config.token_file_path).read_text())
    assert cached["access_token"] == "tok_new"


async def test_authenticate_reuses_cached_token(kis_config):
    """파일에 유효한 토큰이 있으면 /oauth2/tokenP 호출하지 않는다."""
    save_token(
        kis_config.token_file_path,
        TokenRecord(access_token="tok_cached", expires_at=time.time() + 7200),
    )
    with respx.mock(base_url=kis_config.base_url, assert_all_called=False) as mock:
        route = _token_route(mock, kis_config.base_url)
        api = KISApi(kis_config)
        try:
            token = await api.authenticate()
        finally:
            await api.close()
    assert token == "tok_cached"
    assert not route.called


async def test_authenticate_refreshes_near_expiry(kis_config):
    """만료 60초 이내면 강제 갱신."""
    save_token(
        kis_config.token_file_path,
        TokenRecord(access_token="tok_old", expires_at=time.time() + 30),
    )
    with respx.mock(base_url=kis_config.base_url, assert_all_called=True) as mock:
        _token_route(mock, kis_config.base_url, token="tok_fresh")
        api = KISApi(kis_config)
        try:
            token = await api.authenticate()
        finally:
            await api.close()
    assert token == "tok_fresh"


async def test_snapshot_success(kis_config):
    with respx.mock(base_url=kis_config.base_url, assert_all_called=True) as mock:
        _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/quotations/inquire-price").mock(
            return_value=httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "msg_cd": "",
                    "msg1": "",
                    "output": {
                        "stck_prpr": "71200",
                        "stck_oprc": "70500",
                        "stck_hgpr": "71500",
                        "stck_lwpr": "70000",
                        "acml_vol": "1000000",
                        "prdy_ctrt": "1.23",
                        "per": "12.5",
                        "pbr": "1.2",
                    },
                },
            )
        )
        api = KISApi(kis_config)
        try:
            snap = await api.get_snapshot("005930")
        finally:
            await api.close()

    assert snap.stock_code == "005930"
    assert snap.price == 71200
    assert snap.high_price == 71500
    assert snap.volume == 1_000_000
    assert snap.per == 12.5


async def test_request_nonzero_rt_cd_raises(kis_config):
    with respx.mock(base_url=kis_config.base_url, assert_all_called=True) as mock:
        _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/quotations/inquire-price").mock(
            return_value=httpx.Response(
                200,
                json={"rt_cd": "1", "msg_cd": "MCA00001", "msg1": "bad request"},
            )
        )
        api = KISApi(kis_config)
        try:
            with pytest.raises(KISApiError) as excinfo:
                await api.get_snapshot("005930")
        finally:
            await api.close()
    assert excinfo.value.rt_cd == "1"


async def test_auth_error_triggers_token_refresh(kis_config):
    """401 응답 → authenticate(force=True) → 재요청."""
    call_count = {"n": 0}

    def snapshot_side_effect(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(401, json={"rt_cd": "1", "msg1": "unauthorized"})
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "msg1": "",
                "output": {"stck_prpr": "100"},
            },
        )

    with respx.mock(base_url=kis_config.base_url) as mock:
        token_route = _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/quotations/inquire-price").mock(
            side_effect=snapshot_side_effect
        )
        api = KISApi(kis_config)
        try:
            snap = await api.get_snapshot("005930")
        finally:
            await api.close()
    assert snap.price == 100
    # 토큰은 최초 + 강제 갱신 — 총 2회
    assert token_route.call_count >= 2


async def test_place_order_success(kis_config):
    with respx.mock(base_url=kis_config.base_url, assert_all_called=True) as mock:
        _token_route(mock, kis_config.base_url)
        mock.post("/uapi/domestic-stock/v1/trading/order-cash").mock(
            return_value=httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "msg1": "SUCCESS",
                    "output": {"ODNO": "ORD-123", "ORD_TMD": "090001"},
                },
            )
        )
        api = KISApi(kis_config)
        try:
            result = await api.place_order(
                order_type="buy", stock_code="005930", quantity=10, price=0
            )
        finally:
            await api.close()
    assert result["order_no"] == "ORD-123"


async def test_place_order_failure_raises(kis_config):
    with respx.mock(base_url=kis_config.base_url, assert_all_called=True) as mock:
        _token_route(mock, kis_config.base_url)
        mock.post("/uapi/domestic-stock/v1/trading/order-cash").mock(
            return_value=httpx.Response(
                200,
                json={"rt_cd": "1", "msg_cd": "ORDER_REJECT", "msg1": "잔고 부족"},
            )
        )
        api = KISApi(kis_config)
        try:
            with pytest.raises(KISApiError) as excinfo:
                await api.place_order(order_type="buy", stock_code="005930", quantity=10, price=0)
        finally:
            await api.close()
    assert "잔고 부족" in str(excinfo.value)


async def test_check_order_status_two_stage(kis_config):
    """Step 1(체결만) → Step 2(전체) 폴링 흐름."""
    stage = {"count": 0}

    def ccld_side_effect(request: httpx.Request) -> httpx.Response:
        stage["count"] += 1
        if stage["count"] == 1:
            # Step 1: 체결 존재
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "msg1": "",
                    "output1": [{"odno": "ORD-1", "tot_ccld_qty": "5", "avg_prvs": "71000"}],
                },
            )
        # Step 2: 잔여 0
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "msg1": "",
                "output1": [{"odno": "ORD-1", "rmn_qty": "0"}],
            },
        )

    with respx.mock(base_url=kis_config.base_url) as mock:
        _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/trading/inquire-daily-ccld").mock(
            side_effect=ccld_side_effect
        )
        api = KISApi(kis_config)
        try:
            status = await api.check_order_status("ORD-1")
        finally:
            await api.close()
    assert status is not None
    assert status["filled"] is True
    assert status["filled_qty"] == 5


async def test_get_balance_composes_positions(kis_config):
    with respx.mock(base_url=kis_config.base_url) as mock:
        _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/trading/inquire-balance").mock(
            return_value=httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "msg1": "",
                    "output1": [
                        {
                            "pdno": "005930",
                            "prdt_name": "삼성전자",
                            "hldg_qty": "10",
                            "pchs_avg_pric": "70000",
                            "pchs_amt": "700000",
                            "prpr": "71000",
                            "evlu_amt": "710000",
                            "evlu_pfls_rt": "1.43",
                        }
                    ],
                    "output2": [{"prvs_rcdl_excc_amt": "1000000", "scts_evlu_amt": "710000"}],
                },
            )
        )
        mock.get("/uapi/domestic-stock/v1/trading/inquire-psbl-order").mock(
            return_value=httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "msg1": "",
                    "output": {"nrcvb_buy_amt": "1500000", "ord_psbl_cash": "1500000"},
                },
            )
        )
        api = KISApi(kis_config)
        try:
            balance = await api.get_balance()
        finally:
            await api.close()
    assert balance["cash_balance"] == 1_500_000
    assert balance["stock_eval_amount"] == 710_000
    assert balance["total_asset"] == 2_210_000
    assert len(balance["positions"]) == 1
    assert balance["positions"][0]["stock_code"] == "005930"
