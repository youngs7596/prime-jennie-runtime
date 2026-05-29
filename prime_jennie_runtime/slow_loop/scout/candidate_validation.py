"""Scout 후보 검증 (결정론).

결정론 스코어러가 낸 candidates 를 정제·검증한다. 후보 출처와 무관한 일반
검증 로직 — 2026-05-29 LLM-at-core 잔재 정리 때 validators.py 에서 옮겨 왔다.
- S06: 21개+ 반환 → 상위 20개만 취함
- S07: ticker 중복 → conviction 높은 것 유지
- S08: universe 밖 ticker가 30% 이상이면 경고, 50% 이상이면 실패
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import ScreeningCandidate

MAX_CANDIDATES = 20
HALLUCINATION_WARN_THRESHOLD = 0.30
HALLUCINATION_FAIL_THRESHOLD = 0.50


@dataclass(frozen=True)
class ScoutValidationResult:
    """검증 후 최종 candidates + 경고/실패 플래그."""

    candidates: list[ScreeningCandidate]
    truncated_count: int = 0
    duplicate_tickers_removed: list[str] = field(default_factory=list)
    hallucinated_tickers: list[str] = field(default_factory=list)
    hallucination_warn: bool = False
    hallucination_fail: bool = False


def validate_candidates(
    raw: list[ScreeningCandidate],
    universe: list[str],
) -> ScoutValidationResult:
    """Scout 코드가 반환한 candidates를 정제 + 검증."""
    universe_set = set(universe)

    # 1. universe 필터 먼저 (환각 카운트용). 제거 이전의 총 개수로 비율 계산.
    total_before_filter = len(raw)
    outside_universe = [c for c in raw if c.ticker not in universe_set]
    inside_universe = [c for c in raw if c.ticker in universe_set]

    hallucinated = [c.ticker for c in outside_universe]
    hallucination_ratio = (
        len(outside_universe) / total_before_filter if total_before_filter else 0.0
    )

    # 2. 중복 제거 — 같은 ticker면 conviction 높은 것 유지 (S07)
    by_ticker: dict[str, ScreeningCandidate] = {}
    removed_dupes: list[str] = []
    for c in inside_universe:
        if c.ticker in by_ticker:
            existing = by_ticker[c.ticker]
            if c.conviction > existing.conviction:
                by_ticker[c.ticker] = c
            removed_dupes.append(c.ticker)
        else:
            by_ticker[c.ticker] = c
    deduped = list(by_ticker.values())

    # 3. 상위 MAX_CANDIDATES로 잘라냄 (S06, conviction 내림차순)
    deduped.sort(key=lambda c: c.conviction, reverse=True)
    truncated = max(0, len(deduped) - MAX_CANDIDATES)
    final = deduped[:MAX_CANDIDATES]

    return ScoutValidationResult(
        candidates=final,
        truncated_count=truncated,
        duplicate_tickers_removed=sorted(set(removed_dupes)),
        hallucinated_tickers=hallucinated,
        hallucination_warn=hallucination_ratio >= HALLUCINATION_WARN_THRESHOLD
        and hallucination_ratio < HALLUCINATION_FAIL_THRESHOLD,
        hallucination_fail=hallucination_ratio >= HALLUCINATION_FAIL_THRESHOLD,
    )
