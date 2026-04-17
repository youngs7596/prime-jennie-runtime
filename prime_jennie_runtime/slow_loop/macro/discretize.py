"""size_multiplier 이산화 로직.

MACRO_GATE_SPEC §2.2 half-open (low, high] 방식. 경계값은 아래 구간 포함.
  - 0.25 → 0.25, 0.2500001 → 0.50 (MG15, MG22)
  - open + 0.0 → 0.25 강제 + inconsistent_open_zero 이벤트 (MG21)
  - x > 1.0 → 1.0, x < 0.0 → 0.0 (clamp, MG19 MG20)
  - gate == closed → 항상 0.0 (MG06)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

logger = logging.getLogger(__name__)


def discretize_sync(
    x: float,
    gate: Literal["open", "closed"],
    *,
    inconsistent_hook: Callable[[], None] | None = None,
) -> float:
    """동기 버전 이산화. 테스트 및 순수 함수 용도.

    inconsistent_hook: open + 0.0 모순 발생 시 호출되는 콜백 (optional).
                      async observer 대신 테스트에서 flag 확인용.
    """
    if gate == "closed":
        return 0.0

    # clamp
    x = max(0.0, min(1.0, x))

    if x == 0.0:
        # open + 0.0은 모순 — 0.25로 강제
        if inconsistent_hook is not None:
            inconsistent_hook()
        logger.warning("macro gate=open but size_multiplier=0.0 — forced to 0.25")
        return 0.25

    # half-open (low, high]
    if x <= 0.25:
        return 0.25
    if x <= 0.50:
        return 0.50
    if x <= 0.75:
        return 0.75
    return 1.0
