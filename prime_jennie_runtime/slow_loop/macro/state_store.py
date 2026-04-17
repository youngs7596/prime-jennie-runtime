"""MacroStateStore — Redis `macro:current_state` 읽기/쓰기 + stale detection.

MACRO_GATE_SPEC §6.3, §6.4:
- 최신 macro_run의 JSON snapshot을 `macro:current_state`에 보관 (TTL 없음)
- 24시간 이상 stale이면 Strategy Engine이 시트 발행을 중단해야 함

저장 포맷 (JSON):
{
  "macro_run_id": "...",
  "gate": "open|closed",
  "size_multiplier": 0.75,
  "generated_at": "2026-04-16T08:00:12+09:00"
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from pydantic import BaseModel

CURRENT_STATE_KEY = "macro:current_state"
STALE_THRESHOLD = timedelta(hours=24)


class MacroCurrentState(BaseModel):
    """Redis `macro:current_state`의 저장 포맷."""

    macro_run_id: str
    gate: str  # "open" | "closed"
    size_multiplier: float
    generated_at: datetime


@dataclass
class MacroStateStore:
    """Redis 기반 상태 저장소."""

    client: aioredis.Redis
    key: str = CURRENT_STATE_KEY

    async def set(self, state: MacroCurrentState) -> None:
        await self.client.set(self.key, state.model_dump_json())

    async def get(self) -> MacroCurrentState | None:
        raw = await self.client.get(self.key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return MacroCurrentState.model_validate_json(raw)

    async def is_stale(self, now: datetime | None = None) -> bool:
        """현재 상태가 24시간 이상 오래됐으면 True (MG08).

        상태 자체가 없어도 True (Strategy Engine은 발행 중단해야 함).
        """
        state = await self.get()
        if state is None:
            return True
        reference = now or datetime.now(UTC)
        # generated_at은 timezone aware, reference도 aware여야 함
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        age = reference - state.generated_at
        return age >= STALE_THRESHOLD


def state_from_json(raw: str | bytes) -> MacroCurrentState:
    """외부에서 받은 raw JSON을 파싱. Redis pub/sub 구독자 편의."""
    if isinstance(raw, bytes):
        raw = raw.decode()
    data = json.loads(raw)
    return MacroCurrentState.model_validate(data)
