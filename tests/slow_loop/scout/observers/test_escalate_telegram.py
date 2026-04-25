"""EscalateTelegramObserver 테스트.

Coverage:
  - happy path: pj.scout.escalate → send_message 호출, 메시지 본문 검증
  - filter: 다른 이벤트는 send 호출 안 됨
  - 예외 격리: send_message 가 raise 해도 observer 가 raise 하지 않음
  - dry_run: 포맷만 하고 send 호출 X
  - graceful fallback: metadata 누락 / last_error=None 케이스 → 크래시 없음
  - no_output 케이스: attempts=0, reason="no_output" 페이로드 → 다른 헤더
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.infra.observer_impl import pj_event
from prime_jennie_runtime.slow_loop.scout.observers import EscalateTelegramObserver


class _FakeBot:
    """send_message 호출 기록 + 옵션으로 raise."""

    def __init__(self, *, raises: bool = False, returns: bool = True) -> None:
        self.sent: list[str] = []
        self._raises = raises
        self._returns = returns

    async def send_message(self, text: str, **_: object) -> bool:
        if self._raises:
            raise RuntimeError("network down")
        self.sent.append(text)
        return self._returns


@pytest.mark.asyncio
async def test_emit_escalate_event_sends_telegram_message():
    bot = _FakeBot()
    obs = EscalateTelegramObserver(bot)
    event = pj_event(
        "pj.scout.escalate",
        role="scout",
        ok=False,
        attempts=3,
        last_error="invalid_return_type",
        early_break=None,
    )

    await obs.emit(event)

    assert len(bot.sent) == 1
    msg = bot.sent[0]
    assert "Scout escalate" in msg
    assert "3회 모두 실패" in msg
    assert "attempts: 3" in msg
    assert "invalid_return_type" in msg
    assert "KST:" in msg


@pytest.mark.asyncio
async def test_emit_other_event_is_ignored():
    """`pj.scout.code_attempt` 같은 다른 이벤트는 send 호출 안 함."""
    bot = _FakeBot()
    obs = EscalateTelegramObserver(bot)
    event = pj_event(
        "pj.scout.code_attempt",
        role="scout",
        ok=False,
        attempt=1,
        error="invalid_return_type",
    )

    await obs.emit(event)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_send_failure_is_swallowed():
    """send_message 가 raise 해도 observer 가 예외 전파하지 않음."""
    bot = _FakeBot(raises=True)
    obs = EscalateTelegramObserver(bot)
    event = pj_event("pj.scout.escalate", attempts=3, last_error="boom")

    # 예외 발생하면 테스트 실패 — 정상 경로
    await obs.emit(event)


@pytest.mark.asyncio
async def test_send_returns_false_does_not_raise(caplog):
    """send_message 가 False 반환해도 예외 없음, 경고 로그만."""
    bot = _FakeBot(returns=False)
    obs = EscalateTelegramObserver(bot)
    event = pj_event("pj.scout.escalate", attempts=3, last_error="x")

    with caplog.at_level("WARNING"):
        await obs.emit(event)

    assert any("returned False" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_dry_run_does_not_call_send():
    """dry_run=True 면 send 호출 X."""
    bot = _FakeBot()
    obs = EscalateTelegramObserver(bot, dry_run=True)
    event = pj_event("pj.scout.escalate", attempts=3, last_error="x")

    await obs.emit(event)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_missing_metadata_graceful_fallback():
    """last_error=None / metadata 비어있을 때도 메시지 형성, 크래시 없음."""
    bot = _FakeBot()
    obs = EscalateTelegramObserver(bot)
    # metadata 비어있는 이벤트 — last_error/early_break/attempts 모두 누락
    event = pj_event("pj.scout.escalate", role="scout", ok=False)

    await obs.emit(event)

    assert len(bot.sent) == 1
    msg = bot.sent[0]
    # 누락에 대한 안전 폴백
    assert "Scout escalate" in msg
    assert "정보 없음" in msg or "사유:" in msg
    assert "attempts: 0" in msg


@pytest.mark.asyncio
async def test_no_output_escalate_uses_reason_header():
    """attempts=0, reason="no_output" 페이로드 — 다른 헤더 표기."""
    bot = _FakeBot()
    obs = EscalateTelegramObserver(bot)
    event = pj_event(
        "pj.scout.escalate",
        role="scout",
        ok=False,
        attempts=0,
        reason="no_output",
    )

    await obs.emit(event)

    assert len(bot.sent) == 1
    msg = bot.sent[0]
    assert "출력 없음" in msg
    assert "no_output" in msg


@pytest.mark.asyncio
async def test_kst_timestamp_conversion():
    """KST 변환: 2026-04-27 00:30:00 UTC → 09:30 KST."""
    from minyoung_mah import ObserverEvent

    bot = _FakeBot()
    obs = EscalateTelegramObserver(bot)
    event = ObserverEvent(
        name="pj.scout.escalate",
        timestamp=datetime(2026, 4, 27, 0, 30, 0, tzinfo=UTC),
        role="scout",
        ok=False,
        metadata={"attempts": 3, "last_error": "x"},
    )

    await obs.emit(event)

    assert "KST: 2026-04-27 09:30:00" in bot.sent[0]
