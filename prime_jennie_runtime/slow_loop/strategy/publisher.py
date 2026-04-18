"""PositionSheetPublisher — 발행 + DB persist + DLQ.

TypedStreamPublisher(`v3:position_sheets`)를 래핑. validator 실패 같은 희귀
상황에서는 DLQ 스트림(`v3:position_sheets.dlq`)에 raw payload + 에러를 남긴다.

2026-04-19 개정: publish() 에서 **Redis stream 발행 전에 `position_sheets`
테이블에 upsert** 한다. 이유는 slow_loop pipeline 이 publish 직후 곧바로
`screening_candidates.promoted_to_sheet_id = sheet.sheet_id` 로 FK UPDATE 를
시도하는데, fast_loop 의 stream consume + DB insert 가 비동기라 race
조건에서 FK violation (promoted_to_sheet_id not in position_sheets) 이 발생했다.
DB upsert 를 publish 전에 동기로 끝내면 후행 FK UPDATE 가 안전하다.

ON CONFLICT DO NOTHING 으로 fast_loop 쪽에서 중복 insert 를 시도해도 무해.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from prime_jennie_runtime.infra.redis_streams import (
    STREAM_POSITION_SHEETS,
    STREAM_POSITION_SHEETS_DLQ,
    TypedStreamPublisher,
)
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)


_UPSERT_SHEET_SQL = text(
    """
    INSERT INTO position_sheets (
        sheet_id, schema_version, generated_at, valid_until,
        ticker, strategy_tag, sheet_json, provenance_json
    ) VALUES (
        :sid, :sv, :ga, :vu,
        :tk, :st, CAST(:sj AS JSONB), CAST(:pj AS JSONB)
    )
    ON CONFLICT (sheet_id) DO NOTHING
    """
)


class PositionSheetPublisher:
    """발행 + DB persist + 에러 핸들링."""

    def __init__(
        self,
        client: aioredis.Redis,
        db_engine: AsyncEngine | None = None,
        maxlen: int = 10000,
    ):
        self._client = client
        self._db_engine = db_engine
        self._publisher: TypedStreamPublisher[PositionSheet] = TypedStreamPublisher(
            client, STREAM_POSITION_SHEETS, PositionSheet, maxlen=maxlen
        )
        self._dlq_stream = STREAM_POSITION_SHEETS_DLQ

    async def publish(self, sheet: PositionSheet) -> str:
        """DB upsert → stream 발행. 예외는 DLQ 로 후송.

        DB 실패는 치명적 (후행 `update_candidate_promotion` 의 FK 위반).
        stream 실패는 DLQ 로 guard.
        """
        await self._persist_sheet(sheet)
        try:
            return await self._publisher.publish(sheet)
        except Exception as e:
            logger.exception("publish failed, sending to DLQ: %s", sheet.sheet_id)
            await self._send_to_dlq(sheet.model_dump_json(), str(e))
            raise

    async def _persist_sheet(self, sheet: PositionSheet) -> None:
        """position_sheets 테이블에 upsert. engine 미주입 시 no-op."""
        if self._db_engine is None:
            return
        try:
            async with self._db_engine.begin() as conn:
                await conn.execute(
                    _UPSERT_SHEET_SQL,
                    {
                        "sid": sheet.sheet_id,
                        "sv": sheet.schema_version,
                        "ga": sheet.generated_at,
                        "vu": sheet.valid_until,
                        "tk": sheet.ticker,
                        "st": sheet.strategy_tag,
                        "sj": sheet.model_dump_json(),
                        "pj": json.dumps(
                            sheet.provenance.model_dump(mode="json"),
                            ensure_ascii=False,
                            default=str,
                        ),
                    },
                )
        except Exception:
            logger.exception("position_sheets persist failed: sheet=%s", sheet.sheet_id)
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
