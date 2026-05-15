"""Policy base — Stage 2 advisory 정책의 공통 contract.

Stage 2 는 advisory only (관찰 + notify). Stage 3 (enforce) 는 별도 layer 에서
호출 권한 부여 — 본 contract 자체는 enforce 와 advisory 모두 표현 가능.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from prime_jennie_runtime.coordinator.events import CoordinatorEvent

PolicyOutcome = Literal["ok", "advisory_not_ok"]


@dataclass(frozen=True)
class PolicyResult:
    """단일 정책 평가 결과 — listener 가 decision_log 에 기록.

    Attributes:
        outcome: "ok" 면 통과 (보통 decision_log 미기록), "advisory_not_ok"
            면 advisory alert + decision_log INSERT.
        reason: 정책 식별자 (예: "recent_stoploss_cooldown"). decision_log.reason
            컬럼에 그대로 들어감.
        policy_name: 발화 클래스 명 (예: "RecentStoplossCooldownPolicy").
            decision_log.policy_name 컬럼.
        subject_id: 결정 대상 식별자 (보통 sheet_id). 없으면 None.
        context: context_snapshot_json 의 추가 필드 (정책 고유 상태).
    """

    outcome: PolicyOutcome
    reason: str
    policy_name: str
    subject_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


class Policy(Protocol):
    """정책의 contract — listener 의 dispatcher 가 호출."""

    name: str

    def applies_to(self, event: CoordinatorEvent) -> bool:
        """정책이 이 event 에 적용되는지 — 빠른 filter (DB query 전)."""
        ...

    async def evaluate(self, pool: Any, event: CoordinatorEvent) -> PolicyResult | None:
        """평가 — 위반 시 PolicyResult(advisory_not_ok), 아니면 None.

        None 을 반환하면 decision_log 미기록 (정상 path 는 row 안 만들어 noise
        최소화). advisory_not_ok 반환 시 dispatcher 가 decision_log INSERT +
        alert.
        """
        ...
