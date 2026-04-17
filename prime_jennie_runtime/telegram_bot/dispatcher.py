"""Notification envelope → (model, formatted_text) dispatch.

Publisher 규약 (Track C Fast Loop):
  `TypedStreamPublisher`가 `message.model_dump_json()`을 그대로 payload에 넣어
  `v3:notifications` 스트림에 XADD한다. 각 모델에는 `kind` 필드(Literal)가 있어
  JSON에서 직접 읽을 수 있다.

Consumer는 raw payload를 먼저 dict로 파싱하여 `kind`를 읽고, 해당 모델로
validate 후 formatter에 넘긴다. kind가 미지이면 DLQ 대신 경고 로그만 남기고
메시지를 폐기한다 (예외 전파 금지 — 포맷 오류가 fast loop에 영향 주지 않도록).
"""

from __future__ import annotations

import json
import logging

from prime_jennie_runtime.fast_loop.schemas import (
    GenericAlertNotification,
    RiskLevelChangeNotification,
    TradeNotification,
)

from .formatters import (
    format_generic_alert,
    format_risk_level_change,
    format_trade,
)

logger = logging.getLogger(__name__)


def dispatch(payload_json: str) -> str | None:
    """payload JSON을 읽어 포맷된 Telegram 메시지 문자열 반환.

    Returns:
        HTML 문자열. 파싱 실패/미지 kind일 경우 None.
    """
    try:
        raw = json.loads(payload_json)
    except (ValueError, TypeError) as exc:
        logger.warning("Notification payload not valid JSON: %s", exc)
        return None

    if not isinstance(raw, dict):
        logger.warning("Notification payload not an object: %r", type(raw))
        return None

    kind = raw.get("kind")
    if not isinstance(kind, str):
        logger.warning("Notification payload missing 'kind': keys=%s", list(raw.keys()))
        return None

    try:
        if kind in ("entry_filled", "exit_filled"):
            return format_trade(TradeNotification.model_validate(raw))
        if kind == "risk_level_change":
            return format_risk_level_change(RiskLevelChangeNotification.model_validate(raw))
        if kind == "alert":
            return format_generic_alert(GenericAlertNotification.model_validate(raw))
    except Exception as exc:  # pydantic ValidationError 등
        logger.warning("Notification validation failed kind=%s: %s", kind, exc)
        return None

    logger.warning("Unknown notification kind: %s", kind)
    return None
