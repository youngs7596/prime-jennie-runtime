"""Logs 라우터 happy path — Loki 프록시 (respx mock)."""

from __future__ import annotations

import respx
from httpx import ASGITransport, AsyncClient, Response


async def test_services_list(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/logs/services")
        assert resp.status_code == 200
        assert "kis-gateway" in resp.json()["services"]


async def test_stream_proxy(app):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/loki/api/v1/query_range.*").mock(
            return_value=Response(
                200,
                json={
                    "data": {
                        "result": [
                            {
                                "stream": {"app": "slow-loop"},
                                "values": [
                                    ["1700000000000000000", "scout run started"],
                                    ["1700000000100000000", "scout run done"],
                                ],
                            }
                        ]
                    }
                },
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/logs/stream", params={"service": "slow-loop"})
            assert resp.status_code == 200, resp.text
            logs = resp.json()["logs"]
            assert len(logs) == 2
            assert logs[0]["message"] == "scout run started"
