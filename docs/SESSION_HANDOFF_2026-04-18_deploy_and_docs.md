# 세션 핸드오프 — 2026-04-18 (promtail 재회귀 재수정 + 배포 파이프라인 + 패밀리 docs 최신화)

**세션 범위**: logs_and_vllm_fp8 세션 직후 헬스체크에서 promtail 필터 재회귀 발견 → `docker_sd_configs.filters` 로 근본 재수정 → GitHub Actions 자동 배포 파이프라인 구축 → 패밀리 4 리포 docs 최신화 → systemd 영구화
**시작**: 2026-04-18 22:00 KST (logs_and_vllm_fp8 커밋 42분 후) · **마감**: 같은 날 자정 경
**팀**: lead 단독 (Opus 4.7 1M)
**운영 영향**: promtail drop 46/s → 0, push-to-main 자동 배포 가능, 패밀리 문서 drift 정리, MS-01 재부팅 시 runner 자동 기동

## 시작 상태 진단

직전 logs_and_vllm_fp8 세션에서 "promtail keep 필터로 v2 컨테이너 제외, dropped 소수 안정화" 로 종료됐었음. 본 세션 처음에 `docker exec ... wget promtail:9080/metrics` 로 30s delta 측정해보니:

```
promtail_dropped_entries_total{reason="ingester_error"}: +1,375 / 30s  (초당 46건)
promtail_sent_entries_total: +10 / 30s
```

**Sent vs Dropped = 1:137**. 필터 주장은 있었지만 실제로는 계속 폭주. Loki 400 응답 재확인:
```
error: "at least one label pair is required per stream"
```
핸드오프에 기록된 `reject_old_samples` 가 **아니라** label 없는 stream push 문제. 근본 원인 재진단 필요.

## 이슈 맵 (3건)

| # | 증상 | 근본 원인 | 범위 |
|---|---|---|---|
| 1 | promtail 의 `keep` relabel 필터가 promtail 3.3.2 + docker_sd 환경에서 동작 안 함 — `__meta_docker_container_label_com_docker_compose_project` 값이 `prime-jennie` (v2) 인 컨테이너들이 여전히 active tail 되어 Loki 에 label 없는 stream push | 관측은 `promtail /targets HTML` 에 v2 컨테이너 10개 active. 원인 불명(regex 문법, 소스 필드명, 버전 regression 중 하나) | Logs UI 정상 (v3 stream 도 함께 push 되므로 UI 체감 없음) 이지만 Loki 부하 + 향후 v2 재시작 시 장애 재발 위험 |
| 2 | v3 가 GitHub Actions 자동 배포 없음 | `.github/workflows/` 디렉터리 자체 부재. v2 (`youngs7596/prime-jennie`) 는 3 workflow (CI/GHCR/Deploy) 가 있지만 v3 repo 는 초기 부트스트랩 시 workflow 누락 | 수정마다 수동 scp + docker compose restart. 휴먼 에러 여지 |
| 3 | Phase 2.10~2.13 6 handoff 분산 + README 의 "postgres/redis 만 docker compose up" 단독 기술 등 문서 drift | 2026-04-18 하루 6 세션 연달아 진행되며 누적 결과물 대비 상위 문서 업데이트 누락 | 초신자 진입 장벽, 운영 절차 기억 의존 |

## 수정 내용

### 1. promtail 재수정 — `docker_sd_configs.filters` (commit `8be4786`)

keep relabel 의존에서 탈피. Docker API 단에서 label 필터 적용 → v2 컨테이너는 아예 discovery 대상에서 제외.

```yaml
# infra/promtail/promtail-config.yaml
docker_sd_configs:
  - host: unix:///var/run/docker.sock
    refresh_interval: 5s
    filters:
      - name: label
        values: ['com.docker.compose.project=prime-jennie-runtime']
relabel_configs:
  # belt-and-suspenders: anchored regex 로 명시 (`^...$`)
  - source_labels: ['__meta_docker_container_label_com_docker_compose_project']
    action: keep
    regex: '^prime-jennie-runtime$'
  ...
```

**검증**:
- promtail 재기동 후 `added Docker target` 16건 전부 v3 (v2 0건)
- 30s delta: `dropped: 0 → 0`, `sent: 98 → 132` (+34, 정상 flow)
- Loki `/label/service/values` 응답에 16 v3 서비스 전부 등록

### 2. GitHub Actions 자동 배포 파이프라인

- **`.github/workflows/deploy.yml`** 작성 (commit `7e4ac44`). push to main 시 MS-01 self-hosted runner → `/home/youngs75/projects/prime-jennie-runtime` 에서 `git pull --ff-only` + `docker compose up -d --build`. 로컬 작업 트리 변경이 있으면 stash 로 보존.
- **`ms-01-v3` runner 등록**. v2 전용 `~/actions-runner` 는 repo-level 등록이라 v3 job 을 받지 못함. `~/actions-runner-v3` 별도 디렉터리 생성 + `config.sh` 실행. 처음 `bin` 심볼릭 링크가 원본 v2 경로를 가리켜 Runner.Listener 가 v2 config 를 읽는 문제 발생 → 심볼릭 대신 실제 `cp -r bin.2.333.1 bin` 로 교체 후 정상 등록.
- **profile 누락 수정** (commit `f43015b`). 첫 deploy 가 8초 만에 끝난 이유 추적 → 16 서비스 중 14개가 `profiles: ["full"]` / `["observe", "full"]` 등이어서 `--profile full` 없이는 postgres/redis 2개만 touch. workflow 에 `--profile full` 추가.

### 3. v2 legacy 12 컨테이너 재퇴역 (재발견)

Real mode 세션(2026-04-18 오전) 에서 `docker update --restart=no + stop + rm` 으로 제거했던 v2 12개가 재부팅 또는 v2 compose up 실수로 부활해 있었음. 그 중 `prime-jennie-kis-gateway-1` 이 port 8080 점유 → 2차 deploy 에서 v3 kis-gateway recreate 실패 → v3 5 서비스 (kis-gateway/dashboard/monitor/fast-loop/price-scheduler) 가 `Created` 상태로 갇힘.

조치: 다시 `docker update --restart=no | stop | rm` 로 12개 일괄 제거. 이후 로컬에서 `ssh prime-jennie 'cd ~/projects/prime-jennie-runtime && docker compose --profile full up -d'` 로 v3 5 서비스 재기동.

### 4. 패밀리 docs 최신화 (6 문서, 3 리포)

3 Explore agent 병렬로 원시자료 수집 (v3 startup + real mode / control-ui architecture / minyoung-mah consumer), 핸드오프 6 파일 직접 읽어 요약 정리.

| 파일 | Repo | 성격 |
|------|------|-----|
| `README.md` (재작성) | prime-jennie-runtime | 17 서비스 풀 스택 + 12 migration + 27 scheduled_jobs + 배포 파이프라인 + 운영 모드 |
| `docs/PHASE_2_13_COMPLETE.md` | prime-jennie-runtime | Phase 2.10→2.13 완료 보고 + 컴포넌트별 완성도 + Phase 3 경계 |
| `docs/SESSION_HANDOFF_TIMELINE.md` | prime-jennie-runtime | 6 핸드오프 시간순 인덱스 + 의존 그래프 + 주제별 교차 참조 |
| `docs/REAL_MODE_MIGRATION_CHECKLIST.md` | prime-jennie-runtime | paper→real 체크리스트 + 5-layer 이중 차단 검증 + 롤백 + gotchas |
| `docs/ARCHITECTURE.md` | prime-jennie-control-ui | 9 페이지 + API 계약 + 폴링 주기 + hand-mirror 타입 drift |
| `docs/CONSUMER_SETUP_FAQ.md` | minyoung-mah | 10 Q (editable install/shared_state/metadata usage/fast path 조건/버전 호환) |

v2 (`prime-jennie`) 는 사용자 지시로 제외 ("굳이 갱신할 필요없다").

커밋 체인:
- prime-jennie-runtime `ddaeb3a`
- prime-jennie-control-ui `3ca32cd`  
- minyoung-mah `167fb1f` (master branch, identity youngs75 repo 사용)

`docs/**` + `*.md` 는 deploy workflow 의 `paths-ignore` 로 커버 → deploy 재트리거 없음.

### 5. systemd 영구화

사용자 sudo 비밀번호 제공으로:
- `actions.runner.youngs7596-prime-jennie-runtime.ms-01-v3.service` systemd 등록 (`sudo ./svc.sh install youngs75 && sudo ./svc.sh start`)
- 초기 설치 후 GitHub 측 tmux 세션 잔재로 `Conflict. Retrying until reconnected.` 지속 → `sudo ./svc.sh uninstall` + `./config.sh remove --token` 로 완전 unregister + 신규 registration-token 으로 재등록 → clean online 상태 확보. Runner id 2 → 3.
- `docker-compose.yml` 16 서비스 전부 `restart: unless-stopped` 확인 → Docker 데몬 기동 시 자동 복구 → runtime / control-ui 용 별도 systemd unit 불필요.

## 배포 & 검증

- promtail fix 배포: 로컬 edit → scp → `docker restart promtail` (workflow 작성 전이라 수동). `sent +34/30s, drop 0`.
- workflow 파일 push: ms-01-v3 runner 에서 `f43015b` 실행 (workflow_dispatch)
- 수동 `docker compose --profile full up -d` 로 Created 상태 5 서비스 기동. 16 v3 컨테이너 전부 Up 확인.
- systemd runner 최종 확인: `systemctl is-active ...` = `active`, GitHub API `status = online, busy = false`, journal `Listening for Jobs`.

## 결정 이력

1. **promtail 근본 해결은 Docker SD filters, relabel keep 은 안전망** (2026-04-18 22:xx)
   - 이유: relabel keep 이 promtail 3.3.2 + docker_sd 조합에서 왜 회피되는지 원인 확정 못함 (regex 문법은 정상, 라벨 소스도 맞음). 상위 레이어 (Docker API) 에서 차단하면 regex 동작 여부와 무관
   - 영향: promtail 메이저 업그레이드 시에도 filters 는 Prometheus 공통 API라 호환 유지

2. **Runner 는 v3 repo 전용 2번째 등록 (A 옵션)** (2026-04-18 22:xx)
   - 대안 B (webhook script) / C (cron pull) 대비 idiomatic + v2 runner 와 독립 운영
   - 비용: 프로세스 하나 추가, registration token 발급 필요

3. **Workflow 은 runner workspace 가 아니라 `/home/youngs75/projects/prime-jennie-runtime` 에서 실행** (2026-04-18 22:xx)
   - 이유: v2 방식(runner workspace) 은 bind-mount 경로 이전으로 첫 배포 때 전 컨테이너 recreate. v3 는 기존 working_dir 유지 → 의도적 변경만 반영
   - 영향: git 이 source of truth. 로컬에서 MS-01 파일 직접 수정 금지 (stash 로 보존은 되지만 다음 pull 에서 덮어씀)

4. **`--profile full` workflow 필수** (2026-04-18 22:xx)
   - 이유: 16개 서비스 중 14개가 `profiles` 지정. profile 미지정 = postgres/redis 만 처리
   - 영향: full 프로필은 build 10 + image 6. 첫 배포 2-3분, 이후는 layer cache 로 10-30초

5. **v2 legacy 는 재발견 시마다 수동 퇴역** (2026-04-18 22:xx)
   - 이유: v2 compose `docker compose up` 재실행으로 부활 가능. 자동 감시는 과도
   - 영향: 다음 세션 체크에 "v2 legacy 검증" 명령 포함 (README 하단 + TIMELINE 문서에 반영)

6. **v2 docs 는 갱신 제외** (사용자 지시)
   - 이유: 유지보수 모드라 drift 허용. v3 포팅 항목 가시화는 후순위
   - 영향: 패밀리 docs 는 v3 + control-ui + minyoung-mah 3 리포만

## 후속

- **월요일(2026-04-20) 09:30 KST 첫 scout tick 관측**: `scout_runs` + `screening_candidates` + `position_sheets` 생성 + Logs UI 4 탭 표시 확인 (logs_and_vllm_fp8 세션의 미완 후속)
- **FP8 KV 영향 모니터**: `news_sentiments.score` 일일 mean/std 가 fp16 대비 왜곡 없는지
- **minyoung-mah PyPI 0.1.2 publish**: `cd ~/projects/minyoung-mah && uv build && uv publish` (사용자 액션)
- **Macro 모델 스위치 결정**: 2-3주 shadow 데이터 누적 후 (2026-05-05~)
- **배포 파이프라인 개선 후보**:
  - build-only 이미지 여부 판단하는 path filter (infra/docker/ 변경만 rebuild)
  - 실패 시 Telegram 알림
  - Green-blue 배포 (현재는 in-place recreate, 약 30s 다운타임 존재)
- **백테스트 엔진** (Phase 3 첫 항목): screening_candidates + context_snapshot 기반 재현 실행 + 성과 평가

## 참고 명령

- promtail drop 측정: `ssh prime-jennie 'docker exec prime-jennie-runtime-job-worker-1 python3 -c "import httpx,time; a=httpx.get(\"http://promtail:9080/metrics\").text; time.sleep(30); b=httpx.get(\"http://promtail:9080/metrics\").text; ..."'`
- Workflow 현황: `gh run list --repo youngs7596/prime-jennie-runtime --limit 5`
- Runner 상태: `ssh prime-jennie 'systemctl is-active actions.runner.youngs7596-prime-jennie-runtime.ms-01-v3.service'`
- v2 legacy 검증: `ssh prime-jennie 'docker ps -a --format "{{.Names}}" | grep -E "^prime-jennie-" | grep -v "^prime-jennie-runtime-" | grep -v -E "^prime-jennie-(vllm|qdrant)"'` (비어있어야 정상)
- 수동 배포 트리거: `gh workflow run deploy.yml --repo youngs7596/prime-jennie-runtime`

## 커밋 체인

```
runtime    8be4786  fix(promtail): docker SD filters 로 v2 컨테이너 소스 필터 — relabel keep 회귀 재수정
runtime    7e4ac44  ci: self-hosted GitHub Actions deploy workflow for v3 runtime
runtime    f43015b  ci(deploy): --profile full 로 전 서비스 포함 (16개)
runtime    ddaeb3a  docs: README 재작성 + Phase 2.13 완료 + 세션 타임라인 + real mode 체크리스트
control-ui 3ca32cd  docs: ARCHITECTURE.md 신규 — 9 페이지 + API 계약 + 폴링 주기 + hand-mirror 타입 drift
minyoung-mah 167fb1f docs: CONSUMER_SETUP_FAQ.md — 10 Q 실제 소비자 경험 기반
```

## Addendum — UI 공백 재발 (세션 직후 23:xx)

세션 커밋 직후 사용자가 UI 공백을 다시 신고. `curl http://localhost/api/*` 전부 502. **오늘 3번째 UI 공백**이지만 근본 원인은 또 다름.

### 증상
- control-ui root (`GET /`) 200, `/api/*` 전부 502
- nginx 로그: `"dashboard could not be resolved (2: Server failure)"` — Docker DNS SERVFAIL
- (recovery 세션의 "cached old IP" 와는 완전히 다른 에러)

### 진단
```bash
docker inspect prime-jennie-runtime-dashboard-1 \
  --format "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}"
# 결과: (빈 문자열)
```

dashboard 와 kis-gateway 2개가 **`running` 상태인데 어느 네트워크에도 붙어있지 않음**. 다른 3개 (fast-loop/monitor/price-scheduler) 는 정상 `prime-jennie-runtime_default`.

### 근본 원인

세션 초반 배포 실패 (port 8080 v2 legacy 충돌) 로 5 서비스 (dashboard/kis-gateway/monitor/fast-loop/price-scheduler) 가 `Created` 상태로 남음. v2 legacy 정리 후 `docker compose --profile full up -d` 로 수동 start 했는데, **일부 Created 컨테이너는 network alias 설정 없이 start 됨**.

Docker compose 가 start 시 네트워크 attach 를 보장하지 않는 edge case. recreate 없이 start 만 하면 컨테이너명 alias 는 있지만 **서비스명 alias (`dashboard`) 가 누락**. nginx 는 `dashboard:8090` 을 쓰므로 resolve 불가.

### 시도 & 결과

1. `docker network connect prime-jennie-runtime_default <container>` — 컨테이너를 네트워크에 붙임. 하지만 **서비스명 alias 는 안 붙음** (container name 만). 여전히 nginx SERVFAIL
2. `docker compose up -d --force-recreate dashboard kis-gateway` — compose 가 네트워크 + 서비스 alias 전부 재설정. **즉시 해결**. 4 endpoint `/api/{system/health,macro/regime,scout/runs,portfolio/summary}` 200 복구

### 교훈

- 배포 실패 후 `Created` 상태 컨테이너를 복구할 때 `docker compose up -d` 만 쓰면 불완전 — **`--force-recreate` 필수**
- 진단 시 `docker inspect ... --format "{{range .NetworkSettings.Networks}}..."` 가 빈 값이면 네트워크 미부착 의심 (state=running 이어도)
- `docker network connect` 는 긴급 복구용으로 불충분. `--alias <service>` 옵션 필수. 한 번 연결한 이후엔 alias 추가 불가 (disconnect → reconnect 해야)
- 오늘 UI 공백 3번의 다른 원인들:
  1. nginx startup DNS 영구 캐시 (recovery 세션) → `resolver + set $upstream + proxy_pass $var` 로 해결
  2. promtail 폭주로 v3 stream 간접 영향 (logs_and_vllm_fp8) → docker_sd filter
  3. 배포 실패 후 Created 컨테이너 networkless start (본 세션) → force-recreate

## 다음 세션 시작 시 체크

```bash
# 1. 현재 v3 상태
ssh prime-jennie 'docker ps --format "{{.Names}}\t{{.Status}}" | grep prime-jennie-runtime | wc -l'
# 기대: 16 (전부 Up)

# 2. Runner systemd 상태
ssh prime-jennie 'systemctl is-active actions.runner.youngs7596-prime-jennie-runtime.ms-01-v3.service'
# 기대: active

# 3. GitHub 쪽 runner 상태
gh api repos/youngs7596/prime-jennie-runtime/actions/runners --jq '.runners[] | {name, status, busy}'
# 기대: status=online, busy=false (또는 배포 중이면 busy=true)

# 4. v2 legacy 잔존 검증
ssh prime-jennie 'docker ps -a --format "{{.Names}}" | grep -E "^prime-jennie-" | grep -v "^prime-jennie-runtime-" | grep -v -E "^prime-jennie-(vllm|qdrant)"'
# 기대: 비어있음 (vllm-llm, vllm-embed, qdrant 3개만 나오면 OK)

# 5. promtail drop 여부
ssh prime-jennie 'docker exec prime-jennie-runtime-job-worker-1 python3 -c "import httpx; r=httpx.get(\"http://promtail:9080/metrics\"); print([l for l in r.text.splitlines() if \"dropped_entries\" in l and \"ingester_error\" in l])"'
# 기대: 값이 고정 (증가하지 않음)

# 6. UI API 502 검증 (오늘 3번 재발 — 패턴 고정 체크)
ssh prime-jennie 'for p in /api/system/health /api/macro/regime /api/scout/runs?limit=5; do echo -n "$p: "; curl -s -o /dev/null -w "%{http_code}\n" "http://localhost$p"; done'
# 기대: 200 × 3. 502 나오면 네트워크 검증:
# ssh prime-jennie 'docker inspect prime-jennie-runtime-dashboard-1 --format "{{range \$k,\$v := .NetworkSettings.Networks}}{{\$k}} {{end}}"'
# 빈 값 = networkless → `docker compose up -d --force-recreate dashboard kis-gateway`

# 7. 최신 커밋
cd ~/projects/prime-jennie-runtime && git log --oneline -5
```
