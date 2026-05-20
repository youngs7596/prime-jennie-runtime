"""KISApi — respx 로 KIS REST 응답 mocking."""

from __future__ import annotations

import json
import time
from datetime import date
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


async def test_daily_executions_parses_output1(kis_config):
    """inquire-daily-ccld output1 → 정규화된 체결 dict 리스트."""
    with respx.mock(base_url=kis_config.base_url, assert_all_called=True) as mock:
        _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/trading/inquire-daily-ccld").mock(
            return_value=httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "msg_cd": "",
                    "msg1": "",
                    "ctx_area_fk100": "",
                    "ctx_area_nk100": "",
                    "output1": [
                        {
                            "ord_dt": "20260514",
                            "ord_tmd": "103114",
                            "pdno": "241560",
                            "prdt_name": "두산밥캣",
                            "sll_buy_dvsn_cd": "02",
                            "sll_buy_dvsn_cd_name": "현금매수",
                            "ord_qty": "113",
                            "tot_ccld_qty": "113",
                            "avg_prvs": "69076",
                            "tot_ccld_amt": "7805700",
                            "odno": "0018495800",
                            "cncl_yn": "",
                        },
                        {
                            "ord_dt": "20260518",
                            "ord_tmd": "091520",
                            "pdno": "241560",
                            "prdt_name": "두산밥캣",
                            "sll_buy_dvsn_cd": "01",
                            "sll_buy_dvsn_cd_name": "현금매도",
                            "ord_qty": "72",
                            "tot_ccld_qty": "72",
                            "avg_prvs": "65500",
                            "tot_ccld_amt": "4716000",
                            "odno": "0008991900",
                            "cncl_yn": "",
                        },
                    ],
                    "output2": {},
                },
            )
        )
        api = KISApi(kis_config)
        try:
            rows = await api.get_daily_executions(
                date(2026, 5, 1), date(2026, 5, 20), stock_code="241560"
            )
        finally:
            await api.close()

    assert len(rows) == 2
    assert rows[0]["side"] == "buy"
    assert rows[0]["filled_qty"] == 113
    assert rows[0]["avg_price"] == 69076.0
    assert rows[0]["order_no"] == "0018495800"
    assert rows[0]["cancelled"] is False
    assert rows[1]["side"] == "sell"
    assert rows[1]["filled_qty"] == 72


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


async def test_503_retries_with_backoff_then_succeeds(kis_config, monkeypatch):
    """503 응답 → 지수 백오프 재시도 → 성공 회복."""
    calls = {"n": 0}

    def snapshot_side_effect(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, json={"rt_cd": "1", "msg1": "Service Unavailable"})
        return httpx.Response(
            200,
            json={"rt_cd": "0", "msg1": "", "output": {"stck_prpr": "71000"}},
        )

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("prime_jennie_runtime.kis_gateway.kis_api.asyncio.sleep", fake_sleep)

    with respx.mock(base_url=kis_config.base_url) as mock:
        _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/quotations/inquire-price").mock(
            side_effect=snapshot_side_effect
        )
        api = KISApi(kis_config)
        try:
            snap = await api.get_snapshot("005930")
        finally:
            await api.close()

    assert snap.price == 71000
    # 3회 호출 (1차 503, 2차 503, 3차 200)
    assert calls["n"] == 3
    # backoff sleep 2회: 1.0s, 2.0s (성공 시 break)
    backoffs = [s for s in sleeps if s >= 1.0]
    assert backoffs == [1.0, 2.0]


async def test_503_gives_up_after_max_retries(kis_config, monkeypatch):
    """5xx 가 계속되면 마지막 응답으로 raise_for_status 가 HTTPStatusError 발생."""
    calls = {"n": 0}

    def snapshot_side_effect(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"rt_cd": "1", "msg1": "down"})

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("prime_jennie_runtime.kis_gateway.kis_api.asyncio.sleep", fake_sleep)

    with respx.mock(base_url=kis_config.base_url) as mock:
        _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/quotations/inquire-price").mock(
            side_effect=snapshot_side_effect
        )
        api = KISApi(kis_config)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await api.get_snapshot("005930")
        finally:
            await api.close()

    # 최초 1회 + 재시도 3회 = 4회
    assert calls["n"] == 4


async def test_egw00201_not_re_retried_by_5xx_loop(kis_config, monkeypatch):
    """EGW00201 은 전용 분기에서 1회 backoff 후 처리 — 일반 5xx 루프가 또 retry 하지 않음."""
    calls = {"n": 0}

    def snapshot_side_effect(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                500, json={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "throttle"}
            )
        return httpx.Response(
            200, json={"rt_cd": "0", "msg1": "", "output": {"stck_prpr": "70000"}}
        )

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("prime_jennie_runtime.kis_gateway.kis_api.asyncio.sleep", fake_sleep)

    with respx.mock(base_url=kis_config.base_url) as mock:
        _token_route(mock, kis_config.base_url)
        mock.get("/uapi/domestic-stock/v1/quotations/inquire-price").mock(
            side_effect=snapshot_side_effect
        )
        api = KISApi(kis_config)
        try:
            snap = await api.get_snapshot("005930")
        finally:
            await api.close()

    assert snap.price == 70000
    # 정확히 2회 호출 — EGW00201 backoff 후 재시도 1회만
    assert calls["n"] == 2


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
