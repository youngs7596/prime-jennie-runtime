"""사용자가 인지한 비추적 보유 — state↔KIS 대조 경고의 무시 목록.

KIS 실잔고에는 있지만 v3 가 의도적으로 추적하지 않는 종목 (예: 2026-06-12
KODEX200(069500) 관리 제외 결정). 두 대조기 — positions_reconcile 5분 잡 +
fast-loop 부팅 검사 — 의 **only_in_kis 방향 경고에서만 제외**한다. 반대 방향
(v3 만 보유라 착각 — 무한 매도 위험) 과 수량 drift 는 여전히 경고한다.

env ``RECONCILE_IGNORE_KIS_ONLY``: 쉼표 구분 ticker 목록. 호출 시점마다 읽어
컨테이너 재시작 없이 테스트 가능. compose 의 fast-loop / job-worker 두 서비스
environment 에 함께 정의 (코드/compose 양면 — 누락 시 컨테이너에 안 닿음).
"""

from __future__ import annotations

import os

ENV_RECONCILE_IGNORE_KIS_ONLY = "RECONCILE_IGNORE_KIS_ONLY"


def acknowledged_untracked_tickers() -> set[str]:
    """무시 목록 ticker set — env 미설정이면 빈 set (모든 mismatch 경고)."""
    raw = os.environ.get(ENV_RECONCILE_IGNORE_KIS_ONLY, "")
    return {t.strip() for t in raw.split(",") if t.strip()}


__all__ = ["ENV_RECONCILE_IGNORE_KIS_ONLY", "acknowledged_untracked_tickers"]
