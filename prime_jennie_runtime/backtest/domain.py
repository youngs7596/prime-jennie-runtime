"""v2 domain.enums 로부터 백테스트가 참조하는 6종 enum 로컬 복사.

v2 값과 1:1 (StrEnum). 새 값 추가 금지 — v2 DB row 와 호환.
포팅 원본: prime-jennie/prime_jennie/domain/enums.py
"""

from __future__ import annotations

from enum import StrEnum


class MarketRegime(StrEnum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    SIDEWAYS = "SIDEWAYS"
    BEAR = "BEAR"
    STRONG_BEAR = "STRONG_BEAR"


class TradeTier(StrEnum):
    TIER1 = "TIER1"
    TIER2 = "TIER2"
    BLOCKED = "BLOCKED"


class SignalType(StrEnum):
    GOLDEN_CROSS = "GOLDEN_CROSS"
    RSI_REBOUND = "RSI_REBOUND"
    MOMENTUM = "MOMENTUM"
    MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
    DIP_BUY = "DIP_BUY"
    VOLUME_BREAKOUT = "VOLUME_BREAKOUT"
    WATCHLIST_CONVICTION = "WATCHLIST_CONVICTION"
    ORB_BREAKOUT = "ORB_BREAKOUT"
    GAP_UP_REBOUND = "GAP_UP_REBOUND"


class SellReason(StrEnum):
    PROFIT_TARGET = "PROFIT_TARGET"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    RSI_OVERBOUGHT = "RSI_OVERBOUGHT"
    TIME_EXIT = "TIME_EXIT"
    PROFIT_FLOOR = "PROFIT_FLOOR"
    DEATH_CROSS = "DEATH_CROSS"
    RISK_OFF = "RISK_OFF"
    MANUAL = "MANUAL"
    FORCED_LIQUIDATION = "FORCED_LIQUIDATION"


class SectorGroup(StrEnum):
    SEMICONDUCTOR_IT = "반도체/IT"
    BIO_HEALTH = "바이오/헬스케어"
    SECONDARY_BATTERY = "2차전지/소재"
    FINANCE = "금융"
    AUTOMOBILE = "자동차"
    CONSTRUCTION = "건설/부동산"
    CHEMICAL = "화학/에너지"
    STEEL_MATERIAL = "철강/소재"
    FOOD_CONSUMER = "음식료/생활"
    MEDIA_ENTERTAINMENT = "미디어/엔터"
    LOGISTICS_TRANSPORT = "운송/물류"
    TELECOM = "통신"
    UTILITY = "유틸리티"
    DEFENSE_SHIPBUILDING = "조선/방산"
    ETC = "기타"


__all__ = [
    "MarketRegime",
    "SectorGroup",
    "SellReason",
    "SignalType",
    "TradeTier",
]
