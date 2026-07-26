"""TriggerWatcher 테스트 — KOSPI/VIX/FX 임계 도달 시 ad-hoc 인보커 호출.

MACRO_GATE_SPEC §1.3 자동 트리거 구현 검증.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from prime_jennie_runtime.slow_loop.macro.trigger_watcher import (
    DEBOUNCE_KEY_PREFIX,
    DEBOUNCE_TTL_SEC,
    FX_REASON,
    KOSPI_REASON,
    MACRO_SNAPSHOT_KEY_PREFIX,
    VIX_REASON,
    TriggerWatcher,
    evaluate_triggers,
    make_invoker,
)


class _RecorderObserver:
    """Observer mock — emit 호출 기록."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _trigger_names(observer: _RecorderObserver) -> list[str]:
    return [e.name for e in observer.events]


async def _seed_snapshot(redis_client, day, **fields: Any) -> None:
    key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{day.isoformat()}"
    await redis_client.set(key, json.dumps(fields))


# =====================================================================
# evaluate_triggers (pure function)
# =====================================================================


def test_evaluate_no_triggers_when_calm():
    triggers = evaluate_triggers(
        {"kospi_change_pct": -0.5, "vix": 18.0, "usd_krw": 1350.0},
        {"usd_krw": 1348.0},
    )
    assert triggers == []


def test_evaluate_kospi_drop_exactly_3pct_triggers():
    """경계값: 정확히 -3% 도 트리거 (<=)."""
    triggers = evaluate_triggers({"kospi_change_pct": -3.0}, None)
    assert len(triggers) == 1
    assert triggers[0].reason == KOSPI_REASON


def test_evaluate_kospi_drop_below_threshold_no_trigger():
    triggers = evaluate_triggers({"kospi_change_pct": -2.99}, None)
    assert triggers == []


def test_evaluate_vix_spike_exactly_30_triggers():
    triggers = evaluate_triggers({"vix": 30.0}, None)
    assert len(triggers) == 1
    assert triggers[0].reason == VIX_REASON


def test_evaluate_vix_below_threshold_no_trigger():
    triggers = evaluate_triggers({"vix": 29.99}, None)
    assert triggers == []


def test_evaluate_fx_shock_positive_triggers():
    """USD/KRW 1350 → 1371 = +1.555% — 트리거."""
    triggers = evaluate_triggers({"usd_krw": 1371.0}, {"usd_krw": 1350.0})
    assert len(triggers) == 1
    assert triggers[0].reason == FX_REASON
    assert triggers[0].value > 1.5


def test_evaluate_fx_shock_negative_triggers():
    """USD/KRW 1350 → 1329 = -1.555% — 트리거."""
    triggers = evaluate_triggers({"usd_krw": 1329.0}, {"usd_krw": 1350.0})
    assert len(triggers) == 1
    assert triggers[0].reason == FX_REASON
    assert triggers[0].value < -1.5


def test_evaluate_fx_below_threshold_no_trigger():
    """USD/KRW 1.4% 변동 — 트리거 X."""
    triggers = evaluate_triggers({"usd_krw": 1369.0}, {"usd_krw": 1350.0})
    # 1369/1350 - 1 = 1.407% < 1.5
    assert triggers == []


def test_evaluate_fx_no_prior_no_trigger():
    """전일 snapshot 없으면 FX 계산 자체 skip — fail-open."""
    triggers = evaluate_triggers({"usd_krw": 1500.0}, None)
    assert triggers == []


def test_evaluate_multiple_triggers_all_returned():
    """KOSPI + VIX + FX 동시 — 3 개 모두."""
    triggers = evaluate_triggers(
        {"kospi_change_pct": -3.5, "vix": 35.0, "usd_krw": 1380.0},
        {"usd_krw": 1350.0},
    )
    reasons = sorted(t.reason for t in triggers)
    assert reasons == sorted([KOSPI_REASON, VIX_REASON, FX_REASON])


def test_evaluate_missing_fields_no_crash():
    """필드 누락은 fail-open (트리거 X)."""
    triggers = evaluate_triggers({}, None)
    assert triggers == []


def test_evaluate_invalid_value_types_no_crash():
    """잘못된 타입(문자열 등) 은 fail-open."""
    triggers = evaluate_triggers(
        {"kospi_change_pct": "abc", "vix": None, "usd_krw": "xyz"},
        {"usd_krw": "abc"},
    )
    assert triggers == []


# =====================================================================
# TriggerWatcher (Redis 통합)
# =====================================================================


def _fixed_clock(dt: datetime):
    return lambda: dt


@pytest.mark.asyncio
async def test_process_once_no_snapshot_returns_empty(fake_redis):
    """snapshot 자체가 없으면 평가 결과 빈 리스트."""
    invoked: list[str] = []

    async def _invoker(reason: str) -> None:
        invoked.append(reason)

    obs = _RecorderObserver()
    watcher = TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=obs,
        clock=_fixed_clock(datetime(2026, 5, 11, 13, 30)),
    )
    fired = await watcher.process_once()
    assert fired == []
    assert invoked == []


@pytest.mark.asyncio
async def test_process_once_fires_invoker_on_kospi_drop(fake_redis):
    """KOSPI -3.2% snapshot → invoker 호출 + fire 이벤트."""
    now = datetime(2026, 5, 11, 13, 30)
    await _seed_snapshot(fake_redis, now.date(), kospi_change_pct=-3.2, vix=22.0, usd_krw=1350.0)

    invoked: list[str] = []

    async def _invoker(reason: str) -> None:
        invoked.append(reason)

    obs = _RecorderObserver()
    watcher = TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=obs,
        clock=_fixed_clock(now),
    )
    fired = await watcher.process_once()
    assert [s.reason for s in fired] == [KOSPI_REASON]
    assert invoked == [KOSPI_REASON]
    assert "pj.macro.trigger_fired" in _trigger_names(obs)


@pytest.mark.asyncio
async def test_process_once_debounces_same_reason(fake_redis):
    """같은 reason 으로 두 번째 process_once 는 invoker 안 부른다."""
    now = datetime(2026, 5, 11, 13, 30)
    await _seed_snapshot(fake_redis, now.date(), vix=32.0)

    invoked: list[str] = []

    async def _invoker(reason: str) -> None:
        invoked.append(reason)

    obs = _RecorderObserver()
    watcher = TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=obs,
        clock=_fixed_clock(now),
    )
    fired1 = await watcher.process_once()
    assert [s.reason for s in fired1] == [VIX_REASON]
    assert invoked == [VIX_REASON]

    # 2 회 cycle — debounce 키 유효, invoker 추가 호출 없음
    fired2 = await watcher.process_once()
    assert fired2 == []
    assert invoked == [VIX_REASON]
    assert "pj.macro.trigger_debounced" in _trigger_names(obs)


@pytest.mark.asyncio
async def test_process_once_independent_reasons_both_fire(fake_redis):
    """다른 reason 두 개는 동일 cycle 에서 둘 다 트리거."""
    now = datetime(2026, 5, 11, 13, 30)
    await _seed_snapshot(
        fake_redis,
        now.date(),
        kospi_change_pct=-3.5,
        vix=35.0,
        usd_krw=1350.0,
    )

    invoked: list[str] = []

    async def _invoker(reason: str) -> None:
        invoked.append(reason)

    watcher = TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=_RecorderObserver(),
        clock=_fixed_clock(now),
    )
    fired = await watcher.process_once()
    assert sorted(s.reason for s in fired) == sorted([KOSPI_REASON, VIX_REASON])
    assert sorted(invoked) == sorted([KOSPI_REASON, VIX_REASON])


@pytest.mark.asyncio
async def test_process_once_fx_uses_prior_day_snapshot(fake_redis):
    """전일 snapshot 의 usd_krw 와 비교해 FX 트리거 산출."""
    now = datetime(2026, 5, 11, 13, 30)
    yesterday = now.date() - timedelta(days=1)
    await _seed_snapshot(fake_redis, yesterday, usd_krw=1350.0)
    await _seed_snapshot(fake_redis, now.date(), usd_krw=1372.0, kospi_change_pct=-1.0, vix=20.0)

    invoked: list[str] = []

    async def _invoker(reason: str) -> None:
        invoked.append(reason)

    watcher = TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=_RecorderObserver(),
        clock=_fixed_clock(now),
    )
    fired = await watcher.process_once()
    assert [s.reason for s in fired] == [FX_REASON]
    assert invoked == [FX_REASON]


@pytest.mark.asyncio
async def test_process_once_invoker_failure_keeps_debounce(fake_redis):
    """invoker 가 raise 해도 watcher 는 죽지 않음. debounce 키는 이미 set 됨."""
    now = datetime(2026, 5, 11, 13, 30)
    await _seed_snapshot(fake_redis, now.date(), kospi_change_pct=-4.0)

    calls = {"n": 0}

    async def _invoker(reason: str) -> None:
        calls["n"] += 1
        raise RuntimeError("simulated downstream failure")

    watcher = TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=_RecorderObserver(),
        clock=_fixed_clock(now),
    )
    # 첫 cycle: invoker raise → swallow, fired 빈 리스트 (실패는 success 로 안 침)
    fired = await watcher.process_once()
    assert fired == []
    assert calls["n"] == 1

    # 두 번째 cycle: 같은 reason 은 debounce 적용 — invoker 추가 호출 없음
    fired2 = await watcher.process_once()
    assert fired2 == []
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_debounce_ttl_set_correctly(fake_redis):
    """debounce 키의 TTL 이 1800 초 (30 분) 로 설정됨."""
    now = datetime(2026, 5, 11, 13, 30)
    await _seed_snapshot(fake_redis, now.date(), vix=31.0)

    async def _invoker(reason: str) -> None:
        return None

    watcher = TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=_RecorderObserver(),
        clock=_fixed_clock(now),
    )
    await watcher.process_once()
    ttl = await fake_redis.ttl(f"{DEBOUNCE_KEY_PREFIX}{VIX_REASON}")
    # fakeredis 는 정확한 TTL 을 돌려준다. 살짝 변동 가능성 대비 범위 체크.
    assert 0 < ttl <= DEBOUNCE_TTL_SEC


@pytest.mark.asyncio
async def test_run_with_max_iterations_returns(fake_redis):
    """max_iterations=2 이면 2 회 cycle 후 정상 종료 (sleep 단축은 interval=0)."""
    now = datetime(2026, 5, 11, 13, 30)
    await _seed_snapshot(fake_redis, now.date(), vix=18.0)

    async def _invoker(reason: str) -> None:
        return None

    watcher = TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=_RecorderObserver(),
        clock=_fixed_clock(now),
        interval_sec=0,
    )
    iterations = await watcher.run(max_iterations=2)
    assert iterations == 2


# =====================================================================
# make_invoker
# =====================================================================


@pytest.mark.asyncio
async def test_make_invoker_passes_trigger_to_run_slow_loop():
    """make_invoker 가 macro_trigger 를 그대로 run_slow_loop 에 전달."""
    captured: dict[str, Any] = {}

    async def _fake_run_slow_loop(components, **kwargs) -> None:
        captured["components"] = components
        captured.update(kwargs)

    fixed_now = datetime(2026, 5, 11, 13, 30, 45)
    invoker = make_invoker(
        components="SENTINEL_COMP",
        run_slow_loop_fn=_fake_run_slow_loop,
        clock=lambda: fixed_now,
    )
    await invoker("ad_hoc_kospi_drop_3pct")

    assert captured["components"] == "SENTINEL_COMP"
    assert captured["macro_trigger"] == "ad_hoc_kospi_drop_3pct"
    assert captured["scout_trigger"] == "ad_hoc_kospi_drop_3pct"
    assert captured["as_of_date"] == fixed_now.date()
    assert captured["as_of_dt"] == fixed_now
    # macro_run_id / scout_run_id 는 timestamp + 8-hex suffix
    assert captured["macro_run_id"].startswith("mr_20260511_1330_")
    assert captured["scout_run_id"].startswith("sr_20260511_1330_")


# =====================================================================
# 거래시간 가드 (2026-07-26)
# =====================================================================
#
# 스냅샷이 하루치 키라 임계를 한 번 넘기면 그날 내내 조건이 참으로 남고, 30분 디바운스가
# 풀릴 때마다 장 마감 뒤에도 계속 재발화했다 (7월 573회 중 342회가 장외). 아래 테스트는
# 장외에는 평가 자체를 건너뛰고 디바운스도 소모하지 않는지 확인한다.


async def _drop_watcher(fake_redis, now: datetime, invoked: list[str], **kwargs):
    """KOSPI −3.2% 가 이미 깔린 상태의 watcher — 가드가 없으면 반드시 발화한다."""
    await _seed_snapshot(fake_redis, now.date(), kospi_change_pct=-3.2)

    async def _invoker(reason: str) -> None:
        invoked.append(reason)

    return TriggerWatcher(
        redis_client=fake_redis,
        invoker=_invoker,
        observer=_RecorderObserver(),
        clock=_fixed_clock(now),
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "label"),
    [
        (datetime(2026, 5, 11, 8, 59), "개장 1분 전"),
        (datetime(2026, 5, 11, 15, 31), "마감 1분 후"),
        (datetime(2026, 5, 11, 22, 0), "금요일 밤 유형"),
        (datetime(2026, 5, 11, 1, 0), "새벽"),
    ],
)
async def test_process_once_skips_outside_trading_hours(fake_redis, now, label):
    invoked: list[str] = []
    watcher = await _drop_watcher(fake_redis, now, invoked)
    assert await watcher.process_once() == [], label
    assert invoked == [], label


@pytest.mark.asyncio
@pytest.mark.parametrize("now", [datetime(2026, 5, 16, 13, 30), datetime(2026, 5, 17, 13, 30)])
async def test_process_once_skips_on_weekend(fake_redis, now):
    """토·일은 장중 시각이어도 발화하지 않는다 — 실제로 토요일 01:00 까지 돌았던 사례."""
    invoked: list[str] = []
    watcher = await _drop_watcher(fake_redis, now, invoked)
    assert await watcher.process_once() == []
    assert invoked == []


@pytest.mark.asyncio
async def test_process_once_skips_on_holiday(fake_redis):
    """평일 장중이어도 휴장일이면 건너뛴다."""
    invoked: list[str] = []

    async def _closed() -> bool:
        return False

    watcher = await _drop_watcher(
        fake_redis, datetime(2026, 5, 11, 13, 30), invoked, trading_day_check=_closed
    )
    assert await watcher.process_once() == []
    assert invoked == []


@pytest.mark.asyncio
async def test_process_once_fires_on_trading_day_within_window(fake_redis):
    """장중 거래일이면 그대로 발화한다 — 가드가 정상 경로를 막지 않는지."""
    invoked: list[str] = []

    async def _open() -> bool:
        return True

    watcher = await _drop_watcher(
        fake_redis, datetime(2026, 5, 11, 13, 30), invoked, trading_day_check=_open
    )
    assert [s.reason for s in await watcher.process_once()] == [KOSPI_REASON]
    assert invoked == [KOSPI_REASON]


@pytest.mark.asyncio
async def test_holiday_check_failure_assumes_trading_day(fake_redis):
    """휴장일 판정이 터지면 거래일로 가정한다 — gateway 장애가 급변 감지를 죽이면 안 된다."""
    invoked: list[str] = []

    async def _boom() -> bool:
        raise RuntimeError("gateway down")

    watcher = await _drop_watcher(
        fake_redis, datetime(2026, 5, 11, 13, 30), invoked, trading_day_check=_boom
    )
    assert [s.reason for s in await watcher.process_once()] == [KOSPI_REASON]


@pytest.mark.asyncio
async def test_off_hours_skip_does_not_consume_debounce(fake_redis):
    """장외 skip 은 디바운스를 쓰지 않는다 — 다음 개장 때 곧바로 한 번 발화해야 한다."""
    invoked: list[str] = []
    friday_night = datetime(2026, 5, 15, 22, 0)
    watcher = await _drop_watcher(fake_redis, friday_night, invoked)
    assert await watcher.process_once() == []
    assert await fake_redis.get(f"{DEBOUNCE_KEY_PREFIX}{KOSPI_REASON}") is None

    # 같은 스냅샷 그대로 장중 시각으로 시계만 옮기면 곧바로 발화한다.
    watcher.clock = _fixed_clock(datetime(2026, 5, 15, 13, 30))
    assert [s.reason for s in await watcher.process_once()] == [KOSPI_REASON]
    assert invoked == [KOSPI_REASON]
