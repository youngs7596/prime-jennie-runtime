# `telegram_bot/` — 긴급 제어 + 알림

Track C 소유. v2 포팅.

## v2 원본

`prime_jennie/services/telegram/` — app.py, bot.py, handler.py

## 책임

- 긴급 제어: /stop, /pause, /liquidate, /dryrun, /status
- 매매 알림: Redis Stream 소비 → 체결/손절 메시지 전송
- Redis `control.commands` 채널로 명령 발행 (Control UI와 동일 경로)

## v3 확장

- Redis pub/sub `control.commands` — UI와 Telegram 모두 동일 채널
- Executor는 출처(UI/Telegram) 불문 일관 처리
