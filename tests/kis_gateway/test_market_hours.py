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
