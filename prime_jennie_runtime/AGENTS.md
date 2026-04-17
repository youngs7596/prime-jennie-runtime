# `prime_jennie_runtime/` — 패키지 루트

이 디렉토리는 v3 실행 엔진의 전체 Python 패키지입니다.

## 서브모듈 지도

| 디렉토리 | 책임 | Track | 상태 |
|---|---|---|---|
| `infra/` | 설정, DB, Redis Streams, Observer, LiteLLM | A | ✅ 완료 |
| `slow_loop/` | Scout, Macro Gate, Strategy Engine | B | 미구현 |
| `fast_loop/` | Executor (LLM 금지, 결정론 코드) | C | 미구현 |
| `kis_gateway/` | KIS API 프록시 (v2 포팅) | C | 미구현 |
| `telegram_bot/` | 긴급 제어 + 알림 (v2 포팅) | C | 미구현 |
| `screening_executor/` | 격리 샌드박스 코드 실행 | D | 미구현 |
| `news_pipeline_kor/` | 네이버 뉴스 → 감성분석 → Qdrant (v2 포팅) | E | 미구현 |
| `position_sheet/` | 포지션 시트 Pydantic 스키마 (공유 계약) | A/B | ✅ 완료 |
| `backtest/` | 백테스트 엔진 | B | 미구현 |

## 의존성 방향

```
infra/ ← 모든 서브모듈이 의존
position_sheet/ ← slow_loop, fast_loop이 의존 (공유 계약)
slow_loop/ → screening_executor/ (ToolAdapter)
fast_loop/ → kis_gateway/ (HTTP 클라이언트)
```

## 규칙

- 각 서브모듈의 `AGENTS.md`를 먼저 읽고 작업
- `position_sheet/schema.py` 수정은 stop-the-world (모든 Track 영향)
- `infra/` 수정은 Track A 소유, 다른 Track은 read-only
