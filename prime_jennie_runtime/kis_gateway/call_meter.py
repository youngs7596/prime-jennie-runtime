"""KIS 로 실제 나가는 호출의 초당 빈도 계측기.

KIS 는 앱키(계정) 단위로 모든 호출을 합산해 초당 한도를 본다. 시세/매매 분리
버킷과 전역 합산 리미터로 막고 있는데도 EGW00201 (초당 거래건수 초과) 이 나는지
확인하려면, 추측 대신 실제로 KIS 로 나간 호출 수를 재야 한다 (2026-06-05 요청).

``KISApi._send`` 한 군데가 모든 실호출(초기 + 재시도)의 길목이므로 거기서
``record`` 를 호출한다. 핸들러 레벨 리미터는 재시도를 못 세므로 부적합하다.

asyncio 단일 스레드에서만 쓰이고 모든 연산이 동기라 lock 없이 동작한다.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from collections.abc import Callable

logger = logging.getLogger(__name__)

# 스냅샷에 함께 노출할 관측 윈도우 (초). 트레일링 윈도우별 평균 초당 호출수.
_SNAPSHOT_WINDOWS = (1.0, 5.0, 10.0)


def classify_endpoint(path: str) -> str:
    """KIS 경로를 한도 그룹으로 분류. KIS 는 합산하지만 그룹별 분포도 같이 본다."""
    if "oauth" in path or "tokenP" in path:
        return "auth"
    if "/quotations/" in path:
        return "시세"
    if "/trading/" in path:
        return "매매"
    return "기타"


class KisCallMeter:
    """KIS 실호출의 초당 빈도 계측.

    - ``record(path)``: 실제 KIS 로 나간 호출 1건 기록 (재시도도 1건)
    - ``record_throttle(path)``: 그 호출이 EGW00201 로 거부됨
    - ``snapshot()``: 현재 초당 호출수 (총합 + 그룹별), 피크, 누적, throttle 수

    ``warn_per_sec`` 이상이 트레일링 1초에 몰리면 WARNING 로그를 남긴다 (초당 1회로
    제한). 전역 리미터가 한 윈도우 최대 14/sec 로 묶으므로, 그보다 높은 값이 찍히면
    리미터 우회나 외부 합산을 의심할 수 있다.
    """

    def __init__(
        self,
        *,
        history_sec: float = 60.0,
        warn_per_sec: int = 0,
        clock: Callable[[], float] | None = None,
    ):
        self._history_sec = history_sec
        self._warn_per_sec = warn_per_sec
        self._clock = clock or time.monotonic
        # (timestamp, group) — history_sec 보다 오래된 항목은 prune
        self._events: deque[tuple[float, str]] = deque()
        self._total: Counter[str] = Counter()
        self._throttles: Counter[str] = Counter()
        self._peak_rate: int = 0
        self._peak_at: float | None = None
        self._last_warn: float = float("-inf")
        self._started_at: float = self._clock()

    def record(self, path: str) -> None:
        """KIS 로 나간 실호출 1건 기록."""
        now = self._clock()
        group = classify_endpoint(path)
        self._events.append((now, group))
        self._total[group] += 1
        self._prune(now)

        rate_1s = self._count_within(now, 1.0)
        if rate_1s > self._peak_rate:
            self._peak_rate = rate_1s
            self._peak_at = now

        if self._warn_per_sec and rate_1s >= self._warn_per_sec and now - self._last_warn >= 1.0:
            self._last_warn = now
            logger.warning(
                "KIS 초당 호출수 %d 건 — 임계 %d 도달 (그룹별 1s: %s)",
                rate_1s,
                self._warn_per_sec,
                dict(self._group_counts_within(now, 1.0)),
            )

    def record_throttle(self, path: str) -> None:
        """EGW00201 로 거부된 호출 기록."""
        self._throttles[classify_endpoint(path)] += 1

    def _prune(self, now: float) -> None:
        cutoff = now - self._history_sec
        events = self._events
        while events and events[0][0] < cutoff:
            events.popleft()

    def _count_within(self, now: float, window_sec: float) -> int:
        cutoff = now - window_sec
        return sum(1 for ts, _ in self._events if ts >= cutoff)

    def _group_counts_within(self, now: float, window_sec: float) -> Counter[str]:
        cutoff = now - window_sec
        counts: Counter[str] = Counter()
        for ts, group in self._events:
            if ts >= cutoff:
                counts[group] += 1
        return counts

    def snapshot(self) -> dict:
        """현재 계측 상태. 모니터링 엔드포인트가 그대로 반환."""
        now = self._clock()
        self._prune(now)

        rates: dict[str, dict] = {}
        for window in _SNAPSHOT_WINDOWS:
            total = self._count_within(now, window)
            groups = self._group_counts_within(now, window)
            rates[f"{window:g}s"] = {
                "calls": total,
                "per_sec": round(total / window, 2),
                "by_group": dict(groups),
            }

        return {
            "current": rates,
            "peak_1s": {
                "rate": self._peak_rate,
                "seconds_ago": round(now - self._peak_at, 1) if self._peak_at is not None else None,
            },
            "throttles": dict(self._throttles),
            "throttle_total": sum(self._throttles.values()),
            "calls_total": dict(self._total),
            "calls_total_sum": sum(self._total.values()),
            "uptime_sec": round(now - self._started_at, 1),
            "warn_per_sec": self._warn_per_sec,
        }
