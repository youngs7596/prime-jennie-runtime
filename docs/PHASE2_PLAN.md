# Phase 2 착수 계획 (v0.1 — 2026-04-17)

Phase 1 (Track A/B/C/D/E) 완료 이후. 외부 어댑터 실 구현 + 운영 전환이 중심.

Phase 1과의 경계:
- Phase 1: 결정론 핵심 로직 + Stub 어댑터. 423 tests 녹색.
- Phase 2: Protocol 자리에 v2 코드를 포팅한 실 어댑터. 실계좌/실서비스 연결.

## 0. Phase 2 범위 (design §8 확장)

포함:
- v2 포팅 어댑터 7종 (아래 §1~§5)
- 운영 전환 (실 토큰, DRY_RUN=false, paper account smoke)
- Control UI `control.commands` consumer — pub/sub만. UI 자체는 Phase 2.5.

제외 (Phase 3 이후):
- `prime-jennie-meta` (자가진화). Phase 3.
- `prime-jennie-control-ui` 프론트. Phase 2.5.
- v2 은퇴. Phase 6.

## 1. v2 포팅 워크스트림

각 항목: **v2 원본 → v3 대상 / 핵심 설계 / 테스트 전략**.

### 1.1 Telegram 양방향 명령 (Track C)

- **v2**: `prime-jennie/prime_jennie/services/telegram/handler.py` (744 lines)
- **v3**: `prime_jennie_runtime/telegram_bot/handler.py` (신규)
- **명령**: `/stop`, `/pause`, `/resume`, `/liquidate`, `/dryrun`, `/status`
- **핵심 설계**:
  - v2 handler는 Redis `trading_flags` 를 직접 조작. v3도 동일 key 스킴 유지 (v2/v3 공존기 동기화 불필요 — 각자 독립).
  - 명령 수신 → `infra.redis_streams` 에 `control.commands` publish → fast/slow loop consumer 가 반영.
  - whitelist: `TELEGRAM_ALLOWED_CHAT_IDS` 환경변수.
- **테스트**: 각 명령 핸들러 단위 + `control.commands` stream publish 확인. Telegram API는 respx mock.

### 1.2 Control UI `control.commands` consumer (Track C)

- **v2**: 없음 (신규)
- **v3**: `prime_jennie_runtime/fast_loop/control_consumer.py` + `slow_loop/control_consumer.py`
- **핵심 설계**:
  - Redis Stream `control.commands` 구독. 각 루프가 XREAD 로 폴링.
  - 명령 타입: `emergency_stop`, `pause`, `resume`, `liquidate_all`, `set_dryrun`.
  - 기존 `runtime_state` / risk throttle 상태와 합쳐 최종 gate 결정 (min 방식).
- **테스트**: fakeredis로 publish → consumer 가 상태 전이. 기존 risk_throttle과 역전 방지 확인.

### 1.3 KIS Gateway DB 폴백 (Track C)

- **v2**: 부분 구현 (stock_minute_prices read-only 마운트)
- **v3**: `prime_jennie_runtime/kis_gateway/db_fallback.py` (신규)
- **핵심 설계**:
  - KIS API rate limit / circuit open 상태에서 Postgres 의 최근 N 캔들을 fallback 시세로 반환.
  - 테이블은 `legacy_daily_prices` 와 신규 `daily_prices` (v3에서 누적). **design §13에 누락** — Phase 2 시작 시 design 업데이트 필요.
  - read-through 캐시: KIS 응답 성공 시 v3 daily_prices 에 upsert.
- **테스트**: respx 로 KIS 429/circuit-open 시뮬 → fallback 경로 hit. DB는 in-memory SQLite (테스트 전용) 또는 docker postgres.
- **결정 필요**: v2 `stock_minute_prices` read-only 마운트 계속할지, v3 자체 minute_prices 테이블로 재집계할지.

### 1.4 Naver Finance Crawler 실 구현 (Track E)

- **v2**: `prime-jennie/prime_jennie/services/news/collector.py` (87 lines)
- **v3**: `prime_jennie_runtime/news_pipeline_kor/adapters/naver_crawler.py` (신규) → `NewsCrawler` Protocol 구현
- **핵심 설계**: v2 코드 그대로 포팅. `httpx.AsyncClient` + `BeautifulSoup4`. 87줄 소품이라 재작성 유혹 금지.
- **테스트**: `respx` 로 네이버 HTML fixture mock → article 파싱 정확. 네트워크 테스트 X (CI 깨짐).

### 1.5 EXAONE 4.0 Q8 sentiment adapter (Track E)

- **v2**: `prime-jennie/prime_jennie/services/news/analyzer.py` (290 lines)
- **v3**: `prime_jennie_runtime/news_pipeline_kor/adapters/exaone_sentiment.py` → `SentimentAnalyzer` Protocol 구현
- **핵심 설계**: LiteLLM 경유 (`litellm.acompletion(model="ollama/exaone3.5:32b")`). v2 프롬프트 + score 파싱 로직 그대로 포팅. score → label 이산화는 `news_pipeline_kor` 기존 규칙 재사용.
- **테스트**: LiteLLM 응답 mock (freezegun/httpx_mock). 실 LLM 없이 결정론 파서만 검증.

### 1.6 kure-v1 embedder + Qdrant adapter (Track E)

- **v2**: `prime-jennie/prime_jennie/services/news/archiver.py` (235 lines)
- **v3**:
  - `adapters/kure_embedder.py` → `Embedder` Protocol (httpx 로 kure-v1 서버 호출)
  - `adapters/qdrant_vector_store.py` → `VectorStore` Protocol (qdrant-client)
- **핵심 설계**: v2 Qdrant 컬렉션 이름/스키마 유지 (공존기 쓰기 겹침 없게 v3는 별도 컬렉션 + source tag 구분). embedder는 단일 `embed(text) -> vector` 인터페이스.
- **테스트**: qdrant-client는 `qdrant_client.QdrantClient(location=":memory:")` 지원 → 실 컬렉션 E2E. kure-v1은 respx mock.

### 1.7 Postgres 어댑터 (Track E)

- **v2**: 없음 (v3 자체 schema)
- **v3**: `news_pipeline_kor/adapters/pg_sentiment_repo.py` → `SentimentRepo` Protocol 구현 (asyncpg)
- **핵심 설계**: Phase 1의 `InMemorySentimentRepo` 와 동일 쿼리 의미. `news_sentiments` 테이블 신설 (migrations/003).
- **테스트**: 실 postgres (`testcontainers` 선택지) 또는 단위 → in-memory 와 동일 시나리오 통과.

## 2. 의존성 그래프

```
1.4 Naver Crawler ─────┐
                       │
1.5 EXAONE Sentiment ──┼──▶ 1.7 PG SentimentRepo ──▶ slow_loop 통합 E2E (real feeder)
                       │
1.6 Qdrant/kure-v1 ────┘

1.1 Telegram handler ──▶ 1.2 control_consumer ──▶ fast/slow loop 통합

1.3 KIS DB fallback (독립)
```

- 1.1 → 1.2 는 강 결합 (Telegram 이 producer, consumer 가 receiver).
- 1.4/1.5/1.6 은 병렬 가능 (서로 독립). 1.7 이 이들을 묶는 integration point.

## 3. 제안 착수 순서

**A. 선 운영가치 (빠른 효과)**

1. **1.1 + 1.2 Telegram 양방향 + control_consumer** — 운영자가 v3 를 원격 정지할 수단 확보. 실계좌 접근 전 필수.
2. **1.3 KIS DB fallback** — 장중 KIS 장애 시 fast loop 생존력 ↑. 독립이라 언제든 삽입 가능.

**B. 뉴스 파이프라인 실 전환 (1.4 → 1.5 → 1.6 → 1.7)**

3. **1.4 Naver crawler** — 가장 단순 (87 lines). 먼저 포팅해서 StubCrawler 대체.
4. **1.5 EXAONE** — 로컬 Ollama/EXAONE 준비 필요. 실 호출은 영석 환경.
5. **1.6 kure-v1 + Qdrant** — Qdrant 컬렉션 명명 규칙 (v2/v3 공존) 결정 선행.
6. **1.7 PG SentimentRepo + migrations/003** — 위 세 adapter 결과를 받아 저장.

**C. 운영 전환 (마지막)**

7. Telegram bot token 실계좌 + `TELEGRAM_DRY_RUN=false`.
8. KIS paper account 토큰 발급 + smoke test. 실계좌 진입은 Phase 2 종료 조건 중 하나로 남김.

## 4. 결정 사항 (D1~D5 2026-04-17 확정)

| # | 결정 | 근거 |
|---|------|------|
| D1 | Qdrant 컬렉션 **분리** (`v2_news_sentiments` / `v3_news_sentiments`) | 공존기 쓰기 충돌 0, 권한 관리 단순, v2 은퇴 시 v2_* drop 만으로 완료. §13.1 "재임베딩 후 v3 append" 정신과 일치. |
| D2 | v3 자체 `minute_prices` / `daily_prices` 테이블 신설 | KIS API 수신분을 실시간 누적하려면 쓰기 필요. v2 read-only 마운트 의존은 Phase 6 v2 은퇴 시 고통. v2 `stock_minute_prices` 는 historical backfill 1회만 사용. |
| D3 | `legacy_daily_prices` 마이그레이션 **스크립트 불필요** | §13.1 "read-only 마운트, 마이그 없음" 정책 일관. 초기 backfill 은 운영 one-shot 스크립트로 처리 (migrations/ 에는 포함하지 않음). |
| D4 | `TELEGRAM_ALLOWED_CHAT_IDS` 기본값 `[]` (빈 리스트) → **빈 리스트면 기동 거부** | fail-safe. 환경별 분리는 단일 변수 + `.env.prod` / `.env.staging` 파일로. |
| D5 | KIS paper 토큰 발급은 영석 외부 작업 | 코드는 paper 가정으로 작성. smoke test 스크립트 사전 준비 (`scripts/kis_paper_smoke.py`). |

## 5. 종료 조건

Phase 2 완료 기준:
- 7종 어댑터 모두 포팅, 기존 Stub 자리에 drop-in.
- Telegram 양방향 동작 (DRY_RUN 에서 검증).
- KIS paper account smoke 통과.
- 통합 E2E: 실 Naver/EXAONE (로컬) → v3 postgres → slow_loop → fast_loop → Telegram notification.
- 전 테스트 녹색 + ruff clean.

## 6. 실 계좌 진입 체크리스트 (Phase 2.x)

운영 전환은 Phase 2와 별개 단계:
- [ ] KIS paper account 2주 이상 무결성 확인
- [ ] Telegram 명령 응답 시간 < 2s
- [ ] Control UI `/status` 에 v3 state 노출
- [ ] Emergency stop 복구 훈련 (drill) 1회
- [ ] 영석 최종 승인

---

**문서 상태**: 초안. 영석 검토 후 design 본문 (`prime_jennie_v3_phase0_design.md §8`) 에 마스터 반영.
