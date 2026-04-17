"""infra/observer_impl.py 테스트."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.infra.observer_impl import (
    CompositePJObserver,
    StructlogPJObserver,
    build_observer,
    pj_event,
)


@pytest.mark.asyncio
async def test_structlog_observer_emits():
    """StructlogPJObserver가 예외 없이 이벤트를 처리."""
    obs = StructlogPJObserver()
    event = pj_event("pj.test.event", role="scout", ok=True, duration_ms=42)
    await obs.emit(event)  # 예외 없으면 성공


@pytest.mark.asyncio
async def test_composite_observer_tolerates_failure():
    """하위 observer 실패가 전파되지 않음."""

    class FailingObserver:
        async def emit(self, event):
            raise RuntimeError("boom")

    obs = CompositePJObserver(FailingObserver(), StructlogPJObserver())
    event = pj_event("pj.test.fail", ok=False)
    await obs.emit(event)  # FailingObserver 실패해도 전파 안 됨


def test_build_observer_without_langfuse():
    obs = build_observer(langfuse_enabled=False)
    assert isinstance(obs, StructlogPJObserver)


def test_pj_event_fields():
    event = pj_event(
        "pj.scout.code_generated",
        role="scout",
        ok=True,
        duration_ms=1500,
        code_hash="sha256:abc",
    )
    assert event.name == "pj.scout.code_generated"
    assert event.role == "scout"
    assert event.ok is True
    assert event.metadata["code_hash"] == "sha256:abc"
