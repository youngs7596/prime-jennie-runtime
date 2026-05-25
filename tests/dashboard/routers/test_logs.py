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
            # BACKWARD 방향이므로 newest-first 로 정렬되어야 함
            assert logs[0]["message"] == "scout run done"
            assert logs[1]["message"] == "scout run started"


async def test_stream_merges_and_sorts_multiple_streams(app):
    """Loki 가 label set 별로 여러 stream 을 돌려줄 때 (예: stdout/stderr 분리)
    timestamp 기준 전역 정렬되어야 한다."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/loki/api/v1/query_range.*").mock(
            return_value=Response(
                200,
                json={
                    "data": {
                        "result": [
                            {
                                "stream": {"service": "kis-gateway", "stream": "stderr"},
                                "values": [
                                    ["1700000003000000000", "ERROR line C"],
                                    ["1700000001000000000", "ERROR line A"],
                                ],
                            },
                            {
                                "stream": {"service": "kis-gateway", "stream": "stdout"},
                                "values": [
                                    ["1700000004000000000", "INFO line D"],
                                    ["1700000002000000000", "INFO line B"],
                                ],
                            },
                        ]
                    }
                },
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/logs/stream", params={"service": "kis-gateway", "limit": 10}
            )
            assert resp.status_code == 200, resp.text
            logs = resp.json()["logs"]
            # 전역 timestamp desc 정렬: D(4) > C(3) > B(2) > A(1)
            assert [e["message"] for e in logs] == [
                "INFO line D",
                "ERROR line C",
                "INFO line B",
                "ERROR line A",
            ]


async def test_stream_level_and_query_filters_are_applied(app):
    """level (multi) + q 가 LogQL `|~` 로 결합되어 query_range 에 실린다."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*/loki/api/v1/query_range.*").mock(
            return_value=Response(200, json={"data": {"result": []}})
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/logs/stream",
                params=[
                    ("service", "kis-gateway"),
                    ("level", "ERROR"),
                    ("level", "WARN"),
                    ("q", "timeout"),
                ],
            )
            assert resp.status_code == 200, resp.text
        called_query = route.calls.last.request.url.params.get("query", "")
        assert '{service="kis-gateway"}' in called_query
        assert '|~ "ERROR|WARN"' in called_query
        assert '|~ "timeout"' in called_query


async def test_stream_invalid_level_filtered_out(app):
    """whitelist (INFO/WARN/ERROR/DEBUG) 외의 값은 조용히 무시."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*/loki/api/v1/query_range.*").mock(
            return_value=Response(200, json={"data": {"result": []}})
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/logs/stream",
                params=[("service", "kis-gateway"), ("level", "FOO")],
            )
            assert resp.status_code == 200
        called_query = route.calls.last.request.url.params.get("query", "")
        assert "|~" not in called_query


async def test_stream_quote_in_q_is_escaped(app):
    """q 의 " 와 \\ 가 LogQL 안에서 안전하게 escape 되어 injection 차단."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*/loki/api/v1/query_range.*").mock(
            return_value=Response(200, json={"data": {"result": []}})
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/logs/stream",
                params={"service": "kis-gateway", "q": 'foo " bar \\baz'},
            )
            assert resp.status_code == 200
        called_query = route.calls.last.request.url.params.get("query", "")
        assert '|~ "foo \\" bar \\\\baz"' in called_query


async def test_stream_applies_limit_after_merge(app):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/loki/api/v1/query_range.*").mock(
            return_value=Response(
                200,
                json={
                    "data": {
                        "result": [
                            {
                                "stream": {"s": "a"},
                                "values": [
                                    ["1700000005000000000", "a5"],
                                    ["1700000003000000000", "a3"],
                                    ["1700000001000000000", "a1"],
                                ],
                            },
                            {
                                "stream": {"s": "b"},
                                "values": [
                                    ["1700000006000000000", "b6"],
                                    ["1700000004000000000", "b4"],
                                    ["1700000002000000000", "b2"],
                                ],
                            },
                        ]
                    }
                },
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/logs/stream", params={"service": "kis-gateway", "limit": 3}
            )
            assert resp.status_code == 200, resp.text
            logs = resp.json()["logs"]
            assert [e["message"] for e in logs] == ["b6", "a5", "b4"]
