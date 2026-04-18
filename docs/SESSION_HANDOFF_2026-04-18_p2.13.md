# 세션 핸드오프 — 2026-04-18 (Phase 2.13)

**세션 범위**: Phase 2.13 자가진화 feeder/infra 보강 (후보 5개 중 4번·7번 제외 전량)
**시작**: 2026-04-18 늦은 오후 (Phase 2.11/2.12 addendum 직후) · **마감**: 같은 날 야간
**팀**: lead 단독 (Opus 4.7 1M)

## 결과 요약

- **Phase 2.13-1** WSJ/Bloomberg/Reuters RSS 실 크롤러 + DeepSeek LLM digest 파이프라인
- **Phase 2.13-2** Nikkei/HSI/USD-JPY/crude/gold 수집 — Yahoo Finance chart API 재사용
- **Phase 2.13-3** minyoung-mah upstream usage patch (v0.1.2, master push) + consumer 배선
- **Phase 2.13-5** daemon application heartbeat (5 daemon, Redis TTL key)
- **Phase 2.13-6** cloudflared `--metrics :2000` 활성화 + dashboard 가시화
- **Ad-hoc**: Logs Loki 쿼리 라벨 `{app}` → `{service}` (v2 포팅 누락 버그)

**스킵**: 2.13-4 (Macro 모델 스위치 — 2-3주 shadow 데이터 대기), 2.13-7 (real mode 체크리스트 — 운영 결정 필요)

## 시작 상태 진단

사용자가 세션 resume 직후 "UI Logs 가 비어있음" 질문. Loki 16 서비스 전부 수집 중인데 dashboard 쿼리만 `{app=...}` 쓰고 있었음 — compose 서비스에 `labels.app` 이 없어서 항상 0건. v2 포팅 때 labels 블록이 빠졌던 건. `{service=...}` (compose 자동 부여) 로 교체해 즉시 반영.

## Phase 2.13-1 — 전역 매크로 뉴스 파이프라인

### 신규 패키지 `prime_jennie_runtime/news_pipeline_global/`
- **feeds.py**: WSJ / Bloomberg / Reuters RSS 카탈로그. env `GLOBAL_NEWS_FEEDS_JSON` override.
- **rss_crawler.py**: feedparser + httpx 병렬 크롤. 피드 1개 실패는 전체 차단 안 함. `asyncio.to_thread` 로 sync feedparser 우회.
- **storage.py**: asyncpg upsert (articles) + digest upsert.
- **summarizer.py**: DeepSeek chat V3.2 한국어 6-10줄 요약. 키 없거나 실패 시 headlines+소스카운트 fallback.
- **pipeline.py**: `crawl_cycle()` (idempotent RSS 긁기) + `build_digest()` (최근 24h LLM 요약).

### 스키마 (migration 011)
- `global_macro_news_articles`: article_id (SHA256(url)[:32]) PK, source/feed_name/title/description/published_at/fetched_at
- `global_macro_news_digests`: digest_date PK, digest_id/summary/headlines/raw_count/source_counts (JSONB)/summary_model

### Scheduler (job_worker 추가 2건)
- `global_news_crawl`: `0 */2 * * *` (2h 간격 RSS)
- `global_news_digest`: `30 7,11 * * 1-5` (Macro Council cron `40 7,11 * * 1-5` 직전 fresh digest)

### Feeder 교체
`RealWsjDigestFeeder` 가 Phase 2.12 때 `us_market_daily + Redis snapshot` 에서 factual digest 합성하던 것을 `global_macro_news_digests` 테이블 직접 조회로 교체. 최근 36h 내 digest 없으면 기존 합성 fallback 유지.

### 중간 발견 — WSJ RSS 폐지
`feeds.a.dj.com/rss/RSSMarketsMain.xml` 등 WSJ 공식 RSS 가 2025-01-27 이후 업데이트 중단된 상태. 첫 smoke 에서 WSJ 40건 전부 2025-01 frozen 으로 나옴. Reuters 와 동일하게 Google News site-filter (`site:wsj.com+when:1d`) 경유로 교체해 fresh 확보.

### 실측 (2026-04-18 저녁)
- Articles: **259건** (WSJ 100 / Bloomberg 59 / Reuters 100 via Google News)
- Digest: raw_count=50 (24h lookback), model=deepseek-chat, 한국어 8줄
- 주요 헤드라인: 호르무즈 re-opening, 트럼프-이란 긴장완화, Cerebras IPO, Meta 구조조정 등

## Phase 2.13-2 — 해외 지수/환율/원자재

`council_macro.macro_collect_global` 에 Yahoo 5 ticker 추가. 기존 `_fetch_us_latest` 헬퍼 그대로 재사용 — 5d 창에서 prev/latest close + change_pct.

- `^N225` → nikkei_close, nikkei_change_pct
- `^HSI` → hsi_close, hsi_change_pct
- `JPY=X` → usd_jpy, usd_jpy_change_pct (schema 엔 usd_jpy 만)
- `CL=F` → crude_oil, crude_oil_change_pct
- `GC=F` → gold, gold_change_pct

`RealMarketSnapshotFeeder` 가 이 필드들을 실값으로 읽어 MarketSnapshot 에 채움. Nikkei/HSI 는 `IndexPoint(close, change_pct)` 로 업그레이드. 나머지는 단일 float + _change_pct.

**`check_closed_conditions` 바이너리 룰은 이 필드들을 참조하지 않음** — 따라서 gate 거동은 불변, 단지 Macro LLM prompt 의 reasoning context 가 풍부해짐.

### 실측 (2026-04-18 저녁 Yahoo)
Nikkei 58,475 (-1.75%) · HSI 26,160 (-0.89%) · USD/JPY 158.58 · Crude $84 (-11.29% 호르무즈 re-open 실반영) · Gold $4,849 (+1.34%)

## Phase 2.13-3 — minyoung-mah usage patch

### 문제
minyoung-mah 0.1.1 Orchestrator 가 LangChain `AIMessage.usage_metadata` 를 `RoleInvocationResult.metadata` 에 실어주지 않아, consumer (persistence.py) 가 prompt+output 문자수로 ±20% 추정. Anthropic/OpenAI/DeepSeek 모두 표준으로 usage 를 제공하지만 단절.

### Upstream (youngs75/minyoung-mah master → ec31e92)
- `_invoke_structured`: `with_structured_output(..., include_raw=True)` 전환. include_raw 모르는 구식 provider 는 TypeError 로 graceful fallback. parsing_error 는 FAILED 승격.
- `_invoke_loop`: 매 iteration AIMessage usage 누적 → 최종 metadata `{input_tokens, output_tokens, total_tokens}`
- `_extract_usage`: `usage_metadata` 기본 + `response_metadata.usage`/`token_usage` + OpenAI naming (prompt_tokens/completion_tokens) 호환
- pyproject version 0.1.1 → 0.1.2
- tests: 4 신규 (fast path propagates / no-usage empty / tool loop accumulates / openai naming)
- **PyPI publish 는 미수행 — 사용자 액션 대기**

### Consumer (prime-jennie-runtime)
- `persistence._real_cost`: usage dict × _PRICING → 정확 비용
- `persistence._resolve_cost`: cost_usd 직접값 → metadata.usage 실측 → char 추정 순 priority. 0.1.1 에서도 graceful fallback.
- `persist_macro_run` / `persist_scout_run`: `_estimate_cost` 직접 호출 → `_resolve_cost` 교체
- `pipeline._shadow_macro`: shadow PipelineStepResult.outputs[0].metadata 를 shadow_payload["metadata"] 로 전달 — shadow 비용도 실측 usage 사용 가능
- `minyoung-mah>=0.1.0` 유지 (PyPI publish 전까지 하위 호환)

PyPI publish 되는 순간 다음 slow-loop 재기동 시 자동으로 "실측 usage 우선" 경로 활성. 코드 재배포 불필요.

## Phase 2.13-5 — Daemon application heartbeat

### 동기
Docker `container state` 는 프로세스 생존만 알려줌 — scheduler/consumer 가 deadlock 돼도 "running". 관측 공백.

### 구현
- **`infra/heartbeat.py`** 신규
  - `HeartbeatPublisher`: start 시 즉시 1회 publish → interval(30s) 마다 TTL(90s) key 갱신. stop 은 task cancel + CancelledError suppress.
  - `read_heartbeat`: age_seconds/started_at/pid 반환. 파싱 실패는 None.
- **5 daemon app.py** (slow-loop, fast-loop, news-pipeline, price-scheduler, job-worker) 의 `run()` 이 redis_client 생성 직후 HeartbeatPublisher 기동 + AsyncExitStack 에 `stop` 등록.
- **dashboard/routers/system.py**
  - `_check_daemon` 이 heartbeat 먼저 → 없으면 docker container state fallback
  - container running + heartbeat 없음 = `unhealthy` ("loop deadlock 의심") — 기존 "running=healthy" 오인 해소
  - `DASHBOARD_DAEMONS` env 로 daemon 목록 override (빈 문자열 → daemon 체크 off, tests 용)
  - lazy redis client (모듈 스코프 1개 공유)

### 검증 (MS-01)
Redis `heartbeat:{service}` 5개 키 전부 존재. `/api/system/health` 가 `heartbeat 7~9s ago (pid=1)` 메시지로 표시.

### 부수 fix
Phase 2.10 session 에서 daemon docker 체크 추가되며 깨져 있던 `test_health_mixed` 가 env flag 도입으로 통과 회복.

## Phase 2.13-6 — cloudflared metrics

### 문제
`docker-compose.yml` 의 cloudflared command 가 `tunnel --no-autoupdate run --token ...` 뿐이라 metrics 포트 미활성. `scripts/phase_2_10_full_smoke.py` 의 cloudflared probe (`:2000/metrics`) 가 항상 `connection refused`.

### 수정
- compose cloudflared command: `--metrics 0.0.0.0:2000` 추가. `127.0.0.1:2000:2000` 바인드 (외부 비공개).
- dashboard `_DEFAULT_TARGETS` 에 `cloudflared: http://cloudflared:2000/ready` 추가 — UI 에 터널 상태 표시.

### 검증
`/ready` 가 `{"status":200,"readyConnections":4,...}` 응답. dashboard UI 에 `cloudflared healthy` 표시.

## 커밋 목록 (main)

```
e41e81c feat(cloudflared): metrics :2000 활성화 + dashboard health target 등록 — Phase 2.13-6
a20d748 feat(infra): daemon application heartbeat + dashboard 우선 조회 — Phase 2.13-5
1437c5f feat(slow_loop): minyoung-mah 0.1.2 의 실측 usage 소비 — Phase 2.13-3
50c4b55 feat(macro): Nikkei/HSI/USD-JPY/crude/gold 수집 + feeder 배선 — Phase 2.13-2
18dc872 fix(news_pipeline_global): WSJ 공식 RSS 는 retired — Google News site-filter 로 교체
aa61879 feat(news_pipeline_global): WSJ/Bloomberg/Reuters RSS 크롤러 + LLM digest — Phase 2.13-1
6b2e14a fix(dashboard): Logs Loki 쿼리 라벨을 app → service 로 교체
```

Upstream (`youngs75/minyoung-mah` master):
```
ec31e92 feat(orchestrator): RoleInvocationResult.metadata 에 provider usage 전파 — v0.1.2
```

## 운영 현황

**11 v3 서비스 전부 healthy** (dashboard `/api/system/health`):
```
kis-gateway        ok
dashboard/monitor/control-ui healthy
telegram-bot       ok
cloudflared        healthy                           ← NEW (2.13-6)
slow-loop/fast-loop/news-pipeline/price-scheduler/job-worker  healthy + heartbeat  ← NEW (2.13-5)
```

**운영 모드**: paper (실매매 0). Macro Council 다음 실행 (2026-04-21 월 07:40 KST) 부터 real digest + real Asia/commodities feed 기반 판단. Shadow 누적도 계속.

## 결정 이력

1. **WSJ RSS → Google News site-filter 경유** (2026-04-18)
   - 이유: 공식 RSS 가 2025-01-27 이후 업데이트 중단 확인됨
   - 영향: Reuters 와 동일 패턴으로 일원화. 크롤러 입장에선 RSS 하나 추가일 뿐
2. **minyoung-mah upstream 먼저, PyPI publish 는 사용자 결정** (2026-04-18)
   - 이유: PyPI publish 는 외부 노출 행위라 lead 가 독단 결정 부적절
   - 영향: 현재는 char 추정 fallback 으로 기존 동작 유지. publish 후 자동 승격
3. **Heartbeat interval 30s / TTL 90s** (2026-04-18)
   - 이유: Redis 부하 무시할 수준, TTL = 3×interval 이면 일시 지연 흡수
   - 영향: deadlock 감지는 90s 지연. 더 타이트하게 원하면 env 변수화 가능 (미구현)
4. **cloudflared 포트 127.0.0.1 바인드** (2026-04-18)
   - 이유: metrics 는 호스트 smoke probe 용. 외부 노출 불필요
   - 영향: 원격 모니터링 필요 시 reverse proxy 경유 또는 compose 수정

## Phase 2.13 잔여 / Phase 2.14 후보

- **2.13-4 Macro 모델 스위치 결정** — 2-3주 shadow 데이터 누적 후 (2026-05-05 이후 검토)
- **2.13-7 Real mode 전환 체크리스트** — 사용자 결정 대기 (.env KIS_IS_PAPER=false + 토큰 파일 + 재기동)
- **minyoung-mah PyPI 0.1.2 publish** — `cd ~/projects/minyoung-mah && uv build && uv publish` (또는 twine)
- **Heartbeat interval/TTL env 변수화** — 현재 하드코딩
- **Global macro news 확장** — Nikkei 전용 피드, 경제 지표 캘린더 (Trading Economics 등)
- **news_pipeline_global digest summary 분량 조정** — 현재 6-10줄, Macro prompt 에 삽입되는 길이 최적화

## 참고 명령

- 수동 crawl: `docker exec -e ... prime-jennie-runtime-job-worker-1 python -c "..."`
- Macro snapshot 확인: `docker exec prime-jennie-runtime-redis-1 redis-cli ... GET macro:data:snapshot:{date}`
- Heartbeat 확인: `docker exec prime-jennie-runtime-redis-1 redis-cli ... KEYS "heartbeat:*"`
- Cloudflared ready: `curl http://localhost:2000/ready` (MS-01 에서)
- Digest 확인: `docker exec prime-jennie-runtime-postgres-1 psql -U pj_admin -d prime_jennie_v3 -c "SELECT digest_date, raw_count, summary_model, LEFT(summary, 80) FROM global_macro_news_digests ORDER BY digest_date DESC LIMIT 3"`
