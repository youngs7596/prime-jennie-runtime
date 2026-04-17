"""PJObserver — minyoung-mah Observer 프로토콜 구현.

structlog 기반 로깅 + Langfuse 스팬 연동.
pj.* 네임스페이스 이벤트를 발행하여 트레이딩 파이프라인 관찰.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from minyoung_mah import ObserverEvent

logger = logging.getLogger(__name__)


class StructlogPJObserver:
    """structlog 기반 observer. 모든 이벤트를 구조화 로그로 출력."""

    async def emit(self, event: ObserverEvent) -> None:
        level = logging.WARNING if event.ok is False else logging.DEBUG
        extra = {
            "event_name": event.name,
            "timestamp": event.timestamp.isoformat(),
        }
        if event.role:
            extra["role"] = event.role
        if event.tool:
            extra["tool"] = event.tool
        if event.duration_ms is not None:
            extra["duration_ms"] = event.duration_ms
        if event.ok is not None:
            extra["ok"] = event.ok
        if event.metadata:
            extra.update(event.metadata)

        logger.log(level, "[%s] %s", event.name, extra)


class CompositePJObserver:
    """여러 observer를 조합. 하나가 실패해도 나머지에 전파하지 않음."""

    def __init__(self, *observers: object) -> None:
        self._observers = list(observers)

    async def emit(self, event: ObserverEvent) -> None:
        for obs in self._observers:
            try:
                await obs.emit(event)  # type: ignore[union-attr]
            except Exception:
                pass


def build_observer(langfuse_enabled: bool = False) -> object:
    """Observer 팩토리. Langfuse 활성화 시 composite, 아니면 structlog 단독."""
    structlog_obs = StructlogPJObserver()
    if not langfuse_enabled:
        return structlog_obs
    return CompositePJObserver(structlog_obs)


def pj_event(
    name: str,
    *,
    role: str | None = None,
    tool: str | None = None,
    ok: bool | None = None,
    duration_ms: int | None = None,
    **metadata: object,
) -> ObserverEvent:
    """pj.* 네임스페이스 이벤트 생성 헬퍼."""
    return ObserverEvent(
        name=name,
        timestamp=datetime.now(UTC),
        role=role,
        tool=tool,
        ok=ok,
        duration_ms=duration_ms,
        metadata=dict(metadata),
    )
