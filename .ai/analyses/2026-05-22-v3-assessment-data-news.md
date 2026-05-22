# v3 Assessment — 데이터·뉴스 수급 도메인

작성: data-news 에이전트 / 2026-05-22 / v2-teardown 팀 Step 2
대상: v3 = `prime-jennie-runtime` 코드 + MS-01 라이브 (`prime_jennie_v3` postgres)
방법: v3 코드 정독(file:line) + 라이브 DB 데이터 + git 이력. 비교 기준선은
`2026-05-22-v2-teardown-data-news.md` §4 의 17개 훅.
주의: v2-native **거래 성과는 미검증** — 본 문서는 (a) 설계 (b) v3 라이브 데이터 두 축만 비교.
"v3가 v2보다 돈을 못 번다" 식 판정 없음.

---

## 1. 훅별 판정표

| # | 훅 | 판정 | 근거 |
|---|---|---|---|
| 1 | Stream fan-out 구조 | **CHANGED** (의도적 축소) | v2: 1 stream→2 group(analyzer+archiver). v3: 1 stream→1 group(extractor). Qdrant/archiver 제거. group 기반 확장성은 유지 (`pipeline.py:57-67,82`) |
| 2 | 미ack 크래시 복구 | **KEPT** | extractor 가 `last_id="0"` 먼저 읽고 없으면 `">"` (`pipeline.py:140-154`) |
| 3 | LLM 호출 전 PG precheck | **NEW-DEFECT (복구됨)** | v3 재작성 때 *상실* → 운영 사고 → 2026-05-03 재추가. §4.1 |
| 4 | 다층 dedup | **KEPT (실질)** | Redis SET 3일 TTL(v2 동일 키스킴) + PG precheck. v2 의 in-proc 해시·archive SET 은 제거된 컴포넌트용 (`dedup.py`, `naver_crawler.py:18`) |
| 5 | 소스단 노이즈 필터 | **KEPT** | `NAVER_NOISE_KEYWORDS` 18종 v2 동일, 크롤 단계 컷 (`naver_crawler.py:47-66,123`) |
| 6 | 뉴스 소스 다양성 | **부분 IMPROVED** | 국내는 네이버 단일(v2 동일). 단 별도 `news_pipeline_global` 추가 — Bloomberg RSS + Google News(wsj/reuters) (`feeds.py:34-42`) |
| 7 | 감성 입력 깊이 | **NOT FIXED (악화 소지)** | 여전히 헤드라인-only. §4.2 |
| 8 | DART/공시 활용 | **NOT FIXED** | v3 도 `stock_disclosures` 수집만, slow_loop/briefing 소비처 0건 (grep) — v2 와 동일 dead data |
| 9 | 감성 시간가중 | **IMPROVED** | `NewsEventEntry.staleness_hours` 노출 + scout 프롬프트 "staleness>48h 면 뉴스 미사용" (`scout_feeder.py:48-49`, `scout/prompts.py:191,204`) |
| 10 | LLM tiering·비용 | **KEPT** | 고빈도 국내 추출 = 로컬 vLLM Qwen3-30B-4bit (DB `model` 컬럼 확인, 무료). 글로벌 요약만 DeepSeek 유료 — v2 WSJ 도 Claude 유료였음 |
| 11 | 비용 가시성(USD) | **미검증** | 본 세션에서 깊이 확인 안 함 — step3 우선순위 낮음 |
| 12 | contract smoke test | **KEPT** | `jobs/maintenance.py:116` v2 포팅, "임계값 다시 튜닝 말 것" 주석. 깨지면 `ContractSmokeError` raise |
| 13 | 데이터 흐름 명료성 | **CHANGED** | v2: 결정론 `_news_score` 팩터. v3: 구조화 event 분포 → scout LLM 생성코드가 조합 판단. 표현력↑ 결정성↓ |
| 14 | rate-limit 마진 | **KEPT** | 크롤러 `request_delay 0.3s` v2 동일 (`naver_crawler.py:79`) |
| 15 | 부분 실패 격리 | **KEPT** | ticker별 try/except(`pipeline.py:99-102`), `gather(return_exceptions=True)`(`pipeline.py:201`) |
| 16 | 감성 분포 편향 | **IMPROVED** | §3.4 — v3 라이브 분포가 v2 의 degenerate 편향보다 변별력 있음 |
| 17 | source provenance | **IMPROVED** | `news_articles.source_name` 에 언론사명 per-article 보존 (`pg_event_repo.py:42-50`). v2 는 'ANALYZER' 로 덮어썼음 |

---

## 2. 회귀 (Regression)

순수 회귀(v2 에 있던 게 v3 에서 사라져 그대로)는 **거의 없다.** 대부분의 v2 기능은 포팅됐고
(crawler/dedup/noise filter/smoke test/collect job 전부), 빠진 것은 의도적 제거(Qdrant)거나
재작성 중 잃었다가 사고 후 되찾은 것(precheck). 정직하게 적으면:

- **§4.1 (PG precheck)** 은 한때 회귀였다가 복구된 케이스 — 현 상태로는 회귀 아님.
- **헤드라인-only (훅 #7)** 는 v2 도 못 했으므로 회귀가 아니라 *미해결 유산*. 단 v3 가 본문 처리
  스캐폴딩을 깔아놓고 크롤러만 안 채워, "고친 줄 아는데 안 고침" 상태 — §4.2.
- **DART dead data (훅 #8)** 도 v2 유산 그대로 — 회귀 아님, 미해결.

→ "v3 가 v2 대비 잃은 것" 보다 "v3 가 재작성 이음매에서 새로 만든 결함"(§4) 이 본질.

---

## 3. 진짜 개선 (v3 가 실제로 더 나은 것)

### 3.1 단일 감성 점수 → 구조화 이벤트 메타데이터
v2 는 뉴스를 `sentiment_score` 0–100 하나로 압축했다. v3 `NewsEvent` 는 한 번의 LLM 호출로
`event_type`(16종) / `impact_level` / `sentiment` + score / `time_horizon` / `keywords` /
`sector_tags` / `financial_signals` / `confidence` 를 추출한다 (`models.py:95-114`). Scout 가
"점수 평균 비교"가 아니라 "high-impact 이벤트 조합"으로 판단할 수 있게 됐다
(`scout/prompts.py:185`). v2 의 핵심 약점(감성 평균의 낮은 변별력)을 구조적으로 우회.

### 3.2 신선도·기사수를 1급 신호로 노출
v2 scout 는 14일 단순평균(`news_sentiment_avg`)만 받았다 — 오늘·13일전 뉴스 동일 가중, 기사 1건
80점과 50건 80점 구별 불가. v3 `NewsEventEntry` 는 `article_count`, `staleness_hours`,
`latest_at`, `events_by_impact` 를 명시 노출하고 0건 ticker 도 명시적 entry 로 포함
(`scout_feeder.py:42-62`). v2 약점 2개(시간가중 없음 / 볼륨신호 없음)를 직접 해소.

### 3.3 Qdrant 제거 — failure domain 축소
v2 는 archiver→임베딩(vLLM KURE-v1)→Qdrant 라는 별도 소비 경로를 운영했다. v3 는 이를 통째로
제거하고 메타데이터 RDB 1경로로 단순화 (`pipeline.py:4-8`). 제거 근거가 코드에 명시돼 있다 —
"국내 금융 뉴스 semantic duplicate 특성상 벡터 DB 효용 낮음". 임베딩 서버·벡터DB·LangChain
의존·payload index 라는 운영 표면 하나가 통째로 사라졌다. 정당한 단순화.

### 3.4 감성 분포가 덜 degenerate (라이브 데이터)
v2 (§5.2): sentiment ≥60 이 70%, 부정(<40) 11%, <20 단 4건 — 사실상 무변별.
v3 라이브 `news_events` (308,943 행, 2026-04-21~05-22):
- sentiment: positive 65% / negative **19%** / neutral 16% — 부정 뉴스가 v2(11%)보다 잡힘.
- impact_level: medium 72% / high 16% / low 12% — 보수적 impact 루브릭(`exaone_extractor.py:71-86`,
  few-shot 5종) 효과로 medium 중심. (메모리 `news_eval_32fix`: impact_acc 60→90.6%.)
- LLM 파싱 실패 138/308,946 = **0.04%** — Qwen3 structured JSON 안정적.
완벽한 균형은 아니지만(여전히 positive 편향) v2 의 붕괴 수준보단 명백히 낫다.

### 3.5 source provenance 복원
v3 `news_articles.source_name` 이 언론사명을 per-article 보존(`pg_event_repo.py:42-50`).
v2 는 sentiment 저장 시 `source='ANALYZER'` 로 덮어써 출처를 잃었다(§5.2). v3 가 되돌렸다.

### 3.6 confidence 필드 — 실패와 저확신 구분
`NewsEvent.confidence`. LLM 파싱 실패 fallback 은 `confidence=0.0`(`exaone_extractor.py:192-208`),
정상 분류는 0.5~1.0. 데이터 소비 측이 "분석 실패분"을 거를 수 있다 — v2 엔 없던 신호.

---

## 4. 새 결함 (재작성 이음매 위주)

**공통 원인**: v3 는 v2 의 *sync-thread* 뉴스 파이프라인을 *async* 로 재작성했다. NEW-DEFECT 4건이
전부 그 이음매에서 났다. v2 가 안 갖던 버그를 재작성이 새로 만든 패턴이다.

### 4.1 PG precheck 상실 → 운영 사고 → 재추가 (★ 대표 결함)
git 이력이 명백하다:
- `efd5d4d` Phase 1 News Pipeline 신설 — precheck 없음.
- `c98d3dd` sentiment→event 전환 — 여전히 없음.
- `3a10556` (2026-05-03) **재추가**. 커밋 메시지: "기존 extractor 는 PG `news_events.article_id`
  중복을 보지 않고 무조건 vLLM(Qwen3-30B)을 호출 … 운영에서 시간당 distinct 4,170 article
  처리 / 신규 fetch 2,652(24h) 라 2배 이상 재분석. stream lag 7,411 누적 + GPU 줄곧 풀가동."

→ **v2 는 `_is_url_exists()` 로 이 방어를 갖고 있었다**(step1 §2.2). v3 는 재작성 중 그것을 잃고,
GPU 풀가동 + stream lag 7천건 사고를 겪고 나서야 도로 넣었다. v3 메모리
`feedback_news_pipeline_pg_precheck` 는 이를 "새로 학습한 규칙"으로 적지만 — 실상은 **v2 가
이미 갖던 방어를 잃었다 되찾은 것**. 현재는 복구됨(`pipeline.py:176-196`).

### 4.2 본문 처리 스캐폴딩만 깔고 크롤러는 빈 채 — "고친 줄 아는 미해결"
- 추출기 프롬프트에 `본문(발췌): {body_excerpt}` 필드 (`exaone_extractor.py:48,157-163`).
- `LiteLLMEventExtractor.body_excerpt_chars=400` 파라미터.
- `news_articles.body` 컬럼 + upsert (`pg_event_repo.py:42-48`).
- **그런데 크롤러가 `body=""` 하드코딩** — `naver_crawler.py:180` `body="",  # 상세 본문은 별도
  요청 필요 — Phase 2 scope 밖`.
- 라이브 검증: `news_articles` **313,512행 전부 body 빈 문자열** (`SUM((body='')::int)`=313,512).

즉 `body[:400] or "(본문 없음)"` 이 **항상 "(본문 없음)"**. v3 는 16종 event_type + 보수적
impact 를 *제목 한 줄*로 판정 중이다. v2 와 똑같은 헤드라인-only 인데, v3 는 스캐폴딩 때문에
겉보기엔 본문을 쓰는 것처럼 보인다 — 코드만 보면 놓치기 쉬운 함정.

### 4.3 published_at 9시간 shift (TIMESTAMPTZ 이음매)
`a3cf61f fix(news-pipeline): published_at KST→UTC 태깅 수정 (9h shift 버그)`. v3 가 DB 를
TIMESTAMPTZ 로 가면서, 네이버의 KST 로컬 시각을 `dt.replace(tzinfo=UTC)` 로 잘못 태깅 →
9시간 미래로 저장돼 UI 에 미래 시각 표시. 현재는 `tzinfo=KST → astimezone(UTC)` 로 수정
(`naver_crawler.py:200-213`). v2 는 naive datetime 이라 이 버그가 없었다 — 재작성이 만든 것.

### 4.4 asyncio cross-loop / InMemory dedup (재작성 이음매)
- `694e7a5 fix: asyncio.run() 제거 — cross-loop httpx 'Event loop is closed'` — sync→async
  전환 중 별도 event loop 생성으로 httpx 클라이언트가 깨짐.
- `698be35 perf: InMemoryDeduplicator → RedisDeduplicator` — v3 초기 운영이 **InMemory dedup**
  으로 돌았다 (프로세스 재시작 시 dedup 상태 소실, 다중 프로세스 미공유). v2 는 처음부터 Redis.
- `594220c`+`dd8a788 Revert` — vLLM 동시호출 세마포어 추가했다 되돌림 (eGPU 팬 spike). GPU
  thermal 과 파이프라인 동시성이 결합된 운영 이슈.

이 4건은 개별로는 다 수정됐지만, **묶어 보면 "async 재작성이 v2 에 없던 버그류를 새로 들여왔다"**
는 신호다. step3 의 회귀 가드 후보(§5.4).

---

## 5. step3 보완 후보 (우선순위·규모)

elaborate 설계 금지 — 후보와 규모만.

### 5.1 [HIGH / MEDIUM] 기사 본문 크롤
v2·v3 양쪽 통틀어 **가장 큰 미해결 약점**. 추출기·프롬프트·DB 컬럼이 *전부 준비돼 있고*
크롤러의 per-article 상세요청만 빠졌다 (`naver_crawler.py:180`). 신규 기사당 HTTP 1회 +
본문 파싱 추가 — `naver_crawler.py` 에 ~50–80줄. 16종 event_type·impact 판정을 제목이 아니라
본문 발췌로 하게 되면 추출 품질 상한이 직접 올라간다. dedup·precheck 가 신규분만 거르므로
추가 크롤 부하는 제한적.

### 5.2 [MEDIUM / SMALL-MEDIUM] DART 공시 → news_events 브리지
공시는 고신호·정형 데이터인데 v2·v3 모두 수집만 하고 버린다(훅 #8). v3 jobs 가 이미
`stock_disclosures` 를 채우므로, 공시 row 를 추출기에 태우거나 별도 event source 로
news_events 에 합류시키면 됨. 신규 크롤 없음 — 기존 데이터 재활용.

### 5.3 [LOW-MEDIUM / SMALL] `other` event_type 15% 진단
라이브 `news_events` 의 event_type `other` 가 46,137/308,943 = 15%. 표본 추출해 — (a) 16종이
못 잡는 실제 유형이 있는지 (b) 진짜 분류불가 노이즈인지 판별. 프롬프트 튜닝 or 노이즈 필터
보강으로 이어질 수 있음. 코드 변경 거의 없는 진단 작업.

### 5.4 [LOW / SMALL] async 재작성 이음매 회귀 가드
§4 NEW-DEFECT 4건이 전부 sync→async 재작성 이음매. stream 재처리 idempotency(동일 article_id
재진입 시 LLM 미호출) + tz round-trip(KST 입력→UTC 저장→KST 복원) 두 가지를 커버하는
통합테스트 소수. 이 *부류*의 재발을 막는 안전망.

### 비권장
Qdrant 복원 — §3.3 의 제거는 근거 있는 정당한 단순화. 되돌릴 이유 없음.

---

## 한계 (정직하게)

- 훅 #11(USD 비용 가시성) 은 본 세션에서 깊이 확인 안 함 — 우선순위 낮아 보류.
- `news_pipeline_global`(글로벌 RSS+DeepSeek 요약) 은 표면만 확인 — 본 평가의 무게중심은
  국내 뉴스+데이터 수집. global 파이프라인 정밀 해부는 별도 필요시.
- v3 라이브 데이터는 2026-04-21~05-22 1개월치 — 계절성/장기 드리프트는 판단 불가.
- §4.1 의 "운영 사고" 수치(stream lag 7,411 등)는 commit 메시지 인용 — 사고 당시 로그 원본은
  미열람.
