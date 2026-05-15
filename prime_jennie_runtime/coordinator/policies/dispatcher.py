"""Policy dispatcher — listener 가 archive 직후 호출.

매 event 마다 등록된 정책들을 순회하면서 ``applies_to`` 통과한 정책만 evaluate.
advisory_not_ok 인 결과는 decision_log INSERT + GenericAlertNotification emit
(notifier 주입 시).

설계 의도:
  - listener 의 hot path 와 분리 — dispatcher 호출 자체가 실패해도 archive 는
    이미 끝나있고 ACK 됨. 정책 실패는 silent log 만.
  - DEFAULT_POLICIES 가 활성 정책 목록의 단일 진실. 새 정책 추가 시 여기에 등록.

design: .ai/designs/2026-05-15-cooldown-and-duplicate-guard.md §4
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from prime_jennie_runtime.coordinator.events import CoordinatorEvent
from prime_jennie_runtime.coordinator.policies.base import Policy, PolicyResult
from prime_jennie_runtime.coordinator.policies.duplicate_today import (
    DuplicateTodayPolicy,
)
from prime_jennie_runtime.coordinator.policies.recent_stoploss_cooldown import (
    RecentStoplossCooldownPolicy,
)

logger = logging.getLogger(__name__)


# fast_loop 의 GenericAlertNotification 을 그대로 emit — telegram_bot 가
# v3:notifications stream 에서 소비 (기존 패턴 동일).
# fast_loop 모듈 의존을 코드 시점에 끌어오지 않기 위해 lazy import.

DEFAULT_POLICIES: tuple[Policy, ...] = (
    RecentStoplossCooldownPolicy(),
    DuplicateTodayPolicy(),
)

_INSERT_DECISION = (
    "INSERT INTO decision_log "
    "(kind, subject_id, outcome, reason, policy_name, context_snapshot_json, decided_at) "
    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)"
)


def _decision_kind_for(event: CoordinatorEvent) -> str:
    """event 종류 → decision_log.kind 매핑. sheet → sheet_publish_decision,
    entry → entry_decision. 기타는 generic policy_change_decision."""
    k = event.event_kind
    if k == "sheet_published":
        return "sheet_publish_decision"
    if k in ("entry_queued", "entry_decided", "entry_filled", "entry_rejected"):
        return "entry_decision"
    if k in ("exit_decided", "exit_filled"):
        return "exit_decision"
    return "policy_change_decision"


async def _record_decision(pool: Any, event: CoordinatorEvent, result: PolicyResult) -> None:
    """decision_log INSERT. 실패는 silent — alert 는 별도 단계라 영향 0."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_DECISION,
                _decision_kind_for(event),
                result.subject_id,
                result.outcome,
                result.reason,
                result.policy_name,
                json.dumps(result.context, default=str),
                datetime.now(UTC),
            )
    except Exception:
        logger.exception(
            "decision_log insert failed policy=%s reason=%s",
            result.policy_name,
            result.reason,
        )


async def _emit_alert(notifier: Any, event: CoordinatorEvent, result: PolicyResult) -> None:
    """advisory alert publish. notifier 없으면 log only."""
    title = f"[advisory] {result.reason}"
    body = (
        f"policy={result.policy_name} ticker={result.context.get('ticker', '?')} "
        f"sheet_id={result.subject_id} trigger={event.event_kind}"
    )
    if notifier is None:
        logger.warning("advisory (no notifier): %s — %s", title, body)
        return
    try:
        # lazy import 로 coordinator 가 fast_loop 에 module-level 의존하지 않도록.
        from prime_jennie_runtime.fast_loop.schemas import GenericAlertNotification

        await notifier.emit(
            GenericAlertNotification(
                severity="warning",
                title=title,
                body=body,
                ts=datetime.now(UTC),
                metadata={
                    "component": "coordinator_policy",
                    "policy": result.policy_name,
                    "reason": result.reason,
                    **{k: str(v) for k, v in result.context.items()},
                },
            )
        )
    except Exception:
        logger.exception(
            "advisory emit failed policy=%s reason=%s",
            result.policy_name,
            result.reason,
        )


async def evaluate_policies(
    pool: Any,
    event: CoordinatorEvent,
    *,
    notifier: Any | None = None,
    policies: tuple[Policy, ...] = DEFAULT_POLICIES,
) -> list[PolicyResult]:
    """등록된 정책을 순회. advisory_not_ok 결과는 decision_log + alert.

    Returns:
        실제 발화한 정책 결과들 (advisory_not_ok 만). 정상 path 는 빈 list.
    """
    fired: list[PolicyResult] = []
    for policy in policies:
        if not policy.applies_to(event):
            continue
        try:
            result = await policy.evaluate(pool, event)
        except Exception:
            logger.exception(
                "policy evaluation crashed policy=%s event_kind=%s",
                policy.name,
                event.event_kind,
            )
            continue
        if result is None or result.outcome == "ok":
            continue
        await _record_decision(pool, event, result)
        await _emit_alert(notifier, event, result)
        fired.append(result)
    return fired


# pydantic forward-ref placeholder (정책 평가 path 가 BaseModel 직접 다루지
# 않지만, 의존성 그래프상 BaseModel 을 명시적으로 import 한 상태 유지).
_ = BaseModel
