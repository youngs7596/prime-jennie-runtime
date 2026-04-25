"""System 라우터 happy path — 서비스 헬스 체크 (respx mock)."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import ASGITransport, AsyncClient, ConnectError, Response


@pytest.fixture
def targets_env(monkeypatch):
    monkeypatch.setenv(
        "DASHBOARD_HEALTH_TARGETS",
        json.dumps(
            [
                {"name": "kis-gateway", "url": "http://kis-gateway/health"},
                {"name": "slow-loop", "url": "http://slow-loop/health"},
            ]
        ),
    )
    # daemon heartbeat 체크를 끔 (별도 테스트에서 커버).
    monkeypatch.setenv("DASHBOARD_DAEMONS", "")


async def test_health_mixed(app, targets_env):
    """하나는 healthy, 하나는 unreachable."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://kis-gateway/health").mock(
            return_value=Response(
                200,
                json={"status": "healthy", "version": "0.1.0"},
                headers={"content-type": "application/json"},
            )
        )
        mock.get("http://slow-loop/health").mock(side_effect=ConnectError("nope"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/system/health")
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            assert len(rows) == 2
            by_name = {r["name"]: r for r in rows}
            assert by_name["kis-gateway"]["status"] == "healthy"
            assert by_name["slow-loop"]["status"] == "unreachable"


async def test_health_normalizes_ok_to_healthy(app, targets_env):
    """서비스가 `{"status":"ok"}` 또는 `{"status":"alive"}` 처럼 응답해도 백엔드가
    "healthy" 로 정규화 — UI 의 "N/M healthy" 카운트가 ok-라벨 서비스를 빼먹지
    않도록.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://kis-gateway/health").mock(
            return_value=Response(
                200,
                json={"status": "ok"},
                headers={"content-type": "application/json"},
            )
        )
        mock.get("http://slow-loop/health").mock(
            return_value=Response(
                200,
                json={"status": "alive"},
                headers={"content-type": "application/json"},
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/system/health")
            rows = resp.json()
            by_name = {r["name"]: r["status"] for r in rows}
            assert by_name["kis-gateway"] == "healthy"
            assert by_name["slow-loop"] == "healthy"
