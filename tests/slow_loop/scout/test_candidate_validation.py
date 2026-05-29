"""Scout 후보 검증 테스트 (S06, S07, S08)."""

from __future__ import annotations

from prime_jennie_runtime.slow_loop.scout.candidate_validation import validate_candidates
from prime_jennie_runtime.slow_loop.scout.schemas import EntryHint, ScreeningCandidate


def _cand(ticker: str, conviction: float = 0.5) -> ScreeningCandidate:
    return ScreeningCandidate(
        ticker=ticker,
        strategy_tag="SECTOR_MOMENTUM",
        conviction=conviction,
        entry_hint=EntryHint(trigger="market"),
    )


# =====================================================================
# S06: 21개+ 반환 → 상위 20만
# =====================================================================


def test_s06_truncates_to_top_20():
    universe = [f"{i:06d}" for i in range(25)]
    raw = [_cand(f"{i:06d}", conviction=i / 25) for i in range(25)]
    result = validate_candidates(raw, universe)
    assert len(result.candidates) == 20
    assert result.truncated_count == 5
    # 상위 20개는 conviction 큰 순
    assert result.candidates[0].conviction > result.candidates[-1].conviction


def test_exactly_20_not_truncated():
    universe = [f"{i:06d}" for i in range(20)]
    raw = [_cand(f"{i:06d}") for i in range(20)]
    result = validate_candidates(raw, universe)
    assert len(result.candidates) == 20
    assert result.truncated_count == 0


# =====================================================================
# S07: ticker 중복 제거
# =====================================================================


def test_s07_duplicate_removed_higher_conviction_kept():
    universe = ["005930"]
    raw = [
        _cand("005930", conviction=0.3),
        _cand("005930", conviction=0.8),
        _cand("005930", conviction=0.5),
    ]
    result = validate_candidates(raw, universe)
    assert len(result.candidates) == 1
    assert result.candidates[0].conviction == 0.8
    assert "005930" in result.duplicate_tickers_removed


# =====================================================================
# S08: universe 밖 ticker 환각
# =====================================================================


def test_s08_hallucination_30pct_warn():
    """3/10 universe 밖 = 30% → warn."""
    universe = [f"{i:06d}" for i in range(10)]
    raw = [_cand(f"{i:06d}") for i in range(7)] + [
        _cand("999996"),
        _cand("999997"),
        _cand("999998"),
    ]
    result = validate_candidates(raw, universe)
    assert result.hallucination_warn is True
    assert result.hallucination_fail is False
    assert len(result.hallucinated_tickers) == 3
    # 환각 ticker는 최종 candidates에서 제외
    assert all(c.ticker in universe for c in result.candidates)


def test_s08_hallucination_50pct_fail():
    """5/10 universe 밖 = 50% → fail."""
    universe = [f"{i:06d}" for i in range(10)]
    raw = [_cand(f"{i:06d}") for i in range(5)] + [
        _cand("999994"),
        _cand("999995"),
        _cand("999996"),
        _cand("999997"),
        _cand("999998"),
    ]
    result = validate_candidates(raw, universe)
    assert result.hallucination_fail is True
    assert result.hallucination_warn is False


def test_clean_input_no_flags():
    """모든 ticker가 universe 안, 중복/환각 없음."""
    universe = [f"{i:06d}" for i in range(5)]
    raw = [_cand(f"{i:06d}") for i in range(5)]
    result = validate_candidates(raw, universe)
    assert result.hallucination_warn is False
    assert result.hallucination_fail is False
    assert result.truncated_count == 0
    assert result.duplicate_tickers_removed == []
    assert len(result.candidates) == 5


def test_empty_input():
    """빈 입력은 안전하게 처리."""
    result = validate_candidates([], ["005930"])
    assert result.candidates == []
    assert result.hallucination_warn is False
