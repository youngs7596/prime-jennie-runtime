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
        """섹터 모멘텀 중심화(2026-08-15)에 맞춘 눈금 — 바꾸려면 의도해서 바꿀 것.

        이탈만 2026-08-27 에 59 → 62 로 올렸다. 신규 진입이 조여진 뒤에도 이월분이
        빈자리를 메워 하루 시트가 안 줄어서, 이월분만 깎는 손잡이를 조인 것이다.
        """
        assert (ENTRY_THRESHOLD, EXIT_THRESHOLD) == (66.0, 62.0)
        assert ENTRY_THRESHOLD - EXIT_THRESHOLD == 4.0

    def test_carryover_between_59_and_62_now_removed(self):
        """2026-08-27 회귀 방지 — 옛 유지 구간(59~62)은 이제 이탈이다.

        이 구간 종목이 다시 유지되기 시작하면 문턱이 되돌려진 것이다.
        """
        result = select_with_hysteresis({"OLD_KEEP": 60.5}, previous_codes={"OLD_KEEP"})
        assert result == []

    def test_carryover_just_above_new_exit_still_kept(self):
        """새 문턱 바로 위(62.0)는 직전 선정에 있으면 유지 — 경계 포함."""
        result = select_with_hysteresis({"KEEP": 62.0}, previous_codes={"KEEP"})
        assert result == [("KEEP", 62.0)]

    def test_carryover_never_outranks_a_new_entry(self):
        """이월분은 항상 진입권 아래 — 상한 20 자리를 새 후보와 다투지 않는다.

        2026-08-27 계산의 전제다. 이월분(MA < 진입)이 진입권(MA >= 진입)보다 위로
        정렬되면 문턱을 올릴 때 새 후보가 밀려나 건수가 안 줄어든다.
        """
        ma = {"HELD": 65.9, "ENTER_LOW": 66.0, "ENTER_HIGH": 80.0}
        result = select_with_hysteresis(ma, previous_codes={"HELD"})
        codes = [c for c, _ in result]
        assert codes == ["ENTER_HIGH", "ENTER_LOW", "HELD"]
