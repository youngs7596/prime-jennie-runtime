"""PositionTracker — 활성 포지션 상태 저장소.

in-memory dict로 빠르게 조회하고 Redis Hash `position_state:{sheet_id}`에 백업.
재시작 시 Redis에서 로드 → in-memory 복원.

v2는 종목별로 흩어진 키(`watermark:{code}`, `scale_out:{code}` 등)를 썼지만,
v3는 시트 단위(같은 종목 시트 여러 개 가능하도록)로 한 Hash에 묶는다.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime

import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.domain import PositionState

logger = logging.getLogger(__name__)

STATE_KEY_PREFIX = "position_state:"


def _state_key(sheet_id: str) -> str:
    return f"{STATE_KEY_PREFIX}{sheet_id}"


@dataclass
class TrackerEntry:
    """tracker가 관리하는 내부 엔트리 — state + 정적 정보."""

    state: PositionState
    exit_rules_raw: list[dict] = field(default_factory=list)  # exit_evaluator 호출 편의용


class PositionTracker:
    """sheet_id → PositionState + ticker → sheet_ids 역인덱스.

    단일 프로세스 기준. 재시작 시 `load_from_redis()`로 복원.
    """

    def __init__(self, redis_client: aioredis.Redis):
        self._redis = redis_client
        self._states: dict[str, PositionState] = {}
        self._by_ticker: dict[str, set[str]] = defaultdict(set)

    # ─── 조회 ─────────────────────────────────────────────

    def get(self, sheet_id: str) -> PositionState | None:
        return self._states.get(sheet_id)

    def sheet_ids_for(self, ticker: str) -> list[str]:
        return sorted(self._by_ticker.get(ticker, set()))

    def active_sheet_ids(self) -> list[str]:
        return sorted(self._states.keys())

    def tickers_active(self) -> list[str]:
        return sorted(t for t, ids in self._by_ticker.items() if ids)

    # ─── 변경 ─────────────────────────────────────────────

    async def register(self, state: PositionState) -> None:
        """entry 체결 후 호출."""
        self._states[state.sheet_id] = state
        self._by_ticker[state.ticker].add(state.sheet_id)
        await self._persist(state)

    async def persist(self, sheet_id: str) -> None:
        """변경된 state를 Redis에 다시 저장."""
        state = self._states.get(sheet_id)
        if state is not None:
            await self._persist(state)

    async def close(self, sheet_id: str) -> None:
        """exit 체결 후 호출 — 추적 종료."""
        state = self._states.pop(sheet_id, None)
        if state is not None:
            self._by_ticker[state.ticker].discard(sheet_id)
            if not self._by_ticker[state.ticker]:
                del self._by_ticker[state.ticker]
            await self._redis.delete(_state_key(sheet_id))

    async def _persist(self, state: PositionState) -> None:
        await self._redis.set(_state_key(state.sheet_id), _encode_state(state))

    # ─── 복구 ─────────────────────────────────────────────

    async def load_from_redis(self) -> int:
        """부팅 시 호출. Redis에 있는 모든 state를 in-memory에 복원.

        Returns: 복원된 개수
        """
        cursor = 0
        count = 0
        while True:
            cursor, keys = await self._redis.scan(cursor, match=f"{STATE_KEY_PREFIX}*", count=100)
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                raw = await self._redis.get(key)
                if raw is None:
                    continue
                try:
                    state = _decode_state(raw)
                except Exception:
                    logger.exception("failed to decode state %s", key)
                    continue
                self._states[state.sheet_id] = state
                self._by_ticker[state.ticker].add(state.sheet_id)
                count += 1
            if cursor == 0:
                break
        logger.info("position_tracker restored %d states from redis", count)
        return count


# ─── 직렬화 ─────────────────────────────────────────────


def _encode_state(state: PositionState) -> str:
    data = asdict(state)
    # set → list, datetime → iso string
    data["scale_out_executed"] = sorted(state.scale_out_executed)
    data["entered_at"] = state.entered_at.isoformat()
    if state.death_cross_checked_at is not None:
        data["death_cross_checked_at"] = state.death_cross_checked_at.isoformat()
    return json.dumps(data, ensure_ascii=False)


def _decode_state(raw: str | bytes) -> PositionState:
    if isinstance(raw, bytes):
        raw = raw.decode()
    data = json.loads(raw)
    data["entered_at"] = datetime.fromisoformat(data["entered_at"])
    if data.get("death_cross_checked_at"):
        data["death_cross_checked_at"] = datetime.fromisoformat(data["death_cross_checked_at"])
    data["scale_out_executed"] = set(data.get("scale_out_executed") or [])
    return PositionState(**data)
