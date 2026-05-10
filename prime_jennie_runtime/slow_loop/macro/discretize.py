"""size_multiplier 이산화 로직.

MACRO_GATE_SPEC §2.2 half-open (low, high] 방식. 경계값은 아래 구간 포함.

open 게이트는 2단계로 축소 (2026-05-10):
  - 0.0 < x <= 0.75 → 0.75 (보수~중립)
  - 0.75 < x <= 1.0 → 1.0 (적극)
  - open + 0.0 → 0.75 강제 + inconsistent_open_zero 이벤트
  - x > 1.0 → 1.0, x < 0.0 → 0.0 (clamp)
  - gate == closed → 항상 0.0

Why: base_pct(3%) × macro 0.25/0.50 = 0.75~1.5% 슬롯이 cash 잔여분에
곱해지면 종목당 1주도 못 사는 상황 발생. open 의 의미를 "유의미한 규모로
매수" 로 정의하고, 그보다 보수적이어야 하면 closed 로 가는 것이 일관됨.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

logger = logging.getLogger(__name__)

OPEN_FLOOR = 0.75


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
        # open + 0.0은 모순 — OPEN_FLOOR 로 강제
        if inconsistent_hook is not None:
            inconsistent_hook()
        logger.warning("macro gate=open but size_multiplier=0.0 — forced to %s", OPEN_FLOOR)
        return OPEN_FLOOR

    # 2 단계 (low, high]
    if x <= OPEN_FLOOR:
        return OPEN_FLOOR
    return 1.0
