# Real Mode Migration Checklist

KIS paper → real 전환 시 **반드시** 이 문서를 Top-down 으로 실행. 이전 세션(`../.ai/sessions/session-2026-04-18-0005.md`) 의 실전 경험을 체크리스트화.

> **긴급 롤백이 필요하면** 섹션 6 으로 바로 이동.

---

## 0. 전제 조건 (전환 전 반드시 확인)

- [ ] **현재 시각이 한국 장 중이 아님** (09:00~15:30 KST 밖). 장중 전환은 KIS OAuth 갱신/재인증 레이스 위험
- [ ] KIS OpenAPI real 계정 정보 준비: APP_KEY / APP_SECRET / ACCOUNT_NO
- [ ] **KOSPI 20d volatility 확인**: 현재 60% 이상이면 `MACRO_AUTO_OVERRIDE_DISABLED=0` 상태에선 Macro 가 closed 유지 → Scout 데이터 축적 멈춤. 고변동성 구간에서 데이터 수집 목적이면 bypass 플래그 `1` 유지 검토
- [ ] `~/projects/prime-jennie-runtime` 에서 `git status` clean (로컬 수정 없음). 필요 시 stash
- [ ] MS-01 디스크 여유 (`df -h /`) 최소 10GB — kis token/log 저장 공간
- [ ] 현재 v3 runtime 16 컨테이너 모두 healthy (`ssh prime-jennie 'docker ps --format "{{.Names}}\t{{.Status}}" | grep prime-jennie-runtime'`)

---

## 0.1 Blocking Pre-check Gates (stop 해제 직전 마지막 관문)

> 2026-04-19 리뷰(`../.ai/sessions/session-2026-04-19-0001.md` 참조) 산출. 아래 중 **하나라도 미충족이면 stop 해제 금지**.
> 정기 체크리스트의 나머지 항목은 위에서부터 점검해도, 이 게이트는 섹션 5 (stop 해제) 직전에 **재확인**한다.

```bash
# G1. MACRO_AUTO_OVERRIDE_DISABLED 가 slow-loop 컨테이너에서 제거/0 인지 검증.
#     bypass 가 켜진 채 stop 을 풀면 고변동성 장세(20d vol >= 35%)에 자동 closed 방어가 무력화된다.
ssh prime-jennie 'docker exec prime-jennie-runtime-slow-loop-1 env | grep MACRO_AUTO_OVERRIDE_DISABLED'
# 기대: 비어있거나 `MACRO_AUTO_OVERRIDE_DISABLED=0`

# G2. 최근 1시간 macro_runs 에서 auto_override 가 실제로 동작하는지 샘플 확인.
ssh prime-jennie '
  docker exec prime-jennie-runtime-postgres-1 psql -U pj_admin -d prime_jennie_v3 -c "
    SELECT gate, auto_override, trigger_reason, generated_at
    FROM macro_runs
    WHERE generated_at > NOW() - INTERVAL '"'"'1 hour'"'"'
    ORDER BY generated_at DESC LIMIT 5;"'
# 기대: trigger_reason='"'"'scheduled:scout_daily'"'"' 샘플에서 bypass 이벤트가 있으면
#       `pj.macro.auto_override_bypassed` 가 loki 에 나오지 않아야 함

# G3. screening-executor 샌드박스 argv 에 seccomp 가 붙는지 (F4 배포 이후).
#     SCREENING_SECCOMP_PROFILE env 가 호스트 절대경로로 지정되어야 적용된다.
ssh prime-jennie 'docker exec prime-jennie-runtime-slow-loop-1 env | grep SCREENING_SECCOMP_PROFILE'
ssh prime-jennie 'test -f $(docker exec prime-jennie-runtime-slow-loop-1 env | grep SCREENING_SECCOMP_PROFILE | cut -d= -f2) && echo ok'
# 기대: 절대경로 출력 + `ok`

# G4. allowlist M13~M17 우회 테스트 green 확인 (F5 배포 이후 regression 가드).
cd ~/projects/prime-jennie-runtime && pytest tests/screening_executor/test_allowlist.py::TestAttributeBypass -q
# 기대: 전부 pass
```

- [ ] **G1 통과** — `MACRO_AUTO_OVERRIDE_DISABLED` 가 컨테이너 env 에 없거나 `0`
- [ ] **G2 통과** — 최근 1시간 정기 macro_runs 의 bypass 흔적 없음
- [ ] **G3 통과** — seccomp 프로파일 파일이 호스트에 실재하고 env 로 주입됨 (F4 배포 완료 후)
- [ ] **G4 통과** — allowlist 우회 테스트 10건 전부 green (F5 배포 완료 후)

**미충족 시 조치**: stop 유지 + 해당 게이트의 F 작업을 먼저 닫는다. G1 만 통과시키면 이후 섹션 5 절차 진행 가능하지만 **G3/G4 는 defense-in-depth 권장**이며 비상시 G1 만으로도 운영 가능하다.

---

## 1. 선결 차단 — STOP 먼저 (실매매 방지)

실매매 전환 직전에 stop flag 를 세팅해서 **모든 진입을 차단**. 이 단계 생략 시 전환 직후 fast-loop 가 즉시 주문 전송.

```bash
# Redis stop 플래그 두 개 모두 세팅
REDIS_PW=$(ssh prime-jennie 'grep REDIS_PASSWORD ~/projects/prime-jennie-runtime/.env | cut -d= -f2')

ssh prime-jennie "docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a $REDIS_PW SET trading_flags:stop 1"
ssh prime-jennie "docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a $REDIS_PW SET control.state:stop 1"

# 검증
ssh prime-jennie "docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a $REDIS_PW GET control.state:stop"
# 기대: "1"
```

**검증 layer 5 종** (손절 방지 이중 차단):

| # | Layer | 코드 위치 | 확인 방법 |
|---|-------|----------|---------|
| 1 | Redis `trading_flags:stop=1` | `fast_loop/app.py:77-86` (BalanceAwareSizer.__call__) | 위 Redis GET |
| 2 | v3 `position_sheets` 0 rows (진입 대상 없음) | `fast_loop/sheet_fetcher()` | `SELECT COUNT(*) FROM position_sheets WHERE valid_until > NOW()` |
| 3 | Stream ACK-first | `redis_streams.py:157` | pending 큐에 안 남음, stop 해제 시 폭주 불가 |
| 4 | `PositionSheet.valid_until` validator | Pydantic 모델 | 장외 시트 자체 거부 |
| 5 | `StrategyEngine.duplicate_today` | `slow_loop/engine.py` | 같은 날 같은 ticker 중복 차단 |

---

## 2. 환경 변수 교체

### 2.1 백업
```bash
ssh prime-jennie 'cd ~/projects/prime-jennie-runtime && cp .env .env.bak.paper.$(date +%Y%m%d_%H%M)'
```

### 2.2 .env 5줄 교체
```bash
# SSH 후 vi 또는 sed 로:
ssh prime-jennie
cd ~/projects/prime-jennie-runtime
vi .env
```

교체 대상 5줄 (paper 기본값 → real):

| 변수 | Paper | Real |
|------|-------|------|
| `KIS_APP_KEY` | `<PAPER_KEY>` | `<REAL_KEY>` |
| `KIS_APP_SECRET` | `<PAPER_SECRET>` | `<REAL_SECRET>` |
| `KIS_ACCOUNT_NO` | `50156036` (예) | `68211289` (예) |
| `KIS_BASE_URL` | `https://openapivts.koreainvestment.com:29443` | `https://openapi.koreainvestment.com:9443` |
| `KIS_IS_PAPER` | `true` | `false` |

**주의**:
- 포트가 `29443` → `9443` 로 바뀌는 것 놓치면 OAuth 401
- `KIS_IS_PAPER=false` 명시 (공백/생략 X)
- 다른 변수(REDIS_PASSWORD, POSTGRES_PASSWORD 등) 는 절대 바꾸지 말 것

---

## 3. KIS Gateway 재인증 + 재기동

### 3.1 Paper 토큰 제거 (real OAuth 강제 발급)
```bash
ssh prime-jennie '
cd ~/projects/prime-jennie-runtime
# 백업
mkdir -p data/kis_token
if [ -f data/kis_token/v3_kis_token.json ]; then
  mv data/kis_token/v3_kis_token.json data/kis_token/v3_kis_token.json.paper.$(date +%Y%m%d_%H%M)
fi
'
```

### 3.2 재기동
```bash
ssh prime-jennie '
cd ~/projects/prime-jennie-runtime
COMPOSE_PROJECT_NAME=prime-jennie-runtime docker compose up -d --force-recreate kis-gateway
'
```

### 3.3 토큰 발급 확인 (최대 30초 대기)
```bash
ssh prime-jennie 'docker logs prime-jennie-runtime-kis-gateway-1 --since 30s 2>&1 | grep -iE "token|oauth|openapi" | tail -10'
# 기대: "Token refreshed" 또는 "[200] OpenAPI token acquired"
# URL 확인: openapi.koreainvestment.com:9443 (real 주소)
```

실패 시:
- `grep -i error`  로 에러 확인
- 401 → APP_KEY/SECRET 오타 또는 BASE_URL 불일치
- 403 → 계좌번호 타입 (모의/실전 혼동)
- connection refused → BASE_URL 포트 (29443 → 9443)

---

## 4. 실계좌 smoke test

```bash
# Balance (실 포지션 + 현금 잔고)
ssh prime-jennie 'curl -s http://localhost:8080/api/balance | python3 -m json.tool'
```

**기대 응답 예시** (실계좌):
```json
{
  "positions": [
    {"ticker": "005380", "stock_name": "현대차", "quantity": 266, "profit_pct": -1.06, ...},
    {"ticker": "010130", "stock_name": "고려아연", "quantity": 15, ...},
    {"ticker": "267250", "stock_name": "HD현대", "quantity": 123, ...}
  ],
  "cash_krw": 341498,
  "total_asset_krw": 200552998
}
```

**검증 포인트**:
- [ ] `positions[]` 가 실제 보유 종목과 일치
- [ ] `cash_krw` + `stock_eval_amount` ≈ `total_asset_krw`
- [ ] 빈 배열 or `{"error": "..."}` 아님

```bash
# Price smoke (실시간 호가)
ssh prime-jennie 'curl -s "http://localhost:8080/api/price?ticker=005930" | python3 -m json.tool'
# 기대: 실시간 삼성전자 호가 (paper와 다른 값 가능)
```

---

## 5. MACRO_AUTO_OVERRIDE_DISABLED 점검

`docker-compose.yml` 의 slow-loop environment 에 영구 주입되어 있음. 기본값 `1` (bypass 활성).

**상태 확인**:
```bash
ssh prime-jennie 'docker exec prime-jennie-runtime-slow-loop-1 env | grep MACRO_AUTO_OVERRIDE'
```

**3가지 운영 모드**:

### Mode A: 데이터 축적 우선 (stop 유지 + bypass 활성, 현재 기본)
- `control.state:stop=1`
- `MACRO_AUTO_OVERRIDE_DISABLED=1`
- 효과: 고변동성이어도 Macro 가 open 판단 → Scout 가 candidates 생성 → position_sheets 생성 → **하지만 stop 으로 실제 주문 0**
- 용도: 월요일부터 하루 7 run × ~20 candidates 자동 축적 (2-3 주 후 backtest 엔진 대비)

### Mode B: 실매매 재개 (stop 해제 + bypass 해제)
- `control.state:stop=0`
- `MACRO_AUTO_OVERRIDE_DISABLED=0` 또는 env 제거
- 효과: 고변동성 구간엔 Macro closed → 신규 진입 차단, 저변동성 구간엔 실제 주문
- **실매매 재개 전 반드시 이 모드**. 역순 (stop 먼저 풀고 bypass 나중) 은 고변동 구간에 매수 폭주 위험

### Mode C: 긴급 차단 (stop 활성 + bypass 무관)
- `control.state:stop=1`
- 효과: Mode A 와 동일 — 어떤 상황에도 주문 0
- 용도: 장애 시 또는 전환 중 일시 차단

**Mode B 전환 시**:
```bash
ssh prime-jennie '
cd ~/projects/prime-jennie-runtime
# Option 1: env 추가
echo "MACRO_AUTO_OVERRIDE_DISABLED=0" >> .env

# Option 2: docker-compose.yml slow-loop environment 에서 해당 줄 제거

# slow-loop 재기동 (env 반영)
COMPOSE_PROJECT_NAME=prime-jennie-runtime docker compose up -d --force-recreate slow-loop
'

# 검증
ssh prime-jennie 'docker exec prime-jennie-runtime-slow-loop-1 env | grep MACRO_AUTO_OVERRIDE'
# 기대: MACRO_AUTO_OVERRIDE_DISABLED=0 또는 빈 출력
```

**그 다음** (반드시 순서 엄수):
```bash
# stop 해제
ssh prime-jennie "docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a $REDIS_PW SET trading_flags:stop 0"
ssh prime-jennie "docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a $REDIS_PW DEL control.state:stop"

# 검증
ssh prime-jennie "docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a $REDIS_PW GET control.state:stop"
# 기대: (nil) 또는 "0"
```

---

## 6. 긴급 롤백 — Real → Paper 복귀

**상황**: 시스템 오작동, 오주문 발견, 또는 검증 실패 시 즉시 복귀.

### 6.1 Stop flag 즉시 활성 (1초 내)
```bash
ssh prime-jennie "docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a $REDIS_PW SET control.state:stop 1"
```
이 한 줄만으로 fast-loop 가 즉시 모든 신규 진입 차단. 기존 보유 종목의 exit rule 평가는 계속.

### 6.2 Paper 설정 복원
```bash
ssh prime-jennie '
cd ~/projects/prime-jennie-runtime

# .env 복원 (가장 최근 백업 파일 사용)
BACKUP=$(ls -1t .env.bak.paper.* | head -1)
cp "$BACKUP" .env

# 토큰 복원
if [ -f data/kis_token/v3_kis_token.json ]; then
  mv data/kis_token/v3_kis_token.json data/kis_token/v3_kis_token.json.real.$(date +%Y%m%d_%H%M)
fi
PAPER_TOKEN=$(ls -1t data/kis_token/v3_kis_token.json.paper.* 2>/dev/null | head -1)
if [ -n "$PAPER_TOKEN" ]; then
  cp "$PAPER_TOKEN" data/kis_token/v3_kis_token.json
fi
'
```

### 6.3 재기동
```bash
ssh prime-jennie '
cd ~/projects/prime-jennie-runtime
COMPOSE_PROJECT_NAME=prime-jennie-runtime docker compose up -d --force-recreate kis-gateway slow-loop
sleep 15
docker logs prime-jennie-runtime-kis-gateway-1 --since 30s 2>&1 | grep -iE "token|openapi"
'
```

### 6.4 Paper 응답 확인
```bash
ssh prime-jennie 'curl -s http://localhost:8080/api/balance | python3 -m json.tool'
# 기대: paper 계좌 포지션 (real 과 다른 값)
```

### 6.5 진행 중 real 주문 정리 (선택)
```bash
# 활성 position_sheets 목록 확인
ssh prime-jennie '
docker exec prime-jennie-runtime-postgres-1 psql -U pj_admin -d prime_jennie_v3 -c \
  "SELECT sheet_id, ticker, generated_at FROM position_sheets WHERE valid_until > NOW() ORDER BY generated_at DESC LIMIT 10;"
'

# 필요 시 개별 sheet invalid 처리 또는 valid_until 수동 NULL 처리
# SQL: UPDATE position_sheets SET valid_until = NULL WHERE sheet_id = '...';
```

---

## 7. 전환 직후 모니터링 (첫 1시간)

### 7.1 5분 간격으로 체크

- [ ] v3 runtime 16 서비스 모두 healthy
- [ ] `control.state:stop` 유지 (Mode A 또는 B 의도대로)
- [ ] KIS `/api/balance` 정상 응답 (계좌 + 포지션 + 현금)
- [ ] dashboard `/api/system/health` 에서 5 daemon heartbeat 30s 이내
- [ ] Loki 에 신규 로그 유입 (promtail dropped_entries_total 0)

### 7.2 관찰 대상 지표

**KIS Gateway**:
- `kis-gateway` 로그에서 `EGW00201` (rate limit) 출현 주기
- circuit breaker open (20회 연속 실패 시) 여부

**Macro Gate**:
- `macro_runs.gate` 최신 row — Mode A 면 override 된 open, Mode B 면 시장 상황 반영한 open/closed
- `metadata_json->shadow->gate` (DeepSeek 판단) 일관성

**Scout**:
- 매 tick 마다 `scout_runs` 1 row + `screening_candidates` ~20 rows 생성
- `rejection_reason` 분포 (macro_closed/validator_hallucination/engine_error 비율)

**Fast Loop** (Mode B 인 경우):
- `position_sheets` 생성 → `executions` 체결 연결 확인
- `executions.status` 분포 (filled/cancelled/failed)
- Slippage 분포 (`executions.slippage_bps`)

### 7.3 Grafana 대시보드 힌트

- Loki 쿼리: `{service="fast-loop"} |= "entry blocked"` (stop 동작 확인)
- Loki 쿼리: `{service="slow-loop"} |= "auto_override"` (bypass 해제 시 동작 확인)
- Loki 쿼리: `{service="kis-gateway"} |= "circuit"` (장애 감지)

---

## 8. 설정 실수하기 쉬운 부분 (Gotchas)

### 8.1 MACRO_AUTO_OVERRIDE_DISABLED 장시간 유지

`docker-compose.yml` 에 `${…:-1}` 로 기본값 주입. 실매매 재개 전 반드시 해제 확인. 안 하면 고변동 구간 매수 필터가 없어진 상태로 실주문 발행.

**점검 방법**:
```bash
ssh prime-jennie 'docker exec prime-jennie-runtime-slow-loop-1 env | grep MACRO_AUTO_OVERRIDE'
```

### 8.2 KIS_IS_PAPER vs KIS_BASE_URL 불일치

`KIS_IS_PAPER=true` 인데 BASE_URL 이 real → 401 발생 (또는 반대).

**정상 조합**:
- Paper: `IS_PAPER=true` + `BASE_URL=https://openapivts.koreainvestment.com:29443`
- Real: `IS_PAPER=false` + `BASE_URL=https://openapi.koreainvestment.com:9443`

### 8.3 Stop flag 해제 순서

**잘못된 순서**: bypass(MACRO_AUTO_OVERRIDE) 해제 전에 stop 먼저 해제 → 고변동 구간에서도 Macro open → Scout candidates 생성 → 실제 매수 체결.

**올바른 순서**: (1) slow-loop 재기동으로 MACRO_AUTO_OVERRIDE 제거 반영 → (2) Macro 가 real volatility 로 closed 판단하는지 observation → (3) 그 다음 stop 해제.

### 8.4 Token 파일 경로 혼동

Paper 와 real 토큰 파일명 동일 (`data/kis_token/v3_kis_token.json`). 백업 시 suffix 로 구분 필수:
- `v3_kis_token.json.paper.YYYYMMDD_HHMM`
- `v3_kis_token.json.real.YYYYMMDD_HHMM`

### 8.5 Stop flag 키 2개 혼동

실 차단은 `control.state:stop` (v3 fast-loop 가 읽는 키). `trading_flags:stop` 은 legacy v2 호환 키로 현재 fast-loop 은 미사용. 하지만 이전 real_mode 세션에서 둘 다 세팅하는 관행 유지 (double safety).

---

## 9. Appendix — 거래 제어 신호 흐름

```
User (Telegram) → telegram_bot/control.py
  ├─ /stop         → SET control.state:stop=1
  ├─ /resume       → DEL control.state:stop
  ├─ /pause reason → SET control.state:pause=reason
  └─ /dryrun       → SET control.state:dryrun=1

fast_loop/app.py (BalanceAwareSizer)
  ├─ SystemState.snapshot() → control.state:* 읽음
  └─ entry_allowed = NOT (stopped OR paused)
      └─ qty = entry_allowed ? size_calc() : 0

slow_loop/macro/post_processor.py (line 52)
  ├─ check_closed_conditions(snapshot) → triggers[] 수집
  ├─ MACRO_AUTO_OVERRIDE_DISABLED=="1" ? skip : apply_override()
  └─ macro_runs.gate = closed (if triggers + open)

Redis Keys (Shared State):
  ├─ control.state:stop  → "1" = all entry blocked (v3)
  ├─ control.state:pause → reason = entry blocked
  ├─ control.state:dryrun → "1" = simulation only
  └─ trading_flags:stop  → legacy (v2 호환, 현재 읽기만 함)
```

---

## 10. 다음 세션 시작 전 체크

첫 진입 세션에서 이 체크리스트를 반드시 훑어본 후 전환 실행.

```bash
# 1. 현재 v3 상태 확인
ssh prime-jennie 'docker ps --format "{{.Names}}\t{{.Status}}" | grep prime-jennie-runtime | wc -l'
# 기대: 16 (전부 Up)

# 2. 현재 거래 모드
ssh prime-jennie 'grep "KIS_IS_PAPER\|KIS_BASE_URL" ~/projects/prime-jennie-runtime/.env'

# 3. stop 상태
ssh prime-jennie "docker exec prime-jennie-runtime-redis-1 redis-cli --no-auth-warning -a \$REDIS_PW GET control.state:stop"

# 4. MACRO_AUTO_OVERRIDE 상태
ssh prime-jennie 'docker exec prime-jennie-runtime-slow-loop-1 env | grep MACRO_AUTO_OVERRIDE'

# 5. 최근 scheduled_job 실행 기록 (월-금 정상 tick 여부)
ssh prime-jennie 'docker exec prime-jennie-runtime-postgres-1 psql -U pj_admin -d prime_jennie_v3 -c "SELECT job_id, started_at AT TIME ZONE \"Asia/Seoul\" as kst, status FROM scheduled_job_runs ORDER BY started_at DESC LIMIT 5"'
```
