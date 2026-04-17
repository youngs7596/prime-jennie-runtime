# 세션 핸드오프 — 2026-04-18

**세션 범위**: Phase 2.10 (v2 완전 대체 포팅 + 실전 배치)  
**시작**: 2026-04-17 늦은 저녁 · **마감**: 2026-04-18 새벽  
**팀 구성**: lead (Opus 4.7 1M) + 6 Opus teammate (Agent Teams experimental)

## 결과 요약

**12/12 task closing + v3 실전 배치 완료**. v2 22 컨테이너 전면 퇴역, v3 17 컨테이너 단독 운영.

## 완료된 Phase 2.10 task

| # | 담당 | 성과 |
|---|---|---|
| #1 | Track B | v2 job-worker 22ep → v3 25 handler, 73 tests, Dockerfile, scheduled_jobs seed |
| #2 | Track A | migrations/006–010 신설, 17 테이블 + 권한 |
| #3 | Track A | `scripts/mariadb_to_postgres_etl.py` + apply/rollback 테스트 |
| #4 | Track C | `prime_jennie_runtime/dashboard/` 9→8 routers + control 신규 + 18 tests |
| #5 | Track C | `prime_jennie_runtime/monitor/` 슬림 포팅 + Dockerfile |
| #6 | Track D | `prime_jennie_runtime/briefing/` LLMCaller 주입 + 17 tests |
| #7 | Track D | `prime_jennie_runtime/council_logging/` metadata_json 확장 + 26 tests |
| #8 | Track D | `prime_jennie_runtime/backtest/` 엔진 + 26 tests |
| #9 | Track E | utilities/prompts/reports 인벤토리 + 이관, 18 obsolete 정리 |
| #10 | Track F | `prime-jennie-control-ui` (youngs7596) 신규 repo, React 18/Vite/TS, 4 page + ConfirmDialog + X-User 헤더 |
| #11 | Track E | compose 통합 15 서비스 + profiles(apps/observe/tunnel/full) + smoke 스크립트 |
| #12 | lead | MS-01 cutover: ETL 1.47M rows, v2 stop, v3 up, smoke 10/10 |

## v3 실전 배치 현황

**17 컨테이너 Up**:
- v3 runtime (14): postgres · redis · kis-gateway · dashboard · monitor · slow-loop · fast-loop · news-pipeline · price-scheduler · job-worker · telegram-bot · promtail · grafana · loki · cloudflared · control-ui
- v2 공유 (3): qdrant · vllm-llm · vllm-embed (v3 가 127.0.0.1 로 접근, `.env` 에 연결)

**포트 노출**: :80 control-ui · :3300 grafana · :8080 kis-gateway · :8090 dashboard · :8091 monitor

**운영 모드**: **paper (시뮬 계좌)**. KIS_IS_PAPER=true, trading_flags 기본값, 실매매 0 리스크. Real 전환은 사용자 결정 시점에 `.env` 4줄 교체 + kis-gateway 재기동.

**데이터 축적**: 시세/뉴스/공시/펀더멘털/수급/지수/미장/AI 로깅 전부 paper 에서 실시장 데이터 수집 중. 실매매 관련 테이블(positions/trade_logs/daily_asset_snapshots)만 paper 계좌 기준.

**v2 MariaDB**: `~/backups/jennie_db_20260417_1615.sql.gz` (178M gzip) 보존 후 stop. 복구 필요 시 `docker compose -f ~/projects/prime-jennie/docker-compose.yml up -d mariadb`.

## 기술 이슈 & 해결 ledger

### ETL cutover 병목 2건
- **`is_active` TINYINT(1) → BOOLEAN**: asyncpg 기본은 int→bool 거부. `set_type_codec("bool", text encoder)` 로 해결. `scripts/mariadb_to_postgres_etl.py:607–613`.
- **composite PK ON CONFLICT**: `pk="(stock_code, trade_date)"` 를 `build_insert_sql` 이 `(( ... ))` 이중 괄호 생성 → Postgres syntax error. `migrations/_common.py:build_insert_sql` 에 괄호 감지 로직.

### Git worktree 격리 (Track 간 충돌)
공용 workdir 에서 `git checkout` 경쟁으로 Track B uncommitted 2회 유실. 전 Track 에게 `git worktree add /home/youngs75/projects/pj-track-{a..e}` 로 이전 강제. 이후 무사고. 주 공용 workdir 는 **lead 전용**으로 유지.

### lead 실수 + reflog 복구
공용 workdir 이 Track D branch 에 있던 걸 놓치고 Track C merge 를 잘못 건 후 `git reset --hard HEAD~1` 로 Track D 의 702cc77 council_logging commit 까지 삭제. reflog 에서 object 살아있어 `git branch -f` 로 복구. 이후부터 lead Bash 는 **`git branch --show-current` 선행 확인** 고정.

### telegram-bot CMD 버그
Dockerfile.telegram_bot 의 CMD 가 `python -m ... .app` 이라 FastAPI 앱을 import 만 하고 exit (main block 없음). `uvicorn ... --factory` 로 수정.

### daemon 가시성 (docker socket 통합)
pure async daemon 5종(slow/fast/news/price/job-worker) 은 HTTP 없음 → System UI 에서 connection refused 5개. Dashboard `/api/system/health` 에 **docker SDK 로 container state 조회** 추가 (`/var/run/docker.sock:ro` mount). 10/10 서비스 통합 가시성.

### 공유 인프라 경계
v2 의 qdrant/vllm-llm/vllm-embed 는 v3 compose 에 미포함 — v3 가 `127.0.0.1:6333/8001/8002` 로 접근. `host.docker.internal` 대신 host network 기반. 충돌 방지용 v2 redis/grafana/loki/promtail/cloudflared 만 stop 후 v3 의 것으로 교체. mariadb 는 백업 후 stop.

## 완료 안 된 영역 (Phase 2.11 후보)

- **Macro 페이지**: control-ui 에 아직 없음. dashboard `/api/macro/*` 는 존재, UI 미구현
- **Scout 페이지**: control-ui 에 아직 없음
- **LLM Stats / Logs 페이지**: control-ui Phase 2.10 backlog 로 유보
- **Overview 페이지**: control-ui Phase 2.11 backlog
- **daemon heartbeat**: 현재는 container state 만 확인. application-level heartbeat(redis key) 추가 시 "tick 돌고 있는가" 까지 가시화
- **cloudflared metrics probe**: smoke 스크립트의 `:2000/metrics` probe 완화 (현재 FAIL=2 중 1건)
- **real mode 전환 체크리스트**: `.env` KIS_APP_KEY/SECRET/ACCOUNT_NO/BASE_URL real 로 교체 + KIS_IS_PAPER=false + v2 토큰 파일 핸드오버 (`cp /docker_data/kis_token/kis_token.json /docker_data/kis_token/v3_kis_token.json`) + kis-gateway 재기동. 실매매 차단 유지 필요 시 **v3 redis 에 `trading_flags:stop=1` 선행 set**.

## 팀 운영 경험 — Agent Teams

- 팀 6 Opus teammate 병렬 × 약 **3시간** 만에 Phase 2.10 12 task 전수 closing. 단독 세션이면 4주 예상이었던 작업량
- **bypassPermissions 모드** 필수 (`~/.claude/settings.json` 에 영구 박음). 안 하면 lead 승인 prompt 로 팀 전체 블로킹
- **teammate 간 자율 조율** 효과적: Track C ↔ F (API 계약), Track D ↔ E (lib+CLI 설계), Track A ↔ B (스키마 질의). lead 개입 없이 해결된 사례 다수
- **주의**: lead 가 실수로 `reset --hard` 치면 teammate commit 도 휩쓸림 → git worktree 분리 + 반드시 `git branch --show-current` 확인

## 핵심 커밋 (main branch)

- `eaf6302` Track A — migrations 006–010 + ETL
- `875954e` Track C — dashboard/monitor + control 라우터 + Dockerfiles
- `70c8b0d` Track D — briefing/backtest/council_logging + 69 tests
- `100299b` Track E — compose 통합 + smoke
- `fb119f1` Track B — 22 handler 포팅 완주 (+ bundled 3)
- `72b8f3c` lead — telegram_bot CMD fix + ETL bool/PK fix
- `92cc428` lead — dashboard daemon container state 통합

## 다음 세션 시작 시 체크

```bash
# 1. MS-01 v3 상태
ssh prime-jennie "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep prime-jennie-runtime"

# 2. 최신 main
cd /home/youngs75/projects/prime-jennie-runtime && git log --oneline -5

# 3. 글로벌 메모리 pull (여러 머신 간 동기화)
gh auth switch -u youngs7596 && cd ~/.claude/global-memory-youngs7596 && git pull --rebase
```
