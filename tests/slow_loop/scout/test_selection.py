"""결정론 선정 — MA 평활 + 히스테리시스 단위 테스트.

v2 `_compute_ma_scores` + `select_watchlist` 히스테리시스 블록 포팅분 검증.
"""

from __future__ import annotations

from prime_jennie_runtime.slow_loop.scout.selection import (
    ENTRY_THRESHOLD,
    EXIT_THRESHOLD,
    compute_ma_scores,
    select_with_hysteresis,
)

# ─── compute_ma_scores ───────────────────────────────────────────


class TestComputeMaScores:
    def test_no_history_returns_current(self):
        """과거 점수 없으면 MA = 현재 점수."""
        ma = compute_ma_scores({"005930": 70.0}, {}, window=3)
        assert ma == {"005930": 70.0}

    def test_averages_with_history(self):
        """과거 [60, 66] + 현재 72 → window=3 평균 = 66.0."""
        ma = compute_ma_scores({"005930": 72.0}, {"005930": [60.0, 66.0]}, window=3)
        assert ma["005930"] == 66.0

    def test_window_limits_lookback(self):
        """window=2 → 최근 2개(과거 마지막 1 + 현재)만 평균."""
        ma = compute_ma_scores({"A": 80.0}, {"A": [10.0, 20.0, 60.0]}, window=2)
        assert ma["A"] == 70.0  # (60 + 80) / 2

    def test_history_longer_than_window_truncated(self):
        ma = compute_ma_scores({"A": 90.0}, {"A": [10.0, 10.0, 10.0, 30.0]}, window=3)
        assert ma["A"] == round((10.0 + 30.0 + 90.0) / 3, 1)

    def test_multiple_tickers(self):
        ma = compute_ma_scores({"A": 70.0, "B": 50.0}, {"A": [80.0]}, window=2)
        assert ma["A"] == 75.0  # (80 + 70)/2
        assert ma["B"] == 50.0  # B 과거 없음


# ─── select_with_hysteresis ──────────────────────────────────────


class TestSelectWithHysteresis:
    def test_cold_start_entry_threshold_only(self):
        """previous_codes=None → entry_threshold 이상만, 정렬."""
        result = select_with_hysteresis(
            {"A": 70.0, "B": 60.0, "C": 65.0},
            previous_codes=None,
            entry_threshold=62.0,
            exit_threshold=55.0,
        )
        assert result == [("A", 70.0), ("C", 65.0)]  # B(60)<62 탈락, 내림차순

    def test_new_entry_above_entry_threshold(self):
        """이전 선정에 없어도 entry_threshold 이상이면 진입."""
        result = select_with_hysteresis(
            {"NEW": 65.0}, previous_codes=set(), entry_threshold=62.0, exit_threshold=55.0
        )
        assert result == [("NEW", 65.0)]

    def test_hold_band_kept_if_previously_selected(self):
        """exit~entry 구간 + 이전 선정에 있음 → 유지."""
        result = select_with_hysteresis(
            {"HELD": 58.0}, previous_codes={"HELD"}, entry_threshold=62.0, exit_threshold=55.0
        )
        assert result == [("HELD", 58.0)]

    def test_hold_band_skipped_if_not_previously_selected(self):
        """exit~entry 구간 + 이전 선정에 없음 → 진입 불가."""
        result = select_with_hysteresis(
            {"X": 58.0}, previous_codes=set(), entry_threshold=62.0, exit_threshold=55.0
        )
        assert result == []

    def test_below_exit_removed_even_if_previously_selected(self):
        """exit_threshold 미만 → 이전 선정에 있어도 제거."""
        result = select_with_hysteresis(
            {"DROP": 50.0}, previous_codes={"DROP"}, entry_threshold=62.0, exit_threshold=55.0
        )
        assert result == []

    def test_full_hysteresis_scenario(self):
        """진입/유지/제거/스킵 4종 동시 검증."""
        ma = {
            "ENTER": 70.0,  # 신규 진입 (>=62)
            "HELD": 57.0,  # 유지 (55~62, prev에 있음)
            "DROP": 52.0,  # 제거 (<55, prev에 있음)
            "SKIP": 58.0,  # 스킵 (55~62, prev에 없음)
        }
        result = select_with_hysteresis(
            ma, previous_codes={"HELD", "DROP"}, entry_threshold=62.0, exit_threshold=55.0
        )
        codes = [c for c, _ in result]
        assert codes == ["ENTER", "HELD"]  # ma 내림차순

    def test_result_sorted_by_ma_desc(self):
        result = select_with_hysteresis(
            {"LOW": 67.0, "HIGH": 90.0, "MID": 75.0}, previous_codes=None
        )
        assert [c for c, _ in result] == ["HIGH", "MID", "LOW"]

    def test_default_thresholds_are_the_centered_scale(self):
        """섹터 모멘텀 중심화(2026-08-15)에 맞춘 눈금 — 바꾸려면 의도해서 바꿀 것."""
        assert (ENTRY_THRESHOLD, EXIT_THRESHOLD) == (66.0, 59.0)
        assert ENTRY_THRESHOLD - EXIT_THRESHOLD == 7.0  # v2 히스테리시스 폭 유지
