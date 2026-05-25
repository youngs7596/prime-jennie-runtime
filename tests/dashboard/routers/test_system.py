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


async def test_health_includes_port_from_url(app, monkeypatch):
    """HTTP target URL 의 명시 포트가 응답의 port 필드로 노출된다."""
    monkeypatch.setenv(
        "DASHBOARD_HEALTH_TARGETS",
        json.dumps(
            [
                {"name": "kis-gateway", "url": "http://kis-gateway:8080/health"},
                {"name": "control-ui", "url": "http://control-ui:80/"},
                {"name": "no-port", "url": "http://no-port/health"},
            ]
        ),
    )
    monkeypatch.setenv("DASHBOARD_DAEMONS", "")
    with respx.mock(assert_all_called=False) as mock:
        for name in ("kis-gateway", "control-ui", "no-port"):
            mock.get(url__regex=rf".*//{name}.*").mock(
                return_value=Response(
                    200,
                    json={"status": "ok"},
                    headers={"content-type": "application/json"},
                )
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/system/health")
            rows = resp.json()
            by_name = {r["name"]: r for r in rows}
            assert by_name["kis-gateway"]["port"] == 8080
            assert by_name["control-ui"]["port"] == 80
            # urllib 는 명시 포트 없으면 None — 80/443 같은 scheme default 도 None
            assert by_name["no-port"]["port"] is None


def test_heartbeat_level_four_stages():
    """heartbeat_status_level 단계가 핸드오프 §3.3 임계와 일치."""
    from prime_jennie_runtime.dashboard.routers.system import _heartbeat_level

    assert _heartbeat_level(None) is None
    assert _heartbeat_level(0.5) == "healthy"
    assert _heartbeat_level(29.9) == "healthy"
    assert _heartbeat_level(30) == "attention"
    assert _heartbeat_level(59.9) == "attention"
    assert _heartbeat_level(60) == "warn"
    assert _heartbeat_level(119.9) == "warn"
    assert _heartbeat_level(120) == "danger"
    assert _heartbeat_level(3600) == "danger"


def test_extract_port_only_for_http():
    """http(s) URL 만 포트 파싱, redis:// / docker:// 는 None."""
    from prime_jennie_runtime.dashboard.routers.system import _extract_port

    assert _extract_port("http://kis-gateway:8080/health") == 8080
    assert _extract_port("https://api.example.com:443/v1") == 443
    assert _extract_port("http://control-ui:80/") == 80
    assert _extract_port("http://no-port/health") is None
    assert _extract_port("redis://heartbeat:slow-loop") is None
    assert _extract_port("docker://prime-jennie-runtime-fast-loop-1") is None


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
