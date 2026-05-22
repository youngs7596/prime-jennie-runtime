"""결정론 선정 — Phase 4.5 MA 평활 + Phase 6 히스테리시스 선정.

v2 `prime_jennie/services/scout/app.py::_compute_ma_scores` + `selection.py` 의
히스테리시스 필터를 포팅 (2026-05-22, Phase 1 a).

**포팅 제외**: v2 `selection.py` 의 greedy 섹터 cap — v3 는 2026-05-22 step3 에서
`strategy/engine.py` 에 섹터 cap 을 이미 복원·배포함 (decision §a "그 위에 build,
재구현 금지"). 따라서 이 모듈은 MA 평활 + 진입/이탈 히스테리시스만 담는다.

v2 는 MA 를 `hybrid_score`(LLM 보정 후)에 적용했다. v3 는 선정 경로 LLM 이 없으므로
MA 대상이 `QuantScore.total_score` 다 — 알고리즘은 동일, 입력 점수만 다름.
v2 측정상 hybrid ≈ quant (LLM 이동 평균 +1.12) 라 entry/exit 임계(62/55)는 그대로 유효.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# v2 ScoutConfig 기본값 — env override 가능 (v2 도 env 주입식이었음).
MA_WINDOW: int = int(os.environ.get("SELECTION_MA_WINDOW", "3"))
ENTRY_THRESHOLD: float = float(os.environ.get("SELECTION_ENTRY_THRESHOLD", "62.0"))
EXIT_THRESHOLD: float = float(os.environ.get("SELECTION_EXIT_THRESHOLD", "55.0"))


def compute_ma_scores(
    current_scores: dict[str, float],
    historical: dict[str, list[float]],
    window: int = MA_WINDOW,
) -> dict[str, float]:
    """현재 run 점수 + 과거 점수 → 이동평균.

    v2 `app.py::_compute_ma_scores` 포팅. 과거 점수(오래된→최신 순) 뒤에 현재
    점수를 붙이고 마지막 ``window`` 개의 평균.

    Args:
        current_scores: {stock_code: 이번 run 의 total_score}.
        historical: {stock_code: [과거 run 점수, 오래된→최신]}.
        window: 이동평균 윈도우 (현재 run 포함).

    Returns:
        {stock_code: ma_score}.
    """
    ma_map: dict[str, float] = {}
    for code, current in current_scores.items():
        past = historical.get(code, [])
        all_scores = [*past, current]
        recent = all_scores[-window:]
        ma = sum(recent) / len(recent)
        ma_map[code] = round(ma, 1)

        if abs(ma - current) >= 1.0:
            logger.info(
                "[MA] %s: raw=%.1f, ma=%.1f (window=%d/%d)",
                code,
                current,
                ma,
                len(recent),
                window,
            )
    return ma_map


def select_with_hysteresis(
    ma_scores: dict[str, float],
    *,
    previous_codes: set[str] | None = None,
    entry_threshold: float = ENTRY_THRESHOLD,
    exit_threshold: float = EXIT_THRESHOLD,
) -> list[tuple[str, float]]:
    """MA 점수 + 히스테리시스 필터 → 선정 종목 (ma_score 내림차순).

    v2 `selection.py::select_watchlist` 의 히스테리시스 블록 포팅:
      - ma_score >= entry_threshold        → 신규 진입 허용
      - exit_threshold <= ma < entry AND 이전 선정에 있음 → 유지 (히스테리시스)
      - ma < exit_threshold AND 이전 선정에 있음          → 제거
      - 유지 구간(exit~entry) + 이전 선정에 없음          → 진입 불가 (skip)

    Args:
        ma_scores: {stock_code: ma_score}.
        previous_codes: 직전 run 에서 선정된 종목 코드. None 이면 히스테리시스
            미적용 — entry_threshold 만으로 필터 (cold start).
        entry_threshold: 신규 진입 최소 MA score.
        exit_threshold: 기존 종목 이탈 MA score.

    Returns:
        [(stock_code, ma_score)] — ma_score 내림차순 정렬.
    """
    survivors: list[tuple[str, float]] = []

    for code, ma in ma_scores.items():
        if previous_codes is None:
            # cold start — 직전 선정 정보 없음, 진입 임계만 적용
            if ma >= entry_threshold:
                survivors.append((code, ma))
            continue

        in_prev = code in previous_codes
        if ma >= entry_threshold:
            survivors.append((code, ma))
        elif ma >= exit_threshold and in_prev:
            logger.info(
                "[HYSTERESIS] %s: ma=%.1f, below entry(%.0f) above exit(%.0f), KEPT",
                code,
                ma,
                entry_threshold,
                exit_threshold,
            )
            survivors.append((code, ma))
        elif ma < exit_threshold and in_prev:
            logger.info(
                "[HYSTERESIS] %s: ma=%.1f, below exit(%.0f), REMOVED",
                code,
                ma,
                exit_threshold,
            )
        # else: 유지 구간 + 이전 선정에 없음 → 진입 불가 (skip)

    survivors.sort(key=lambda x: x[1], reverse=True)
    return survivors


__all__ = [
    "ENTRY_THRESHOLD",
    "EXIT_THRESHOLD",
    "MA_WINDOW",
    "compute_ma_scores",
    "select_with_hysteresis",
]
