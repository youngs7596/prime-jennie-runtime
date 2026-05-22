# `prime_jennie_runtime/` — 패키지 루트

v3 실행 엔진의 전체 Python 패키지. 실거래 운영 중(MS-01).

> **Slow Loop Awareness 가드 (2026-05-17 단순화)** — 3 카테고리: `outcome_feedback` (Scout context), `same_day_cooldown` (Strategy Engine), `thesis_aware_hold` (Phase A 영속 + 재설계 대기). 결정 + cross-ref: [`.ai/designs/2026-05-17-g-series-simplification.md`](../.ai/designs/2026-05-17-g-series-simplification.md). 구 G1~G6 명명은 commit history 에서만 인용.
>
> **결정론 Scout 코어 (2026-05-22)** — LLM-at-core 코드 생성 폐기 → v2 quant.py 결정론 7팩터 스코어러 복원. 결정 기록: [`.ai/decisions/2026-05-22-selection-architecture-decision.md`](../.ai/decisions/2026-05-22-selection-architecture-decision.md). Macro 만 LLM 유지 (안전망 이중).
>
> **outcomes 적재 (2026-05-22)** — 완전 청산 시 `fast_loop/persistence.py:record_sell` 이 `outcomes` 테이블에 결과 UPSERT. 운영 청산 결과를 시스템이 처음 기록 (백필 132건 -1,377만원).

## 서브모듈 지도

| 디렉토리 | 책임 | Track | 상태 |
|---|---|---|---|
| `infra/` | 설정, DB, Redis Streams, Observer, scheduler, heartbeat, llm_stats | A | ✅ 운영 |
| `slow_loop/` | Macro Pipeline(LLM) + 결정론 Scout + Strategy Engine | B | ✅ 운영 (2026-05-22 결정론 복원) |
| `fast_loop/` | 시트 consumer + tick loop + entry/exit (LLM 금지) | C | ✅ 운영 |
| `kis_gateway/` | KIS OpenAPI 프록시 (REST + WebSocket) | C | ✅ 운영 |
| `telegram_bot/` | 제어 명령(24종) + 알림 + LLM intent router | C | ✅ 운영 |
| `monitor/` | KIS 잔고 polling (장 시간 인식) + DLQ 감시 | C | ✅ 운영 |
| `jobs/` | 26 cron job 핸들러 (job-worker 컨테이너) | A/E | ✅ 운영 |
| `news_pipeline_kor/` | Naver 뉴스 → Qwen3 메타데이터 → news_events | E | ✅ 운영 (Qdrant 폐기) |
| `news_pipeline_global/` | WSJ/Bloomberg/Reuters via Google News + DeepSeek digest | E | ✅ 운영 |
| `dashboard/` | FastAPI 11 라우터 (외부 API + control-ui 백엔드) | A | ✅ 운영 |
| `control/` | SystemState snapshot + ControlCommand consumer | A | ✅ 운영 |
| `coordinator/` | event_log 아카이브 + advisory 정책 (별도 컨테이너) | A | ✅ Stage 2 운영 |
| `briefing/` | 일일 17시 브리핑 (Telegram HTML) | A | ✅ 운영 |
| `council_logging/` | Macro Council 실행 이력 (replay 지원) | A | ✅ 운영 |
| `backtest/` | 백테스트 엔진 (fast_loop 청산 재사용 + no-lookahead) | A | ✅ 운영 |
| `position_sheet/` | 포지션 시트 Pydantic 스키마 (공유 계약) | A/B | ✅ 운영 (schema v1.1) |
| `screening_executor/` | 격리 샌드박스 — **2026-05-22 결정론 전환으로 본선 dead-path**. scripts/scout_replay.py 와 일부 테스트에서만 참조 | D | ⚠️ 사용 보류 |

## 의존성 방향

```
infra/         ← 모든 서브모듈이 의존
position_sheet/ ← slow_loop, fast_loop, backtest 의존 (공유 계약)
slow_loop/ → 결정론 scout 함수 직접 호출 (screening_executor 본선 우회)
fast_loop/ → kis_gateway/ (HTTP 클라이언트)
coordinator/ ← slow_loop · fast_loop · control 이 event publish
```

## 규칙

- 각 서브모듈의 `AGENTS.md`를 먼저 읽고 작업
- `position_sheet/schema.py` 수정은 stop-the-world (모든 Track 영향)
- `infra/` 수정은 Track A 소유, 다른 Track 은 read-only
- `slow_loop/strategy/strategy_policy.yaml` 은 수동 관리 (commit으로만 변경)
- 라이브 운영 중 — 매매 path 변경은 가급적 장 외 시간에 + 테스트 동반
