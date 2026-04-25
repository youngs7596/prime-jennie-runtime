"""`pj.scout.escalate` → Telegram 라우팅 observer.

Scout 코드 검증 닫힌 루프 (`slow_loop/scout/code_loop.py`) 가 3회 모두 실패하면
``pj.scout.escalate`` 이벤트를 emit 한다. 이 observer 는 그 이벤트를 받아 사람이
모바일에서 즉시 읽을 수 있는 한국어 알림으로 텔레그램에 보낸다.

설계 원칙:
  - **Best-effort**: send 가 실패해도 절대 raise 하지 않는다. slow_loop 파이프라인을
    알림 실패가 끊으면 안 된다.
  - **Filter**: `pj.scout.escalate` 외 이벤트는 즉시 무시 (CompositePJObserver 가
    모든 이벤트를 broadcast 하므로 자체 필터 필요).
  - **Graceful fallback**: metadata 에 필드가 없어도 (None / 누락) 메시지가
    형성되도록 안전한 기본값 적용.
  - **dry_run**: 메시지 포맷만 하고 실제 send 호출은 생략. 개발/스테이징 안전.

이벤트 페이로드 스펙 (`code_loop.py` 의 두 emission 지점):
  - 정상 escalate: attempts(int), last_error(str|None), early_break(str|None),
    scout_run_id(str|None), code_hashes(list[str], 시도된 코드의 sha256[:16] 시퀀스)
  - no_output 케이스: attempts=0, reason="no_output", scout_run_id, code_hashes=[]
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Protocol
from zoneinfo import ZoneInfo

from minyoung_mah import ObserverEvent

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class _TelegramSender(Protocol):
    """`TelegramBot.send_message` 의 최소 시그니처. 테스트는 fake 로 대체."""

    async def send_message(self, text: str, **kwargs: object) -> bool: ...


class EscalateTelegramObserver:
    """`pj.scout.escalate` 이벤트만 골라 Telegram 으로 라우팅하는 observer.

    minyoung-mah Observer 프로토콜 (`async emit(event)`) 을 구현. 다른 이벤트는 무시.

    Args:
        telegram_client: ``send_message(text)`` 를 가진 비동기 클라이언트
            (`TelegramBot` 또는 테스트 fake).
        dry_run: True 면 메시지 포맷만 하고 실제 send 호출하지 않는다 (DEBUG 로그만).
    """

    EVENT_NAME = "pj.scout.escalate"

    def __init__(self, telegram_client: _TelegramSender, *, dry_run: bool = False) -> None:
        self._client = telegram_client
        self._dry_run = dry_run

    async def emit(self, event: ObserverEvent) -> None:
        """이벤트 수신. 본 observer 가 처리하는 이벤트만 라우팅, 그 외엔 무시."""
        # 1. 필터
        if event.name != self.EVENT_NAME:
            return

        # 2. 메시지 포맷 (예외 절대 전파 X)
        try:
            text = self._format_message(event)
        except Exception:
            logger.exception("EscalateTelegramObserver format failed; skipping send")
            return

        # 3. dry_run 또는 실제 전송
        if self._dry_run:
            logger.info("EscalateTelegramObserver dry_run — would send: %s", text[:200])
            return

        try:
            ok = await self._client.send_message(text)
            if not ok:
                logger.warning("Telegram send returned False for escalate event")
        except Exception:
            # 모든 예외 흡수 — 알림 실패가 slow_loop 파이프라인을 끊어선 안 됨.
            logger.exception("Telegram send raised on escalate event; suppressed")

    @staticmethod
    def _format_message(event: ObserverEvent) -> str:
        """모바일 텔레그램에서 즉시 읽히는 한국어 평문 알림 포맷."""
        meta = event.metadata or {}
        attempts = meta.get("attempts")
        last_error = meta.get("last_error")
        early_break = meta.get("early_break")
        reason = meta.get("reason")  # no_output 케이스용
        scout_run_id = meta.get("scout_run_id")
        code_hashes = meta.get("code_hashes") or []

        # KST 시각 (event.timestamp 는 UTC). 타임존 정보 누락 대비 안전 처리.
        ts = event.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        kst_str = ts.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")

        # 헤더 — attempts 가 0 이면 no_output, 1 이상이면 N회 실패
        if not attempts:
            header = "🚨 Scout escalate (출력 없음)"
        else:
            header = f"🚨 Scout escalate ({attempts}회 모두 실패)"

        lines = [header, ""]
        if scout_run_id:
            lines.append(f"scout_run_id: {scout_run_id}")
        lines.append(f"attempts: {attempts if attempts is not None else 0}")

        if last_error:
            lines.append(f"마지막 에러: {last_error}")
        elif reason:
            lines.append(f"사유: {reason}")
        else:
            lines.append("마지막 에러: (정보 없음)")

        if early_break:
            lines.append(f"조기종료: {early_break}")

        if code_hashes:
            lines.append(f"code_hash 시퀀스: {' → '.join(code_hashes)}")

        lines.append(f"KST: {kst_str}")
        return "\n".join(lines)
