"""PositionSheetPublisher — 발행 + DLQ.

TypedStreamPublisher(`v3:position_sheets`)를 래핑. validator 실패 같은 희귀
상황에서는 DLQ 스트림(`v3:position_sheets.dlq`)에 raw payload + 에러를 남긴다.

Track A의 redis_streams.py가 기반. DB persistence(position_sheets 테이블)는
Phase 2 운영화 시 이 클래스에 주입 (별도 Writer 프로토콜).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import redis.asyncio as aioredis

from prime_jennie_runtime.infra.redis_streams import (
    STREAM_POSITION_SHEETS,
    STREAM_POSITION_SHEETS_DLQ,
    TypedStreamPublisher,
)
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)


class PositionSheetPublisher:
    """발행 + 에러 핸들링."""

    def __init__(self, client: aioredis.Redis, maxlen: int = 10000):
        self._client = client
        self._publisher: TypedStreamPublisher[PositionSheet] = TypedStreamPublisher(
            client, STREAM_POSITION_SHEETS, PositionSheet, maxlen=maxlen
        )
        self._dlq_stream = STREAM_POSITION_SHEETS_DLQ

    async def publish(self, sheet: PositionSheet) -> str:
        """정상 발행. 예외는 DLQ로 후송."""
        try:
            return await self._publisher.publish(sheet)
        except Exception as e:
            logger.exception("publish failed, sending to DLQ: %s", sheet.sheet_id)
            await self._send_to_dlq(sheet.model_dump_json(), str(e))
            raise

    async def send_raw_to_dlq(self, raw_payload: str, error: str) -> None:
        """Scout/Macro 등 상위에서 시트 validator가 터졌을 때 호출."""
        await self._send_to_dlq(raw_payload, error)

    async def _send_to_dlq(self, payload: str, error: str) -> None:
        envelope = {
            "payload": payload,
            "error": error,
            "ts": datetime.now(UTC).isoformat(),
        }
        await self._client.xadd(
            self._dlq_stream,
            {"payload": json.dumps(envelope, ensure_ascii=False)},
            maxlen=1000,
            approximate=True,
        )
