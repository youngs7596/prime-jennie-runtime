"""IntradayRiskThrottle 테스트.

Covers:
  - 5단계 전이 (KOSPI/VIX 조합 parametrize)
  - SOX 하한선 강제 상향
  - 회복 지연 (CAUTION 5분, WARNING 10분, DANGER 20분, CRITICAL 30분)
  - 단계 건너뛰기 금지 (회복은 한 단계씩)
  - 악화는 즉시 + 회복 타이머 리셋
  - min() 역전 방지
  - RiskThrottleSnapshot Protocol 호환
  - Redis 상태 저장/로드
  - risk_updater.run_risk_updater 기본 시나리오
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.fast_loop.risk_throttle import (
    LEVEL_MULTIPLIER,
    REDIS_LEVEL_KEY,
    REDIS_RECOVERY_KEY,
    IntradayRiskThrottle,
    apply_sox_floor,
    calc_intraday_multiplier,
)
from prime_jennie_runtime.fast_loop.risk_updater import run_risk_updater
from prime_jennie_runtime.slow_loop.strategy.risk_throttle import RiskThrottleSnapshot

# ─── 헬퍼: 고정 시각 clock ────────────────────────────────────────


class _FrozenClock:
    """테스트용 clock — advance(seconds)로 진행."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return _FrozenClock(datetime(2026, 4, 17, 9, 0, 0))


# =====================================================================
# 1. 순수함수: calc_intraday_multiplier 5단계 전이표
# =====================================================================


@pytest.mark.parametrize(
    ("kospi_pct", "vix", "expected_level", "expected_mult"),
    [
        # NORMAL
        (0.5, 20.0, "NORMAL", 1.0),
        (-0.5, 29.9, "NORMAL", 1.0),
        # CAUTION — KOSPI -1% 또는 VIX 30
        (-1.0, 20.0, "CAUTION", 0.8),
        (0.0, 30.0, "CAUTION", 0.8),
        (-1.5, 29.0, "CAUTION", 0.8),
        # WARNING — KOSPI -2% 또는 VIX 35
        (-2.0, 20.0, "WARNING", 0.6),
        (0.0, 35.0, "WARNING", 0.6),
        (-2.5, 34.0, "WARNING", 0.6),
        # DANGER — KOSPI -3% 또는 VIX 40
        (-3.0, 20.0, "DANGER", 0.3),
        (0.0, 40.0, "DANGER", 0.3),
        (-3.5, 38.0, "DANGER", 0.3),
        # CRITICAL — KOSPI -4%
        (-4.0, 20.0, "CRITICAL", 0.0),
        (-5.0, 50.0, "CRITICAL", 0.0),
    ],
)
def test_calc_intraday_multiplier_transitions(kospi_pct, vix, expected_level, expected_mult):
    level, mult = calc_intraday_multiplier(kospi_pct, vix)
    assert level == expected_level
    assert mult == expected_mult


# =====================================================================
# 2. SOX 하한선
# =====================================================================


@pytest.mark.parametrize(
    ("raw_level", "sox_pct", "expected"),
    [
        # sox -2.5% → CAUTION 하한선. NORMAL보다 심각하므로 상향
        ("NORMAL", -2.5, "CAUTION"),
        # sox -3.5% → WARNING 하한선. NORMAL → WARNING
        ("NORMAL", -3.5, "WARNING"),
        ("CAUTION", -3.5, "WARNING"),
        # raw가 이미 더 심각 → 그대로
        ("DANGER", -2.5, "DANGER"),
        ("CRITICAL", -3.5, "CRITICAL"),
        # sox 양수/None → 영향 없음
        ("NORMAL", 1.5, "NORMAL"),
        ("NORMAL", None, "NORMAL"),
    ],
)
def test_apply_sox_floor(raw_level, sox_pct, expected):
    assert apply_sox_floor(raw_level, sox_pct) == expected


@pytest.mark.asyncio
async def test_sox_floor_through_update(fake_redis, clock):
    """SOX -2.5%이면 KOSPI/VIX가 NORMAL이어도 CAUTION 강제."""
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    result = await throttle.update(
        kospi_pct=0.0,
        vix=20.0,
        sox_pct=-2.5,
    )
    assert result.level == "CAUTION"
    assert result.intraday_multiplier == 0.8


# =====================================================================
# 3. 즉시 악화 + 회복 타이머 리셋
# =====================================================================


@pytest.mark.asyncio
async def test_immediate_deterioration(fake_redis, clock):
    """NORMAL → CRITICAL 즉시 반영."""
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)

    result = await throttle.update(kospi_pct=0.0, vix=20.0)
    assert result.level == "NORMAL"

    # 급락
    result = await throttle.update(kospi_pct=-5.0, vix=50.0)
    assert result.level == "CRITICAL"
    assert result.intraday_multiplier == 0.0
    assert result.level_changed is True


@pytest.mark.asyncio
async def test_deterioration_resets_recovery_timer(fake_redis, clock):
    """회복 중 다시 악화되면 recovery_start 삭제."""
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)

    # CAUTION 진입
    await throttle.update(kospi_pct=-1.5, vix=20.0)
    # 개선 시도 → 회복 타이머 시작
    await throttle.update(kospi_pct=0.5, vix=20.0)
    assert await fake_redis.get(REDIS_RECOVERY_KEY) is not None

    # 다시 악화 → 타이머 삭제
    await throttle.update(kospi_pct=-2.5, vix=20.0)
    assert await fake_redis.get(REDIS_RECOVERY_KEY) is None


# =====================================================================
# 4. 회복 지연 (CAUTION → NORMAL 5분)
# =====================================================================


@pytest.mark.asyncio
async def test_caution_recovery_5min(fake_redis, clock):
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    # CAUTION 진입
    r = await throttle.update(kospi_pct=-1.5, vix=20.0)
    assert r.level == "CAUTION"

    # 즉시 개선 — 아직 CAUTION (타이머 시작)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "CAUTION"

    # 4분 경과 — 아직 CAUTION
    clock.advance(4 * 60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "CAUTION"

    # 추가 1분 경과 (총 5분) — NORMAL
    clock.advance(60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "NORMAL"
    assert r.intraday_multiplier == 1.0


@pytest.mark.asyncio
async def test_warning_recovery_10min(fake_redis, clock):
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    await throttle.update(kospi_pct=-2.5, vix=20.0)  # WARNING

    # 개선 시도 — 아직 WARNING
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "WARNING"

    # 9분 → 아직
    clock.advance(9 * 60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "WARNING"

    # 10분 — CAUTION (한 단계만)
    clock.advance(60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "CAUTION"


@pytest.mark.asyncio
async def test_danger_recovery_20min(fake_redis, clock):
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    await throttle.update(kospi_pct=-3.5, vix=20.0)  # DANGER

    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "DANGER"

    clock.advance(19 * 60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "DANGER"

    clock.advance(60)  # 총 20분
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "WARNING"


@pytest.mark.asyncio
async def test_critical_recovery_30min(fake_redis, clock):
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    await throttle.update(kospi_pct=-5.0, vix=20.0)  # CRITICAL

    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "CRITICAL"

    clock.advance(29 * 60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "CRITICAL"

    clock.advance(60)  # 총 30분
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "DANGER"


# =====================================================================
# 5. 단계 건너뛰기 금지
# =====================================================================


@pytest.mark.asyncio
async def test_no_skip_levels_critical_to_normal(fake_redis, clock):
    """CRITICAL에서 시장이 정상화되어도 한 번에 NORMAL로 못 감.

    CRITICAL → DANGER (30분) → WARNING (20분) → CAUTION (10분) → NORMAL (5분) 순서.

    각 단계에서 첫 번째 recovery 호출은 타이머 시작만 담당하므로
    전체 회복 시퀀스에는 "recovery 시작" + "타이머 만료" 두 호출이 필요.
    """
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    await throttle.update(kospi_pct=-5.0, vix=20.0)  # CRITICAL

    # 회복 타이머 시작
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "CRITICAL"

    # 30분 후 → DANGER (첫 단계 회복. 타이머 리셋)
    clock.advance(30 * 60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "DANGER"

    # 20분 경과 → WARNING (DANGER 회복 사이클)
    clock.advance(20 * 60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "WARNING"

    # 10분 경과 → CAUTION
    clock.advance(10 * 60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "CAUTION"

    # 5분 경과 → NORMAL
    clock.advance(5 * 60)
    r = await throttle.update(kospi_pct=0.5, vix=20.0)
    assert r.level == "NORMAL"


# =====================================================================
# 7. RiskThrottleSnapshot Protocol 호환
# =====================================================================


@pytest.mark.asyncio
async def test_implements_risk_throttle_snapshot_protocol(fake_redis, clock):
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    # 생성 직후에도 Protocol 만족 (current_multiplier 기본 1.0)
    assert isinstance(throttle, RiskThrottleSnapshot)
    assert throttle.current_multiplier() == 1.0

    # update 후 multiplier 갱신
    await throttle.update(kospi_pct=-2.5, vix=20.0)
    assert throttle.current_multiplier() == 0.6  # WARNING


# =====================================================================
# 8. Redis 상태 저장 + load_state
# =====================================================================


@pytest.mark.asyncio
async def test_redis_state_persists_across_instances(fake_redis, clock):
    """update 후 새 인스턴스로 생성해도 load_state로 이전 level 복원."""
    t1 = IntradayRiskThrottle(fake_redis, clock=clock)
    await t1.update(kospi_pct=-2.5, vix=20.0)
    assert t1.current_level == "WARNING"

    # 새 인스턴스 — 아직 load 전
    t2 = IntradayRiskThrottle(fake_redis, clock=clock)
    assert t2.current_level == "NORMAL"
    await t2.load_state()
    assert t2.current_level == "WARNING"
    assert t2.current_multiplier() == 0.6


@pytest.mark.asyncio
async def test_redis_level_key_written(fake_redis, clock):
    """update 후 REDIS_LEVEL_KEY에 level 문자열 저장."""
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    await throttle.update(kospi_pct=-3.5, vix=20.0)
    raw = await fake_redis.get(REDIS_LEVEL_KEY)
    assert raw is not None
    assert raw.decode() == "DANGER"


# =====================================================================
# 9. 경계 조건
# =====================================================================


@pytest.mark.asyncio
async def test_vix_none_treated_as_zero(fake_redis, clock):
    """vix=None → 0.0 취급 (오직 KOSPI만 반영)."""
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    r = await throttle.update(kospi_pct=-1.0, vix=None)
    assert r.level == "CAUTION"


@pytest.mark.asyncio
async def test_repeated_same_conditions_no_change(fake_redis, clock):
    """같은 악화 조건 반복 — level 유지, 회복 타이머도 없음."""
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    for _ in range(3):
        r = await throttle.update(kospi_pct=-2.5, vix=20.0)
        assert r.level == "WARNING"
    # 악화/동일이므로 recovery 키 없음
    assert await fake_redis.get(REDIS_RECOVERY_KEY) is None


@pytest.mark.asyncio
async def test_level_multiplier_mapping_consistent():
    """LEVEL_MULTIPLIER 상수 일관성 — 설계 스펙과 일치."""
    assert LEVEL_MULTIPLIER["NORMAL"] == 1.0
    assert LEVEL_MULTIPLIER["CAUTION"] == 0.8
    assert LEVEL_MULTIPLIER["WARNING"] == 0.6
    assert LEVEL_MULTIPLIER["DANGER"] == 0.3
    assert LEVEL_MULTIPLIER["CRITICAL"] == 0.0


# =====================================================================
# 10. risk_updater 루프
# =====================================================================


@pytest.mark.asyncio
async def test_run_risk_updater_single_iteration(fake_redis, clock):
    """run_risk_updater(max_iterations=1)로 한 번만 실행."""
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)

    async def fake_fetch():
        return (-2.5, 20.0, None)

    count = await run_risk_updater(
        throttle=throttle,
        fetch_snapshot=fake_fetch,
        interval_sec=0,
        notifier=None,
        clock=clock,
        max_iterations=1,
    )
    assert count == 1
    assert throttle.current_level == "WARNING"


@pytest.mark.asyncio
async def test_run_risk_updater_swallows_fetch_errors(fake_redis, clock):
    """fetch_snapshot 예외가 발생해도 iteration은 증가하고 계속 실행."""
    throttle = IntradayRiskThrottle(fake_redis, clock=clock)
    calls = {"n": 0}

    async def flaky_fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return (0.0, 20.0, None)

    count = await run_risk_updater(
        throttle=throttle,
        fetch_snapshot=flaky_fetch,
        interval_sec=0,
        notifier=None,
        clock=clock,
        max_iterations=2,
    )
    assert count == 2


@pytest.mark.asyncio
async def test_run_risk_updater_skips_after_hours(fake_redis):
    """장 마감 후(평일 18:00)엔 평가·알림을 건너뛴다 — fetch 미호출, 레벨 불변."""
    throttle = IntradayRiskThrottle(fake_redis)
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return (-3.0, 20.0, None)  # 평가됐다면 DANGER 로 갔을 값

    evening = _FrozenClock(datetime(2026, 6, 17, 18, 0, 0))  # Wed 18:00 KST, 마감 후
    count = await run_risk_updater(
        throttle=throttle,
        fetch_snapshot=fetch,
        interval_sec=0,
        off_hours_sleep_sec=0,
        notifier=None,
        clock=evening,
        max_iterations=1,
    )
    assert count == 1
    assert calls["n"] == 0  # 장외라 snapshot 자체를 안 읽음
    assert throttle.current_level == "NORMAL"  # 레벨 변경 없음


@pytest.mark.asyncio
async def test_run_risk_updater_skips_weekend(fake_redis):
    """주말(토요일 정오)엔 거래일이 아니므로 건너뛴다."""
    throttle = IntradayRiskThrottle(fake_redis)
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return (-3.0, 20.0, None)

    saturday = _FrozenClock(datetime(2026, 6, 20, 12, 0, 0))  # Sat noon
    count = await run_risk_updater(
        throttle=throttle,
        fetch_snapshot=fetch,
        interval_sec=0,
        off_hours_sleep_sec=0,
        notifier=None,
        clock=saturday,
        max_iterations=1,
    )
    assert count == 1
    assert calls["n"] == 0
    assert throttle.current_level == "NORMAL"
    assert throttle.current_level == "NORMAL"
