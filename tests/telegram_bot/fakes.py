"""Telegram bot 테스트 Fake 컴포넌트."""

from __future__ import annotations


class FakeTelegramBot:
    """전송된 메시지를 리스트에 기록하는 테스트용 Bot."""

    def __init__(self) -> None:
        self.sent_messages: list[str] = []
        self.closed = False

    async def send_message(self, text: str, **kwargs: object) -> bool:
        self.sent_messages.append(text)
        return True

    async def close(self) -> None:
        self.closed = True
