"""시장 운영 시간 유틸리티 — 거래일 + 장 시간 통합 체크.

Gateway 에서는 KIS API(CTCA0903R)로 거래일 확인, 다른 서비스는 HTTP API 경유.
거래일 결과는 일 단위 캐시 (하루 1회만 API 호출).

원본: prime_jennie/infra/market_hours.py (그대로 포팅, sync 유지)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

# 장 시간 상수 (HHMM 정수)
MARKET_OPEN = 900  # 09:00
MARKET_CLOSE = 1530  # 15:30
STREAM_START = 850  # 08:50 (장 개시 10분 전)
STREAM_END = 1535  # 15:35 (장 마감 5분 후)


class MarketCalendar:
    """거래일 + 장 시간 통합 확인.

    Usage (Gateway — KIS API 직접):
        cal = MarketCalendar(lambda d: kis_api.is_trading_day_sync(d))

    Usage (Scanner/Monitor — Gateway HTTP):
        cal = MarketCalendar(lambda d: _check_via_gateway(d))
    """

    def __init__(self, trading_day_checker: Callable[[date_cls], bool] | None = None):
        self._checker = trading_day_checker
        self._cache: dict[str, bool] = {}
        # 외부 (KIS chk-holiday) 판정으로 채워진 날짜 — "평일=거래일" 가정 캐시와 구분.
        self._primed: set[str] = set()
        self._now: Callable[[], datetime] = lambda: datetime.now(_KST)

    def _clock_now(self) -> datetime:
        return self._now()

    def set_clock(self, now_fn: Callable[[], datetime]) -> None:
        """테스트 전용 — 현재 시각 주입 (KST 기준 datetime 반환 필요)."""
        self._now = now_fn

    def is_trading_day(self, target: date_cls | None = None) -> bool:
        """거래일 여부 (일 단위 캐시)."""
        now = self._clock_now()
        d = target or now.date()
        key = d.isoformat()

        if key in self._cache:
            return self._cache[key]

        # 주말은 API 호출 없이 바로 False
        if d.weekday() >= 5:
            self._cache[key] = False
            return False

        if self._checker:
            try:
                result = self._checker(d)
                self._cache[key] = result
                if not result:
                    logger.info("Non-trading day (holiday): %s", key)
                return result
            except Exception:
                logger.warning("Trading day check failed for %s, assuming trading day", key)
                return True

        # 체커 없으면 평일=거래일 가정
        self._cache[key] = True
        return True

    def today_kst(self) -> date_cls:
        """현재 시각 (주입 가능한 clock 기준) 의 KST 날짜."""
        return self._clock_now().date()

    def prime_trading_day(self, d: date_cls, is_trading: bool) -> None:
        """외부 (KIS chk-holiday 등) 에서 판정한 거래일 여부를 주입.

        체커가 없을 때 "평일=거래일" 가정으로 캐시된 값보다 우선한다 — 선거일·임시공휴일
        같은 KIS 휴장일을 달력이 모르는 채 'regular' 를 반환하던 문제의 보완 경로
        (2026-06-03 지방선거일 실측).
        """
        self._cache[d.isoformat()] = is_trading
        self._primed.add(d.isoformat())
        if not is_trading:
            logger.info("Non-trading day primed (external): %s", d.isoformat())

    def needs_external_trading_day(self, d: date_cls) -> bool:
        """KIS chk-holiday 외부 판정이 필요한지.

        체커가 주입돼 있거나, 이미 외부 판정으로 채워졌거나, 주말이면 불필요.
        """
        if self._checker is not None:
            return False
        if d.isoformat() in self._primed:
            return False
        return d.weekday() < 5

    def is_market_open(self) -> tuple[bool, str]:
        """장 운영 상태 반환 (is_open, session).

        Returns:
            (True, "regular") — 정규장 09:00~15:30
            (True, "pre_opening") — 동시호가 09:00~09:30
            (True, "closing") — 장 마감 동시호가 15:30~16:00
            (False, "holiday") — 공휴일/주말
            (False, "pre_market") — 장 개시 전
            (False, "after_hours") — 장 종료 후
        """
        now = self._clock_now()

        if not self.is_trading_day(now.date()):
            return False, "holiday"

        t = now.hour * 100 + now.minute

        if t < MARKET_OPEN:
            return False, "pre_market"
        if t < 930:
            return True, "pre_opening"
        if t < MARKET_CLOSE:
            return True, "regular"
        if t < 1600:
            return True, "closing"
        return False, "after_hours"

    def is_streaming_hours(self) -> bool:
        """스트리밍 시간 여부 (08:50~15:35, 거래일만)."""
        now = self._clock_now()

        if not self.is_trading_day(now.date()):
            return False

        t = now.hour * 100 + now.minute
        return STREAM_START <= t <= STREAM_END
