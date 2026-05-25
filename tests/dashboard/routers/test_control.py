"""Control 라우터 — v3:control.commands stream publish happy path."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

from prime_jennie_runtime.control.audit import record_change
from prime_jennie_runtime.infra.redis_streams import STREAM_CONTROL_COMMANDS


async def _read_one(redis) -> dict:
    """stream 에서 1건 읽어 ControlCommand payload dict 반환."""
    entries = await redis.xrange(STREAM_CONTROL_COMMANDS, min="-", max="+", count=10)
    assert entries, "no entry in control.commands stream"
    _id, fields = entries[-1]
    raw = (
        fields.get(b"payload")
        if isinstance(next(iter(fields.keys())), bytes)
        else fields.get("payload")
    )
    assert raw, f"no payload field in {fields!r}"
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


async def test_stop_publishes_emergency_stop(app, redis_client):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/control/stop",
            json={"reason": "panic button"},
            headers={"X-User": "alice"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "emergency_stop"
        assert body["issued_by"] == "ui:alice"
        assert body["message_id"]

    payload = await _read_one(redis_client)
    assert payload["kind"] == "emergency_stop"
    assert payload["issued_by"] == "ui:alice"
    assert payload["reason"] == "panic button"


async def test_pause_and_resume(app, redis_client):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post("/api/control/pause", json={"reason": "news"})
        r2 = await client.post("/api/control/resume", json={})
        assert r1.status_code == 200 and r1.json()["kind"] == "pause"
        assert r2.status_code == 200 and r2.json()["kind"] == "resume"

    entries = await redis_client.xrange(STREAM_CONTROL_COMMANDS, min="-", max="+", count=10)
    kinds = []
    for _id, fields in entries:
        raw = (
            fields.get(b"payload")
            if isinstance(next(iter(fields.keys())), bytes)
            else fields.get("payload")
        )
        if isinstance(raw, bytes):
            raw = raw.decode()
        kinds.append(json.loads(raw)["kind"])
    assert kinds == ["pause", "resume"]


async def test_dryrun_publishes_payload(app, redis_client):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/control/dryrun", json={"enabled": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["kind"] == "set_dryrun"

    payload = await _read_one(redis_client)
    assert payload["kind"] == "set_dryrun"
    assert payload["payload"] == {"enabled": True}


async def test_liquidate_arm_requires_confirm(app, redis_client):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # arm 없이 confirm 생략 → 400
        resp = await client.post("/api/control/liquidate", json={"action": "arm"})
        assert resp.status_code == 400, resp.text

        # arm + confirm=true → 200 + 발행
        resp2 = await client.post(
            "/api/control/liquidate",
            json={"action": "arm", "confirm": True},
            headers={"X-User": "bob"},
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["kind"] == "liquidate_arm"

        # disarm 은 confirm 불필요
        resp3 = await client.post("/api/control/liquidate", json={"action": "disarm"})
        assert resp3.status_code == 200
        assert resp3.json()["kind"] == "liquidate_disarm"

    payload = await _read_one(redis_client)
    assert payload["kind"] == "liquidate_disarm"


async def test_state_reads_redis_flags(app, redis_client):
    await redis_client.set("control.state:stop", "1")
    await redis_client.set("control.state:dryrun", "1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/control/state")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stop"] is True
        assert body["pause"] is False
        assert body["dryrun"] is True
        assert body["liquidate_armed"] is False


async def test_state_includes_last_change_meta_per_flag(app, redis_client):
    """control:audit:* hash 에 기록된 last_change 가 응답에 그대로 반영."""
    ts = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    await record_change(
        redis_client,
        "stop",
        "auto:slow-loop-hb-expired",
        "slow-loop heartbeat 만료로 STOP 자동 발동",
        timestamp=ts,
    )
    await record_change(
        redis_client,
        "dryrun",
        "ui:alice",
        "장중 점검",
        timestamp=ts,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/control/state")
        body = resp.json()
        lc = body["last_change"]
        assert lc["stop"]["triggered_by"] == "auto:slow-loop-hb-expired"
        assert lc["stop"]["reason"].startswith("slow-loop heartbeat")
        assert lc["stop"]["timestamp"] == "2026-05-24T14:30:00+00:00"
        assert lc["dryrun"]["triggered_by"] == "ui:alice"
        # 기록 없는 flag 는 None
        assert lc["pause"] is None
        assert lc["liquidate_armed"] is None
