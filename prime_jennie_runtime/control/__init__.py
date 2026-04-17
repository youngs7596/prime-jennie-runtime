"""Phase 2.2 — control.commands stream consumer + system state reader.

Telegram/UI 가 발행한 `ControlCommand` 를 해석해 `control.state:*` 키에 영속화.
fast/slow loop 의 진입/청산 결정이 이 state 를 읽어 gate 에 반영.
"""

from .consumer import ControlCommandConsumer
from .state import SystemState

__all__ = ["ControlCommandConsumer", "SystemState"]
