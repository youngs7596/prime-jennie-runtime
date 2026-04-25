# `briefing/` — 일일 브리핑 content generator

Track D 소유 (Phase 2.10).

## 책임

- v3 DB 를 읽어 하루치 브리핑 데이터 집계 (`collect_briefing_data`)
- Jennie 페르소나 LLM 프롬프트로 HTML 리포트 생성 (`generate_briefing`)
- LLM 실패 시 결정론 HTML fallback (`format_fallback_html`)

## 책임 밖

- **Telegram 발송**: v3 `telegram_bot` 또는 Track B `daily_briefing_report` job 담당
- **중복발송 dedup**: Track B job 담당 (Redis key `briefing:sent:{date}`)
- **KOSPI/KOSDAQ 종가 갱신**: v2 `naver_market` 크롤러. v3 에선 slow_loop feeder 재사용

## v2 원본

`prime-jennie/prime_jennie/services/briefing/reporter.py` (535줄)

## 포팅 규칙

- 프롬프트 문자열, pure formatter (_build_data_context, _format_fallback_html, _compute_trade_summary) 는 그대로 포팅
- v2 SQLModel repo → v3 asyncpg 로 어댑터
- v2 `StockNewsSentimentDB` → v3 `news_sentiments` + `news_articles`
- v2 `PortfolioRepository.get_positions` / `get_recent_trades` → v3 `position_sheets` + `executions` + `outcomes` 조합 (Phase 2.10 범위에선 outcomes 기반 sells만 채움)
- v2 `MacroRepository.get_latest_insight` → v3 `macro_runs` 최신 row
- v2 `WatchlistRepository.get_latest` → migration 016 으로 `legacy_quant_scores` 가 drop 된 뒤로는 빈 리스트 반환 (`_collect_watchlist`). v3 워치리스트 의미는 scout_runs / screening_candidates 가 흡수.
- v2 `AssetSnapshotRepository` → v3 테이블 없음. 당분간 None (Track B 가 KIS snapshot 으로 채울 예정)

## 공개 API

```python
from prime_jennie_runtime.briefing import (
    generate_briefing,       # context(dict) + llm_caller → HTML
    collect_briefing_data,   # AsyncConnection + date → dict
    format_fallback_html,    # LLM 실패 시 결정론 HTML
    JENNIE_SYSTEM_PROMPT,
)
```

Track B (`daily_briefing_report` job) 호출 패턴:

```python
data = await collect_briefing_data(conn, as_of=today)
html = await generate_briefing(data, llm_caller=claude_caller)
await telegram_bot.send(html)
```
