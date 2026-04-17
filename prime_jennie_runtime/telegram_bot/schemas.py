"""Telegram bot 내부 전용 스키마.

대부분의 알림 envelope는 `fast_loop/schemas.py`의
`TradeNotification`, `RiskLevelChangeNotification`, `GenericAlertNotification`을
재사용한다. 이 모듈은 소비자 dispatch 편의를 위한 타입 별칭만 정의.
"""

from __future__ import annotations

from prime_jennie_runtime.fast_loop.schemas import (
    GenericAlertNotification,
    RiskLevelChangeNotification,
    TradeNotification,
)

# 3종 알림 discriminated union — kind 필드로 구분
NotificationModel = TradeNotification | RiskLevelChangeNotification | GenericAlertNotification

# kind → 모델 클래스 매핑
KIND_TO_MODEL: dict[str, type] = {
    "entry_filled": TradeNotification,
    "exit_filled": TradeNotification,
    "risk_level_change": RiskLevelChangeNotification,
    "alert": GenericAlertNotification,
}
