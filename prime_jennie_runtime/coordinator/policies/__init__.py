"""Coordinator advisory policies (Stage 2 첫 use case).

design: .ai/designs/2026-05-15-cooldown-and-duplicate-guard.md §4
상위 design: .ai/designs/2026-05-14-agent-coordinator.md §5 / §8 Stage 2

각 policy 는 event_log archive 성공 직후 listener 로부터 호출되어 평가된다.
advisory_not_ok 결과는 decision_log INSERT + GenericAlertNotification emit
(notifier 경유) — fast_loop 의 enforcement 와 별개로 관찰만.
"""

from .base import Policy, PolicyResult
from .dispatcher import evaluate_policies
from .duplicate_today import DuplicateTodayPolicy
from .recent_stoploss_cooldown import RecentStoplossCooldownPolicy

__all__ = [
    "DuplicateTodayPolicy",
    "Policy",
    "PolicyResult",
    "RecentStoplossCooldownPolicy",
    "evaluate_policies",
]
