"""Agent Coordinator — v3 multi-service mesh coordination layer.

design: ../../.ai/designs/2026-05-14-agent-coordinator.md
결정 메모리: project_v3_orchestrator_decision.md
audit 근거: project_audit_2026_05_14.md

Stage 1 (현재): listener-only. State Hub + Event Bus 골격 + decision_log/event_log 기록.
Stage 2 (예정): advisory decisions.
Stage 3 (예정): enforce mode + supervisor.

원칙 (design §0):
  1. slow/fast loop timing 유지
  2. agents 끼리 직접 소통 금지 (항상 Coordinator 거침)
  3. 점진 도입 (observe → advisory → enforce → supervisor)
  4. Pydantic schema 의 lifecycle 안전성
  5. 결정론 (Coordinator) ↔ 비결정론 (LLM agents) 분리 — prompt 로 결정론 제어 시도 금지
"""

from prime_jennie_runtime.coordinator.events import (
    CoordinatorEvent,
    EntryDecidedEvent,
    EntryFilledEvent,
    EntryQueuedEvent,
    EntryRejectedEvent,
    ExitDecidedEvent,
    ExitFilledEvent,
    ExternalEvent,
    PolicyChangedEvent,
    SheetPublishedEvent,
)
from prime_jennie_runtime.coordinator.listener import (
    COORDINATOR_CONSUMER_ENV,
    COORDINATOR_GROUP,
    run_listener,
)
from prime_jennie_runtime.coordinator.publish import (
    COORDINATOR_STREAM,
    COORDINATOR_STREAM_MAXLEN,
    publish_event,
)

__all__ = [
    "COORDINATOR_CONSUMER_ENV",
    "COORDINATOR_GROUP",
    "COORDINATOR_STREAM",
    "COORDINATOR_STREAM_MAXLEN",
    "CoordinatorEvent",
    "EntryDecidedEvent",
    "EntryFilledEvent",
    "EntryQueuedEvent",
    "EntryRejectedEvent",
    "ExitDecidedEvent",
    "ExitFilledEvent",
    "ExternalEvent",
    "PolicyChangedEvent",
    "SheetPublishedEvent",
    "publish_event",
    "run_listener",
]
