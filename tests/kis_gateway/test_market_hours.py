"""market_hours.MarketCalendar 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prime_jennie_runtime.kis_gateway.market_hours import MarketCalendar

_KST = timezone(timedelta(hours=9))


def _clock(dt: datetime):
    return lambda: dt


def test_weekend_is_not_trading_day():
    cal = MarketCalendar()
    # 2026-04-18 (Saturday)
    saturday = datetime(2026, 4, 18, 10, 0, tzinfo=_KST)
    cal.set_clock(_clock(saturday))
    assert cal.is_trading_day() is False


def test_weekday_with_checker_false():
    cal = MarketCalendar(trading_day_checker=lambda _d: False)
    weekday = datetime(2026, 4, 17, 10, 0, tzinfo=_KST)
    cal.set_clock(_clock(weekday))
    assert cal.is_trading_day() is False


def test_weekday_with_checker_true_cached():
    calls: list[str] = []

    def checker(d):
        calls.append(d.isoformat())
        return True

    cal = MarketCalendar(trading_day_checker=checker)
    weekday = datetime(2026, 4, 17, 10, 0, tzinfo=_KST)
    cal.set_clock(_clock(weekday))
    assert cal.is_trading_day() is True
    assert cal.is_trading_day() is True
    assert len(calls) == 1  # 캐시 hit


def test_market_session_regular():
    cal = MarketCalendar(trading_day_checker=lambda _d: True)
    cal.set_clock(_clock(datetime(2026, 4, 17, 10, 0, tzinfo=_KST)))
    open_flag, session = cal.is_market_open()
    assert open_flag is True
    assert session == "regular"


def test_market_session_pre_market_and_after_hours():
    cal = MarketCalendar(trading_day_checker=lambda _d: True)
    cal.set_clock(_clock(datetime(2026, 4, 17, 8, 0, tzinfo=_KST)))
    assert cal.is_market_open() == (False, "pre_market")

    cal2 = MarketCalendar(trading_day_checker=lambda _d: True)
    cal2.set_clock(_clock(datetime(2026, 4, 17, 17, 0, tzinfo=_KST)))
    assert cal2.is_market_open() == (False, "after_hours")


def test_streaming_hours_boundaries():
    weekday = datetime(2026, 4, 17, 8, 55, tzinfo=_KST)
    cal = MarketCalendar(trading_day_checker=lambda _d: True)
    cal.set_clock(_clock(weekday))
    assert cal.is_streaming_hours() is True

    too_early = datetime(2026, 4, 17, 8, 40, tzinfo=_KST)
    cal2 = MarketCalendar(trading_day_checker=lambda _d: True)
    cal2.set_clock(_clock(too_early))
    assert cal2.is_streaming_hours() is False


def test_streaming_hours_holiday():
    cal = MarketCalendar(trading_day_checker=lambda _d: False)
    cal.set_clock(_clock(datetime(2026, 4, 17, 10, 0, tzinfo=_KST)))
    assert cal.is_streaming_hours() is False


def test_prime_trading_day_overrides_weekday_assumption():
    """외부 (KIS chk-holiday) 판정이 '평일=거래일' 가정 캐시를 덮어쓴다.

    2026-06-03 (수, 지방선거 휴장일) 사고: 체커 없는 calendar 가 'regular' 를 반환해
    휴장일 가드 전체가 무력했다. prime 후에는 holiday 로 정정되어야 한다.
    """
    cal = MarketCalendar()  # 체커 없음 (운영 gateway 와 동일)
    election_day = datetime(2026, 6, 3, 10, 0, tzinfo=_KST)
    cal.set_clock(_clock(election_day))

    # 가정 경로가 먼저 캐시를 채워도 (streamer 등이 먼저 조회한 상황),
    assert cal.is_trading_day() is True
    # 외부 판정 주입이 우선한다.
    cal.prime_trading_day(election_day.date(), False)
    assert cal.is_trading_day() is False
    assert cal.is_market_open() == (False, "holiday")


def test_needs_external_trading_day():
    weekday = datetime(2026, 6, 3, 10, 0, tzinfo=_KST)

    # 체커 없는 평일 — 외부 판정 필요.
    cal = MarketCalendar()
    cal.set_clock(_clock(weekday))
    assert cal.needs_external_trading_day(weekday.date()) is True

    # prime 후에는 불필요 (하루 1회만 KIS 조회).
    cal.prime_trading_day(weekday.date(), True)
    assert cal.needs_external_trading_day(weekday.date()) is False

    # 체커가 주입돼 있으면 불필요.
    cal2 = MarketCalendar(trading_day_checker=lambda _d: True)
    assert cal2.needs_external_trading_day(weekday.date()) is False

    # 주말은 불필요 (달력만으로 판정 가능).
    cal3 = MarketCalendar()
    saturday = datetime(2026, 6, 6, 10, 0, tzinfo=_KST)
    assert cal3.needs_external_trading_day(saturday.date()) is False
