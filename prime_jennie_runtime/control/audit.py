"""Control flag 변경 audit hash 누적.

state SET 직후 호출 — UI / telegram / 자동 발동 (heartbeat 만료 → STOP 등) 모두
같은 헬퍼를 통해 같은 형식으로 기록한다. dashboard /api/control/state 응답이 이
hash 들을 읽어 last_change 메타로 노출하고, control-ui System 페이지의 Control
Flag 카드 audit 메타 + auto-triggered 표시에 사용한다.

각 flag 당 hash 한 칸 (timestamp, triggered_by, reason). 이력을 누적하지 않고
"마지막 변경" 만 노출 — 운영자가 카드 위에서 즉시 컨텍스트를 잡기 위한 자리지
변경 이력 audit log 가 따로 필요하면 별도 stream / DB 로 가야 한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

AUDIT_KEY_PREFIX = "control:audit"

# 응답에 노출하는 canonical flag 이름. STATE_KEY_* 와 1:1 대응.
AUDIT_FLAGS = ("stop", "pause", "dryrun", "liquidate_armed")


def _audit_key(flag: str) -> str:
    return f"{AUDIT_KEY_PREFIX}:{flag}"


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


async def record_change(
    redis_client: aioredis.Redis,
    flag: str,
    triggered_by: str,
    reason: str | None,
    timestamp: datetime | None = None,
) -> None:
    """flag 변경 1건을 Redis hash 에 SET (덮어쓰기).

    record_llm_call 과 같은 정신: 실패해도 swallow 하지 않고 호출자가 다루도록
    raise — control state 자체는 이미 변경됐는데 audit 만 놓치는 자리가 운영
    가시성을 무너뜨리므로 호출자가 인지해야 한다.
    """
    ts = (timestamp or datetime.now(UTC)).isoformat()
    await redis_client.hset(
        _audit_key(flag),
        mapping={
            "timestamp": ts,
            "triggered_by": triggered_by,
            "reason": reason or "",
        },
    )


async def read_change(redis_client: aioredis.Redis, flag: str) -> dict[str, str | None] | None:
    """audit hash 읽기. decode_responses 모드와 무관하게 str 로 반환."""
    data = await redis_client.hgetall(_audit_key(flag))
    if not data:
        return None
    # bytes 또는 str key 어느 쪽이든 처리.
    out: dict[str, str | None] = {}
    for raw_k, raw_v in data.items():
        k = _decode(raw_k)
        v = _decode(raw_v)
        if k is None:
            continue
        out[k] = v if v else None
    return {
        "timestamp": out.get("timestamp"),
        "triggered_by": out.get("triggered_by"),
        "reason": out.get("reason"),
    }


async def read_all(
    redis_client: aioredis.Redis,
) -> dict[str, dict[str, str | None] | None]:
    """모든 flag 의 last_change 한 번에. 없는 자리는 None."""
    return {flag: await read_change(redis_client, flag) for flag in AUDIT_FLAGS}
