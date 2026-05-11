"""실시간 tick → 분봉 집계 + indicator lookup.

POSITION_SHEET_SPEC §4.2 의 ``volume_over_ma20`` 및 (확장) ``rsi_under`` 평가에
필요한 분봉 누적/MA/RSI lookup 인프라. v2 원본:

  - ``prime_jennie/services/scanner/bar_engine.py`` (1분봉 집계 + VWAP)
  - ``prime_jennie/services/scanner/strategies.py`` (compute_rsi_from_bars)

여기서는 fast_loop tick_loop 가 매 tick 마다 ``BarEngine.update(ticker, price,
volume)`` 호출 → 분봉이 완성되면 1분봉 history 에 append. ``IndicatorProvider``
가 ``volume_ma20`` / ``rsi_1m`` / ``recent_high(N)`` 를 lookup 해 평가에 제공.

분봉 history 가 누적될 때까지 (warm-up 기간) 일부 indicator 는 ``None`` 을
반환하므로 평가자는 None 처리를 명시해야 한다.

design Phase 0 §4.5 명시된 Overextension Filter / GAP_UP_REBOUND breakout
confirmation 진입 시그널이 모두 이 인프라 위에서 동작.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)

# 기본 분봉 interval
DEFAULT_BAR_INTERVAL_SEC = 60
# 종목별 보관할 최대 완성 분봉 수 (ma20 + RSI 14 + breakout high N=5 모두 충족)
DEFAULT_MAX_HISTORY = 60


@dataclass
class Bar:
    """1분 캔들스틱."""

    timestamp: float  # epoch sec, bar 시작 시각
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class BarEngine:
    """실시간 tick → 분봉 집계 엔진.

    Usage::

        engine = BarEngine(bar_interval_sec=60, max_history=60)
        completed_bar = engine.update("005930", price=72100, volume=5000)
        bars = engine.recent_bars("005930", count=20)
        vol_ma20 = engine.volume_ma20("005930")
        rsi = engine.rsi("005930", period=14)

    스레드 안전. ``tick_loop`` 가 매 tick 마다 ``update`` 를 호출하고 동시에
    별도 task 에서 indicator lookup 을 한다고 가정.
    """

    def __init__(
        self,
        *,
        bar_interval_sec: int = DEFAULT_BAR_INTERVAL_SEC,
        max_history: int = DEFAULT_MAX_HISTORY,
    ) -> None:
        self._interval = bar_interval_sec
        self._max_history = max_history
        self._lock = threading.Lock()
        self._current: dict[str, dict] = {}
        self._completed: dict[str, list[Bar]] = defaultdict(list)
        # 일별 누적 거래량 — volume_over_ma20 의 "분봉 누적 거래량" 분자
        self._intraday_volume: dict[str, dict] = defaultdict(
            lambda: {"date": None, "cum_volume": 0.0}
        )

    def update(
        self,
        ticker: str,
        price: float,
        volume: float | int | None = 0,
        *,
        ts: datetime | None = None,
    ) -> Bar | None:
        """tick 1개 반영. 분봉이 완성되면 그 Bar 반환, 아니면 None.

        ``volume`` 은 KIS H0STCNT0 의 ``cntg_vol`` (틱 단일 체결량). None / 음수면
        0 으로 취급. ``ts`` 미지정 시 ``datetime.now(UTC)``.
        """
        vol = float(volume or 0.0)
        if vol < 0:
            vol = 0.0

        now = ts.astimezone(UTC) if ts is not None and ts.tzinfo else ts or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        ts_epoch = now.timestamp()
        bar_ts = int(ts_epoch // self._interval) * self._interval
        today = now.astimezone(UTC).date()

        with self._lock:
            self._accumulate_intraday(ticker, today, vol)

            current = self._current.get(ticker)
            if current is None or current["ts"] != bar_ts:
                completed: Bar | None = None
                if current is not None:
                    completed = Bar(
                        timestamp=current["ts"],
                        open=current["open"],
                        high=current["high"],
                        low=current["low"],
                        close=current["close"],
                        volume=current["volume"],
                    )
                    hist = self._completed[ticker]
                    hist.append(completed)
                    if len(hist) > self._max_history:
                        del hist[: len(hist) - self._max_history]

                self._current[ticker] = {
                    "ts": bar_ts,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": vol,
                }
                return completed

            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] += vol
            return None

    def _accumulate_intraday(self, ticker: str, today: date, vol: float) -> None:
        entry = self._intraday_volume[ticker]
        if entry["date"] != today:
            entry["date"] = today
            entry["cum_volume"] = 0.0
        entry["cum_volume"] += vol

    # ── lookups ─────────────────────────────────────────────

    def recent_bars(self, ticker: str, *, count: int = 20) -> list[Bar]:
        """완성된 최근 N 분봉. 부족하면 가진 만큼."""
        with self._lock:
            hist = self._completed.get(ticker)
            if not hist:
                return []
            return list(hist[-count:])

    def current_bar(self, ticker: str) -> Bar | None:
        """진행 중 분봉 snapshot. 없으면 None."""
        with self._lock:
            cur = self._current.get(ticker)
            if cur is None:
                return None
            return Bar(
                timestamp=cur["ts"],
                open=cur["open"],
                high=cur["high"],
                low=cur["low"],
                close=cur["close"],
                volume=cur["volume"],
            )

    def intraday_cum_volume(self, ticker: str, *, now: datetime | None = None) -> float:
        """오늘 누적 체결량. 날짜가 바뀌면 0."""
        ref = now or datetime.now(UTC)
        today = ref.astimezone(UTC).date() if ref.tzinfo else ref.replace(tzinfo=UTC).date()
        with self._lock:
            entry = self._intraday_volume.get(ticker)
            if entry is None or entry["date"] != today:
                return 0.0
            return float(entry["cum_volume"])

    def volume_ma20(self, ticker: str, *, period: int = 20) -> float | None:
        """최근 ``period`` 개 완성 분봉의 거래량 평균. 부족하면 None.

        ``volume_over_ma20`` condition 평가에서 분모로 사용. v2 와 분 단위 의미는
        다르다 — v2 는 일봉 거래량 ma20 vs 일거래량. v3 fast_loop 는 1분봉
        볼륨 ma20 vs 일중 누적 (>>분봉) 이라 ratio 가 매우 큰 값으로 나옴 →
        ``min_ratio`` 임계값은 운영 데이터 보며 점진 조정. (yaml 주석 참조)
        """
        with self._lock:
            hist = self._completed.get(ticker)
            if hist is None or len(hist) < period:
                return None
            window = hist[-period:]
            total = sum(b.volume for b in window)
            return total / period if period > 0 else None

    def rsi(self, ticker: str, *, period: int = 14) -> float | None:
        """완성 분봉 close 기반 RSI. 부족하면 None."""
        with self._lock:
            hist = self._completed.get(ticker)
            if hist is None or len(hist) < period + 1:
                return None
            closes = [b.close for b in hist]
        return _compute_rsi(closes, period)

    def recent_high(self, ticker: str, *, lookback: int = 5) -> float | None:
        """최근 ``lookback`` 개 완성 분봉의 최고 high. 부족하면 None.

        GAP_UP_REBOUND 직전 봉 breakout confirmation 용 (``price_above``).
        """
        with self._lock:
            hist = self._completed.get(ticker)
            if hist is None or len(hist) < lookback:
                return None
            window = hist[-lookback:]
            return max(b.high for b in window)

    def bar_count(self, ticker: str) -> int:
        with self._lock:
            return len(self._completed.get(ticker, []))


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder smoothing RSI. v2 ``strategies._compute_rsi`` 와 등가."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ──────────────────────── IndicatorProvider ────────────────────────


class IndicatorProvider:
    """``EntryConditionEvaluator`` 가 의존하는 indicator lookup 인터페이스.

    프로덕션은 ``BarEngine`` 을 wrap. 테스트는 직접 dict / mock 으로 대체.
    """

    def volume_ma20(self, ticker: str) -> float | None:
        """20분봉 거래량 평균. None 이면 평가자는 보류 (warm-up 중)."""
        return None

    def intraday_cum_volume(self, ticker: str) -> float | None:
        """일중 누적 체결량. None 이면 평가 불가."""
        return None

    def rsi_1m(self, ticker: str) -> float | None:
        """1분봉 RSI (period=14). None 이면 평가 보류."""
        return None

    def recent_high(self, ticker: str, *, lookback: int = 5) -> float | None:
        """최근 lookback 분봉 고가. None 이면 평가 보류."""
        return None


class BarEngineIndicatorProvider(IndicatorProvider):
    """``BarEngine`` 을 indicator 소스로 사용하는 어댑터."""

    def __init__(self, engine: BarEngine) -> None:
        self._engine = engine

    def volume_ma20(self, ticker: str) -> float | None:
        return self._engine.volume_ma20(ticker)

    def intraday_cum_volume(self, ticker: str) -> float | None:
        return self._engine.intraday_cum_volume(ticker)

    def rsi_1m(self, ticker: str) -> float | None:
        return self._engine.rsi(ticker, period=14)

    def recent_high(self, ticker: str, *, lookback: int = 5) -> float | None:
        return self._engine.recent_high(ticker, lookback=lookback)


__all__ = [
    "Bar",
    "BarEngine",
    "IndicatorProvider",
    "BarEngineIndicatorProvider",
]
