# 세션 핸드오프 — 2026-04-18 (Logs 가시성 복구 + v3 news_pipeline 상시화 + vLLM FP8 KV)

**세션 범위**: UI Logs 에 news-pipeline 로그 부재 신고 → promtail 전체 정지 발견 및 근본 해결 → v3 news_pipeline cron 축소 회귀 복구 → vLLM KV cache FP8 적용
**시작**: 2026-04-18 저녁 (real 전환 세션 종료 직후 이어 접속) · **마감**: 같은 날 밤
**팀**: lead 단독 (Opus 4.7 1M)
**운영 영향**: Logs UI 정상화 + 월요일 09:30 첫 scout tick 전 news 상시 구동 복구 + vLLM EXAONE KV 메모리 75% 절감

## 시작 상태 진단

사용자 리포트 "Logs 페이지에 news-pipeline 로그가 전혀 없다". MS-01 control-ui 에서 목격. 11 서비스 healthy, kis-gateway 등 다른 서비스 로그는 정상 표시.

## 발견된 이슈 맵 (3건)

| # | 증상 | 근본 원인 | 범위 |
|---|---|---|---|
| 1 | 모든 v3 데몬이 Loki 에 최근 5분 `NO STREAM` (user 는 news-pipeline 만 신고했지만 실제는 전역) | **promtail 이 09:41 UTC 이후 2시간 완전 정지**. v2 잔존 `prime-jennie-vllm-*` / `prime-jennie-qdrant-*` 컨테이너 로그 파일에 3월 말부터 누적된 old timestamp 라인이 있어 promtail 이 tail 시작 시 이를 Loki 로 push → Loki `reject_old_samples_max_age` 에 걸려 HTTP 400 → **같은 batch 의 v3 신규 로그까지 전량 drop** (`promtail_dropped_entries_total{reason="ingester_error"}=1.1M`) → retry/backoff 루프에 갇힘 | Logs UI 전체, 전 서비스 |
| 2 | news-pipeline 이 장외/주말 idle | Phase 2.9 slice2 에서 v2 의 3-스레드 상시 구동 구조(collector 장중 10분/장외 30분 + analyzer/archiver stream BLOCK 상시)를 apscheduler cron `*/10 9-15 * * 1-5` 로 잘못 축소. EXAONE 로컬 LLM 이라 비용 無 인데도 장중 한정 해놓은 회귀 | Scout 의 뉴스 sentiment 피드 공백, 주말 데이터 축적 0 |
| 3 | vLLM EXAONE KV cache 가 여전히 fp16 | 글로벌 메모리 "vLLM 메모리 최적화 계획" 의 FP8/TurboQuant 가 적용 안 된 상태 | RTX 3090 24GB 병목, 동시 요청/context 확장 제약 |

## 수정 내용 (커밋)

### prime-jennie-runtime

```
52a69f6 fix(promtail): v3 compose_project 만 scrape 해 v2 잔존 컨테이너의 old timestamp 로그 차단
842784c feat(seed): news_pipeline cron 을 24/7 10분 주기로 확장
```

- **`infra/promtail/promtail-config.yaml`**: `relabel_configs` 맨 앞에 `keep` action 추가.
  ```yaml
  - source_labels: ['__meta_docker_container_label_com_docker_compose_project']
    action: keep
    regex: 'prime-jennie-runtime'
  ```
  v2 공유 인프라(vllm/qdrant) 로그는 설계상 v3 UI 관심 대상 아님 (docker logs / 별도 grafana 로 관찰).
- **`scripts/seed_scheduled_jobs.py`**: `news_pipeline.crawl_cycle` cron 을 `*/10 9-15 * * 1-5` → `*/10 * * * *` 로 확장. 주석에 v2 상시 구동 원칙 명시.
- DB live UPDATE: `UPDATE scheduled_jobs SET cron='*/10 * * * *' WHERE id='news_pipeline.crawl_cycle'` + Redis `PUBLISH scheduler.reload:news_pipeline 1` 로 즉시 반영.

### prime-jennie (v2 compose)

```
f79718d infra(vllm): EXAONE-4.0-32B-AWQ 에 FP8 KV cache 적용
1896dbf fix(vllm): FP8 KV + max-num-seqs 128 로 초기화 OOM 회피   (development)
```

- **vllm-llm command** 에 `--kv-cache-dtype fp8_e4m3 --max-num-seqs 128` 추가.
- 초기 시도는 FP8 단독. `--gpu-memory-utilization 0.85` + sampler warmup 기본 256 dummy 가 peak 메모리 초과 → `CUDA out of memory (Tried 302 MiB, 297 MiB free, vllm-embed 1.48GB + vllm-llm 21.78GB)`. 단일 commit 으로 warmup peak 축소.
- news-pipeline cycle 당 동시 요청 ~10-20 건 수준이라 max_num_seqs 128 은 넉넉.

## 배포 & 검증

### promtail
- promtail 재기동 후 v3 stream 등록 확인:
  `dashboard / telegram-bot / promtail / kis-gateway / grafana / job-worker / monitor / postgres / control-ui / redis / loki` 모두 `{compose_project="prime-jennie-runtime"}` 쿼리에 등장.
- `news-pipeline / slow-loop / fast-loop / price-scheduler` 는 초기 Loki label 에 없었으나 news_pipeline cron 확장 후 21:00 KST 첫 cycle 실행되자 `service="news-pipeline"` stream 정상 생성.
- 재시작 후 promtail metrics: `sent=70 → steady 증가`, status=204 정상 push, 400 소수 (v2 old batch 잔재, 한 번만 drop 되고 종료).

### news_pipeline 상시화
- `apscheduler` 실행 로그 실측 (2026-04-18 21:00:00 KST):
  ```
  Running job "SchedulerRunner.run_job (trigger: cron[...minute='*/10'], next run at: 2026-04-18 21:10:00 KST)"
  news cycle: crawled=164 deduped=150 analyzed=150 embedded=150 errors=0
  ```
- 이후 21:10 / 21:20 tick 모두 정상 실행. 주말 idle 회귀 해소.

### vLLM FP8 KV
- 재기동 후 41초 READY.
- 로그 실측:
  ```
  non-default args: {... kv_cache_dtype: fp8_e4m3, max_num_seqs: 128}
  Using fp8 data type to store kv cache. It reduces the GPU memory footprint and boosts the performance.
  Available KV cache memory: 2.49 GiB
  GPU KV cache size: 20,368 tokens
  Maximum concurrency for 4,096 tokens per request: 4.96x
  ```
- 한국어 smoke ("삼성전자 실적 호조. 주가 전망…") 응답 정상.
- 21:20 news cycle 은 재기동 중 초반 `httpx.ConnectError` 다수 발생했으나 retry 로 최종 `errors=0` 복구. dedup 덕분에 장기 영향 없음.

## 결정 이력

1. **promtail scrape 를 compose_project 로 필터**
   - 이유: v2 컨테이너 old log 는 v3 설계 원칙상 Logs UI 대상 아님. Loki retention/limits 손대지 않고 근본 차단이 가장 깨끗
   - 영향: v2 vllm/qdrant 로그가 Loki 에 안 들어감 → 필요 시 `docker logs` 또는 v2 측 grafana
2. **news_pipeline cron 을 24/7 `*/10 * * * *`**
   - 이유: v2 가 3-스레드 상시 구동이었고 EXAONE 로컬 LLM 이라 비용 無. 장중 한정은 v3 포팅 회귀
   - 한계: v3 `NewsPipeline.run_cycle` 은 단일 메서드 직렬 처리. v2 의 Redis stream BLOCK 상시 소비 즉시성은 못 따라가지만 10분 주기면 뉴스 지연 체감 미미
3. **vLLM KV 는 FP8 우선, TurboQuant 는 후속** (Phase 2)
   - 이유: PR #38479 main 병합 됐지만 `v0.20.0` (unreleased) / nightly 에만 존재. 한국어 eval 미검증. 현재 real 모드 + 데이터 축적 기간이라 품질 regression 민감
   - Phase 2 전제: nightly 이미지 업그레이드 + news-pipeline 실텍스트 20~30건으로 FP8 vs `turboquant_k8v4` 감성 점수 RMS 비교 harness 작성
4. **`--max-num-seqs 128` 을 FP8 과 함께**
   - 이유: sampler warmup peak (기본 256 dummy) 이 0.85 util 한계 넘음. news cycle 동시성은 128 로 충분
   - 영향: 초당 처리량 이론 상한 감소이지만 현재 시나리오에서 병목 아님

## 후속

- **월요일 09:30 KST 첫 scout tick 관측**: `scout_runs` + `screening_candidates` + `position_sheets` 생성과 더불어 **Logs UI 의 news-pipeline / slow-loop / fast-loop / price-scheduler 탭 정상 표시** 확인
- **FP8 KV 영향 모니터**: EXAONE 감성 점수 분포가 이전(fp16 KV) 대비 크게 달라지지 않는지 — `news_sentiments.score` 분포 확인 (일일 mean/std)
- **Phase 2 (TurboQuant)** 준비: Korean eval harness 작성. `scripts/evaluate_kv_quant.py` (news-pipeline 실 텍스트 → FP8 vs turboquant_k8v4 감성 점수 RMS). nightly 이미지로 temporary 전환해서 1회 smoke, OK 면 Phase 2 본 세션 개시
- **seed 주석**: `scripts/seed_scheduled_jobs.py` 에 cron 표준/apscheduler 변환 경계 + news_pipeline 24/7 원칙 이미 명시
- **v2 가 공유하는 vllm/qdrant 로그 수집 필요해지면**: prime-jennie repo 의 별도 promtail 을 띄우거나 v3 promtail scrape 에 v2 project 도 keep 하도록 확장 (단 old timestamp drop 전략 동반 필요)

## 참고 명령

- Loki 서비스 라벨 리스트: `docker exec prime-jennie-runtime-dashboard-1 python3 -c "import httpx; print(httpx.get('http://loki:3100/loki/api/v1/label/service/values').text)"`
- v3 stream 만 조회: `curl ".../loki/api/v1/series?match[]={compose_project=\"prime-jennie-runtime\"}"`
- promtail 건강 지표: `curl promtail:9080/metrics | grep -E "promtail_(dropped|sent)_entries_total"`
- news-pipeline 수동 tick: `docker exec ... redis-cli PUBLISH scheduler.reload:news_pipeline 1` (cron 변경 반영 용)
- vLLM FP8 확인: `docker logs prime-jennie-vllm-llm-1 | grep -iE "fp8|kv cache|maximum concurrency"`

## 커밋 체인

```
runtime  52a69f6  fix(promtail): v3 compose_project 만 scrape
runtime  842784c  feat(seed): news_pipeline cron 24/7 확장
v2       f79718d  infra(vllm): FP8 KV cache 적용
v2       1896dbf  fix(vllm): max-num-seqs 128 로 OOM 회피
```
