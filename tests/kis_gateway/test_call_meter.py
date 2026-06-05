"""KisCallMeter 테스트 — 초당 호출수 계측."""

from __future__ import annotations

from prime_jennie_runtime.kis_gateway.call_meter import KisCallMeter, classify_endpoint

_QUOTE = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
_BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"
_TOKEN = "/oauth2/tokenP"


class FakeClock:
    """단조 증가 가상 시계. tick() 로 시간 전진."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, delta: float) -> None:
        self.now += delta


def test_classify_endpoint():
    assert classify_endpoint(_QUOTE) == "시세"
    assert classify_endpoint(_BALANCE) == "매매"
    assert classify_endpoint(_TOKEN) == "auth"
    assert classify_endpoint("/uapi/etc/foo") == "기타"


def test_trailing_1s_rate_counts_only_recent():
    clock = FakeClock()
    meter = KisCallMeter(clock=clock)

    for _ in range(5):
        meter.record(_QUOTE)  # t=0 에 5건
    snap = meter.snapshot()
    assert snap["current"]["1s"]["calls"] == 5

    clock.tick(1.5)  # 1초 윈도우 밖으로 밀려남
    snap = meter.snapshot()
    assert snap["current"]["1s"]["calls"] == 0
    # 5s 윈도우엔 아직 남아있다
    assert snap["current"]["5s"]["calls"] == 5


def test_peak_rate_tracked():
    clock = FakeClock()
    meter = KisCallMeter(clock=clock)

    for _ in range(3):
        meter.record(_QUOTE)
    clock.tick(2.0)
    for _ in range(7):  # 더 높은 버스트
        meter.record(_BALANCE)

    snap = meter.snapshot()
    assert snap["peak_1s"]["rate"] == 7


def test_group_breakdown():
    clock = FakeClock()
    meter = KisCallMeter(clock=clock)

    meter.record(_QUOTE)
    meter.record(_QUOTE)
    meter.record(_BALANCE)

    snap = meter.snapshot()
    assert snap["current"]["1s"]["by_group"] == {"시세": 2, "매매": 1}
    assert snap["calls_total"] == {"시세": 2, "매매": 1}
    assert snap["calls_total_sum"] == 3


def test_throttle_count():
    clock = FakeClock()
    meter = KisCallMeter(clock=clock)

    meter.record(_BALANCE)
    meter.record_throttle(_BALANCE)
    meter.record(_QUOTE)
    meter.record_throttle(_QUOTE)
    meter.record_throttle(_QUOTE)

    snap = meter.snapshot()
    assert snap["throttles"] == {"매매": 1, "시세": 2}
    assert snap["throttle_total"] == 3


def test_warn_logged_when_threshold_reached(caplog):
    import logging

    clock = FakeClock()
    meter = KisCallMeter(warn_per_sec=3, clock=clock)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):  # 임계 3 도달
            meter.record(_QUOTE)

    assert any("초당 호출수" in r.message for r in caplog.records)


def test_warn_throttled_to_once_per_sec(caplog):
    import logging

    clock = FakeClock()
    meter = KisCallMeter(warn_per_sec=2, clock=clock)

    with caplog.at_level(logging.WARNING):
        meter.record(_QUOTE)
        meter.record(_QUOTE)  # 첫 경고
        meter.record(_QUOTE)  # 같은 초 — 억제
        warn_count_same_sec = sum("초당 호출수" in r.message for r in caplog.records)
        assert warn_count_same_sec == 1

        clock.tick(1.0)
        meter.record(_QUOTE)
        meter.record(_QUOTE)  # 1초 경과 후 다시 경고
    total = sum("초당 호출수" in r.message for r in caplog.records)
    assert total == 2


def test_history_pruned():
    clock = FakeClock()
    meter = KisCallMeter(history_sec=10.0, clock=clock)

    meter.record(_QUOTE)
    clock.tick(20.0)
    meter.record(_BALANCE)

    # 오래된 항목은 prune 됐지만 누적 카운트는 유지
    snap = meter.snapshot()
    assert snap["calls_total_sum"] == 2
    assert snap["current"]["10s"]["calls"] == 1
