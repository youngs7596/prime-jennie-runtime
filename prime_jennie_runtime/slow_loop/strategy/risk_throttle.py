"""Intraday Risk Throttle 스냅샷 프로토콜.

Track C(fast loop)가 실제 RiskThrottle을 구현한다. Track B는 **시트 발행 시점**에
스냅샷 값만 읽어 `size.risk_multiplier`에 고정한다 (POSITION_SHEET_SPEC §3.1).

Phase 1 slow loop는 기본 NoOpRiskThrottle(1.0)을 사용. Track C 완성 시 실제 구현체로 교체.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class RiskThrottleSnapshot(Protocol):
    """현재 risk_multiplier (0.0 ~ 1.0)을 돌려주는 프로토콜."""

    def current_multiplier(self) -> float: ...


@dataclass(frozen=True)
class NoOpRiskThrottle:
    """항상 1.0을 반환하는 stub. Phase 1 기본값."""

    def current_multiplier(self) -> float:
        return 1.0


@dataclass(frozen=True)
class FixedRiskThrottle:
    """테스트용 고정값 throttle."""

    value: float

    def current_multiplier(self) -> float:
        return self.value
