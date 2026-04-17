"""Control 라우터 — UI → `v3:control.commands` stream publish.

Phase 2.10 신규. v2 에는 없던 라우터 — Telegram bot 이 유일한 제어 경로였던 것을
UI 에서도 같은 stream 으로 보낼 수 있게 노출. `control_consumer` 가 소비하므로
audit trail 이 Telegram 과 통일된다.

엔드포인트 (ControlKind 와 1:1 매핑):
- POST /api/control/stop       → `emergency_stop`
- POST /api/control/pause      → `pause`
- POST /api/control/resume     → `resume`
- POST /api/control/dryrun     → `set_dryrun` (body: {enabled: bool})
- POST /api/control/liquidate  → `liquidate_arm` / `liquidate_disarm`
                                  (body: {action: "arm"|"disarm", confirm?: bool})
                                  arm 시 confirm=true 필수.
- GET  /api/control/state      → 현재 control.state:* Redis 상태

모든 publish 는 issued_by="ui:<user>" — user 는 `X-User` 헤더에서 읽는다 (없으면 "ui:anon").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from prime_jennie_runtime.infra.redis_streams import STREAM_CONTROL_COMMANDS, TypedStreamPublisher
from prime_jennie_runtime.telegram_bot.control import (
    STATE_KEY_DRYRUN,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    ControlCommand,
    ControlKind,
)

from ..deps import get_redis

router = APIRouter(prefix="/control", tags=["control"])


class ControlResponse(BaseModel):
    ok: bool = True
    kind: ControlKind
    message_id: str
    issued_by: str
    issued_at: datetime


class StopRequest(BaseModel):
    reason: str | None = None


class PauseRequest(BaseModel):
    reason: str | None = None


class ResumeRequest(BaseModel):
    reason: str | None = None


class DryRunRequest(BaseModel):
    enabled: bool
    reason: str | None = None


class LiquidateRequest(BaseModel):
    action: Literal["arm", "disarm"]
    confirm: bool = False
    reason: str | None = None


class ControlStateResponse(BaseModel):
    stop: bool
    pause: bool
    dryrun: bool
    liquidate_armed: bool
    fetched_at: datetime


async def _publish(
    redis: aioredis.Redis,
    kind: ControlKind,
    issued_by: str,
    reason: str | None,
    payload: dict | None = None,
) -> ControlResponse:
    publisher: TypedStreamPublisher[ControlCommand] = TypedStreamPublisher(
        redis, STREAM_CONTROL_COMMANDS, ControlCommand
    )
    cmd = ControlCommand(
        kind=kind,
        issued_at=datetime.now(UTC),
        issued_by=issued_by,
        reason=reason,
        payload=dict(payload or {}),
    )
    msg_id = await publisher.publish(cmd)
    return ControlResponse(
        kind=kind, message_id=msg_id, issued_by=issued_by, issued_at=cmd.issued_at
    )


def _issuer(x_user: str | None) -> str:
    return f"ui:{x_user}" if x_user else "ui:anon"


@router.post("/stop", response_model=ControlResponse)
async def stop(
    body: StopRequest | None = None,
    x_user: str | None = Header(default=None, alias="X-User"),
    redis: aioredis.Redis = Depends(get_redis),
) -> ControlResponse:
    reason = (body.reason if body else None) or "ui_emergency"
    return await _publish(redis, "emergency_stop", _issuer(x_user), reason)


@router.post("/pause", response_model=ControlResponse)
async def pause(
    body: PauseRequest | None = None,
    x_user: str | None = Header(default=None, alias="X-User"),
    redis: aioredis.Redis = Depends(get_redis),
) -> ControlResponse:
    reason = (body.reason if body else None) or "ui_pause"
    return await _publish(redis, "pause", _issuer(x_user), reason)


@router.post("/resume", response_model=ControlResponse)
async def resume(
    body: ResumeRequest | None = None,
    x_user: str | None = Header(default=None, alias="X-User"),
    redis: aioredis.Redis = Depends(get_redis),
) -> ControlResponse:
    return await _publish(redis, "resume", _issuer(x_user), body.reason if body else None)


@router.post("/dryrun", response_model=ControlResponse)
async def dryrun(
    body: DryRunRequest,
    x_user: str | None = Header(default=None, alias="X-User"),
    redis: aioredis.Redis = Depends(get_redis),
) -> ControlResponse:
    return await _publish(
        redis,
        "set_dryrun",
        _issuer(x_user),
        body.reason,
        payload={"enabled": body.enabled},
    )


@router.post("/liquidate", response_model=ControlResponse)
async def liquidate(
    body: LiquidateRequest,
    x_user: str | None = Header(default=None, alias="X-User"),
    redis: aioredis.Redis = Depends(get_redis),
) -> ControlResponse:
    if body.action == "arm" and not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="liquidate arm requires confirm=true",
        )
    kind: ControlKind = "liquidate_arm" if body.action == "arm" else "liquidate_disarm"
    return await _publish(redis, kind, _issuer(x_user), body.reason)


@router.get("/state", response_model=ControlStateResponse)
async def state(redis: aioredis.Redis = Depends(get_redis)) -> ControlStateResponse:
    stop_v = await redis.get(STATE_KEY_STOP)
    pause_v = await redis.get(STATE_KEY_PAUSE)
    dry_v = await redis.get(STATE_KEY_DRYRUN)
    armed_v = await redis.get(STATE_KEY_LIQUIDATE_ARMED)
    return ControlStateResponse(
        stop=bool(stop_v),
        pause=bool(pause_v),
        dryrun=bool(dry_v),
        liquidate_armed=bool(armed_v),
        fetched_at=datetime.now(UTC),
    )
