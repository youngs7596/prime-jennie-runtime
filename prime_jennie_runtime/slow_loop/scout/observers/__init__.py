"""Scout observer subscribers — `pj.scout.*` 이벤트 부수효과 라우팅.

`code_loop.py` 가 emit 하는 mah Observer 이벤트를 외부 시스템 (Telegram, alerting,
metrics) 으로 전달하는 thin subscriber 들. Subscriber 는 절대 emission 파이프라인을
중단시키면 안 된다 (CompositePJObserver 가 예외를 삼키지만, 추가 안전망으로 자체에서도
모든 예외 catch).
"""

from __future__ import annotations

from .escalate_telegram import EscalateTelegramObserver

__all__ = ["EscalateTelegramObserver"]
