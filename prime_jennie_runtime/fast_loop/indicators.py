"""기술적 지표 — SMA, EMA, RSI, Bollinger, MACD, Death Cross.

v2 `prime_jennie/services/monitor/indicators.py` 포팅 (그대로).
pandas 없이 순수 Python. Track C Fast Loop가 1분봉/일봉 평가에 사용.
"""

from __future__ import annotations

import math


def calculate_sma(prices: list[float], period: int) -> list[float | None]:
    """단순이동평균. period 미만 인덱스는 None."""
    result: list[float | None] = [None] * len(prices)
    if len(prices) < period:
        return result

    window_sum = sum(prices[:period])
    result[period - 1] = window_sum / period

    for i in range(period, len(prices)):
        window_sum += prices[i] - prices[i - period]
        result[i] = window_sum / period

    return result


def calculate_ema(prices: list[float], span: int) -> list[float]:
    """지수이동평균."""
    if not prices:
        return []

    k = 2.0 / (span + 1)
    result = [prices[0]]
    for i in range(1, len(prices)):
        result.append(prices[i] * k + result[-1] * (1 - k))
    return result


def calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    """RSI (Wilder's smoothing). 데이터 부족 시 None."""
    if len(closes) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_bollinger_bands(
    prices: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[float | None, float | None, float | None]:
    """볼린저 밴드 (최근 시점 기준). 데이터 부족 시 (None, None, None)."""
    if len(prices) < period:
        return (None, None, None)

    window = prices[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)

    upper = middle + num_std * std
    lower = middle - num_std * std
    return (upper, middle, lower)


def check_death_cross(
    close_prices: list[float],
    short: int = 5,
    long: int = 20,
    gap: float = 0.002,
) -> bool:
    """Death Cross 판정 — 현재 short MA < long MA * (1-gap) AND 직전 short MA >= long MA."""
    if len(close_prices) < long + 1:
        return False

    sma_short = calculate_sma(close_prices, short)
    sma_long = calculate_sma(close_prices, long)

    curr_s = sma_short[-1]
    curr_l = sma_long[-1]
    prev_s = sma_short[-2]
    prev_l = sma_long[-2]

    if curr_s is None or curr_l is None or prev_s is None or prev_l is None:
        return False

    return curr_s < curr_l * (1 - gap) and prev_s >= prev_l


def calculate_macd(
    close_prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float], list[float]]:
    """MACD line + signal line."""
    ema_fast = calculate_ema(close_prices, fast)
    ema_slow = calculate_ema(close_prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow, strict=True)]
    signal_line = calculate_ema(macd_line, signal)
    return macd_line, signal_line
