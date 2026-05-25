# Control UI 재설계 의뢰서 — Claude Design

**작성**: 2026-05-24
**대상**: Claude Design 캔버스에 던질 입력 자료
**연결**: prime-jennie-control-ui 리포 (React + Vite + TypeScript + Tailwind), 백엔드는 prime-jennie-runtime FastAPI

이 글은 두 부분으로 되어 있다. 앞 절반은 사람이 다시 읽을 자료 (현재 상태, 우선순위, 데이터 가용성) 이고, 마지막 §8 은 Claude Design 캔버스에 그대로 붙일 프롬프트 묶음이다.

## 1. 배경

지금 운영 중인 v3 control UI 는 10 개 페이지로 사이드바 네비게이션과 다크톤은 갖춰져 있지만, 운영 대시보드로 보면 세 가지 문제가 있다.

첫째, 정보 위계가 약하다. Overview 상단 5 개 카드가 모두 같은 비중으로 현재 스냅샷만 보여 주고, 추이가 없다. Macro Gate 상태나 자산 P&L 처럼 운영자가 먼저 봐야 하는 정보가 도드라지지 않는다.

둘째, 색 토큰이 일관되지 않다. heartbeat 가 25 초여도 빨강, 3 초여도 빨강으로 칠해진다. 정상 범위인데 항상 위험해 보여 운영자가 색을 신뢰하지 않게 된다. Macro 게이트 색, throttle 단계 색, P&L 색이 한 팔레트에서 충돌 안 나게 정의돼 있지도 않다.

셋째, 화면마다 빈 공간이 너무 많다. Logs 페이지 우측 80% 가 비어 있고 검색·필터·하이라이트가 없어 ERROR 검출이 어렵다. LLM Stats Today 가 "No calls today" 한 줄로 비어 있는 자리, Trades 의 Tier/Reason 빈 컬럼도 같은 맥락이다.

5-23 민지 (이전 컨설팅 LLM) 분석이 이 셋을 짚었다. 이번 글은 그 분석을 받아 (1) 현 코드 ground truth 와 (2) 백엔드 데이터 가용성을 확인하고, Claude Design 캔버스에 넘길 수 있는 형태로 정리한 것이다.

## 2. 작업 범위

전체 10 개 화면을 한 의뢰서에 묶는다. 디자인 시스템 자체가 화면 단위로 충돌하는 자리가 많아 화면 하나만 시안 뽑으면 나머지가 따라오지 않는다.

- 사이드바 항목 그대로 유지: Overview, Portfolio, Trades, Macro, Scout, News, LLM Stats, Logs, Jobs, System
- 라우팅과 URL 도 그대로 (`/overview`, `/portfolio`, ...)
- 백엔드 API 응답 형식은 §6 에 정리된 자리만 보강. 나머지는 현 상태 유지

이번 의뢰는 시안 산출이 목적이고, 시안이 나온 다음 Claude Code 핸드오프로 React/Tailwind 구현은 별도 작업으로 간다.

## 3. 디자인 톤

Datadog · Grafana 의 운영 대시보드 톤으로 간다. 이유 세 가지.

첫째, 정보 밀도가 trading dashboard 와 맞는다. Linear/Vercel 의 minimal 톤은 여백 위주라 운영자가 한 화면에서 잡아내야 하는 신호 수가 줄어든다. Bloomberg Terminal 은 진입장벽이 너무 높다.

둘째, 색으로 상태를 구분하는 패턴이 이미 정립돼 있다. healthy/warning/critical 3 단계, 시계열 차트의 sparkline, 표 안의 inline mini chart 같은 패턴을 그대로 응용할 수 있다.

셋째, 다크 배경 + 청록·노랑·빨강 액센트 조합이 trading 도메인의 BULL/NEUTRAL/BEAR, P&L +/-, throttle 단계와 자연스럽게 매칭된다.

레퍼런스: Datadog APM Service Map, Grafana 시계열 패널, Loki Logs 탐색 UI.

## 4. 디자인 시스템 토큰

### 4.1 기존 토큰 (유지)

현재 Tailwind 설정에 정의된 색은 다음과 같다.

- 배경: `#0a0e18` (가장 어두운 면), `#0f1724` (카드), `#162032` (강조 카드/호버)
- 텍스트: `#e0e8f0` (주), `#7a8ea0` (보조), `#3a5070` (희미)
- 액센트: 파랑 `#3a8fff`, 청록 `#00c8ff`, 녹색 `#3FB950`, 빨강 `#F85149`, 노랑 `#D29922`, 보라 `#BC8CFF`, 주황 `#F0883E`

이 팔레트는 그대로 유지한다. Datadog 톤과도 충돌 없다.

### 4.2 의미 색 토큰 (재정의)

지금 ad-hoc 으로 흩어진 의미 색을 다섯 묶음으로 정의한다.

**Macro 게이트 상태**
- BULL/open: 녹색 `#3FB950`
- NEUTRAL: 노랑 `#D29922`
- BEAR/closed: 빨강 `#F85149`

**Throttle 5 단계** (intraday_multiplier 0.0 ~ 1.0 의 5 분할)
- 1.0 (정상): 녹색 `#3FB950`
- 0.75: 청록 `#00c8ff`
- 0.5: 노랑 `#D29922`
- 0.25: 주황 `#F0883E`
- 0.0 (정지): 빨강 `#F85149`

**Heartbeat 단계** (마지막 heartbeat 경과 초)
- < 30s: 회색 `#7a8ea0` (정상, 굳이 색칠 안 함)
- 30 ~ 60s: 노랑 `#D29922` (주의)
- 60 ~ 120s: 주황 `#F0883E` (경고)
- > 120s: 빨강 `#F85149` (위험)

지금 코드는 마지막 heartbeat 시각만 보고 무조건 빨강을 칠한다. 임계값 자체가 없다. 이 자리부터 손봐야 한다.

**P&L 부호**
- +: 녹색 `#3FB950`
- 0: 회색 `#7a8ea0`
- -: 빨강 `#F85149`

**Control flag** (STOP / PAUSE / DRYRUN / LIQUIDATE ARMED)
- off: 회색 `#7a8ea0`
- on (보호적 — DRYRUN, PAUSE): 노랑 `#D29922`
- on (위급 — STOP, LIQUIDATE ARMED): 빨강 `#F85149`

이 다섯 묶음은 시안 그리기 전에 디자인 시스템 단계에 박혀 있어야 한다. 시안 다 뽑고 색 갈아 끼우면 카드마다 의미가 어긋난다.

### 4.3 타이포그래피

현재 시스템 폰트 (BlinkMacSystemFont, Segoe UI, Noto Sans KR) 그대로 유지. 숫자는 표·통계 카드에서 tabular-nums 적용해 자릿수 정렬. 한국어와 영어 라벨이 섞이는 자리 (예: 사이드바 "Portfolio" + 본문 "기간/종목") 가 있는데, 라벨 자체는 영어로 통일하고 본문 한국어를 허용하는 방향이 운영 도구로는 자연스럽다.

### 4.4 spacing 과 그리드

12 컬럼 그리드, gap 16px 기준. 카드 내부 padding 16px. Datadog 대시보드의 카드 밀도와 비슷한 수준.

## 5. 화면별 사양

각 화면마다 (1) 핵심 정보, (2) 정보 위계, (3) 데이터 모델, (4) 빈 공간 채우는 방법 순서로 정리한다.

### 5.1 Overview

운영자가 가장 먼저 보는 화면. 한 화면에 모든 거시 신호를 압축.

핵심 정보: Macro 게이트 상태 (지금 거래 가능한가), Control flag (STOP/PAUSE/DRYRUN 켜져 있나), 자산 P&L 추이, Scout 마지막 실행 결과, 시스템 헬스 요약, 오늘의 LLM 호출량.

정보 위계: Macro 게이트 + Control flag 두 카드가 전체 폭을 차지하는 hero band 로. 그 아래 4 개 카드 (Portfolio P&L, Latest Scout, System Health, LLM Today) 가 동일 폭. 각 카드는 현재 값 + sparkline 한 줄 + 클릭 시 해당 페이지 이동.

데이터 모델:
- Macro Gate: `/api/macro/regime` (현재 상태) + `/api/macro/runs?limit=24` (24 시간 sparkline)
- Control flag: `/api/system/control` (STOP, PAUSE, DRYRUN, LIQUIDATE ARMED 네 boolean)
- Portfolio: `/api/portfolio/summary` (현재 평가액, P&L) + `/api/portfolio/history?days=30` (30 일 sparkline)
- Latest Scout: `/api/scout/latest` (마지막 실행 conviction 분포)
- System Health: `/api/system/health` (11/11 healthy 같은 요약)
- LLM Today: `/api/llm/stats` (오늘) + 7 일 sparkline 은 어제부터 6 일 전까지 `/api/llm/stats/{date}` for-loop

기존 Macro Timeline (last 10) 막대 + Recent Scout Runs 리스트 + Recent Trades 표는 화면 하단에 유지하되, Macro Timeline 막대는 클릭 시 해당 macro_run 의 reasoning + top_risks 가 우측 패널로 슬라이드 인.

### 5.2 Portfolio

핵심 정보: 보유 종목 리스트와 평가액, 자산 시계열.

정보 위계: 상단 요약 (Holdings 수, Eval, P&L) 그대로. Positions 표는 Stock / Sector / Qty / Avg Price / Cur Price / P&L / Eval 컬럼. Sector 컬럼은 §6 의 자리에 따라 sector_group 데이터가 채워진다. Asset History 탭은 P&L curve 와 평가액 curve 두 시계열 차트로.

데이터 모델:
- Positions: `/api/portfolio/positions` 응답에 `sector_group` 필드 추가 필요 (백엔드 DB 에 있는데 응답 모델에서 누락)
- Asset History: `/api/portfolio/history?days=30` — 이미 daily_asset_snapshots 백엔드 데이터 있음, 차트로 그리기만

빈 공간 채우는 방법: Asset History 탭을 currently 비활성처럼 보이는 자리에서 실제 시계열 차트로. 30 일 / 60 일 / YTD 토글.

### 5.3 Trades

핵심 정보: 최근 체결 내역, BUY/SELL 분포, realized P&L.

정보 위계: 상단 요약 줄 (Total / Buy / Sell / Realized P&L) 유지. 7d / 14d / 30d / 60d 토글 유지. 표 컬럼은 Time / Stock / Type / Qty / Price / Total / P&L%.

**Tier / Reason 컬럼은 제거한다**. §6 에서 확인했듯이 v3 schema 에 데이터 자체가 없다. v2-only 필드라 의도적으로 빠진 자리. 컬럼 자리를 비워 두느니 표 전체가 보이는 공간을 늘리는 게 운영에 낫다.

데이터 모델: `/api/trades/recent?days=N` — 현재 응답 형식 그대로.

### 5.4 Macro

핵심 정보: 최근 macro 판단들의 시계열, 하나를 선택하면 LLM reasoning + top_risks + 결정 근거.

정보 위계: 현재 좌측 list + 우측 detail 패턴 유지. 단 좌측 list 의 각 row 에 gate 상태 색 점 + size_multiplier 작게 표시. 우측 detail 은 Run Detail / Reasoning / Top Risks / Confidence 4 섹션으로 분리. Top Risks 카드 색 코딩 (GEOPOLITICAL HIGH 등) 유지 — 이미 잘 돼 있다.

데이터 모델: `/api/macro/runs?limit=50` + `/api/macro/insight/{run_id}`. 응답 그대로.

추가: 우측 상단에 "previous run 과 비교" 토글 — 직전 run 의 gate, size, top_risks 차이를 보여 주는 자리. 5-24 0004 세션에서 추가한 recent_macro_runs 가 prompt 에 들어가기 시작했으니 운영자도 그 비교를 볼 수 있어야 한다.

### 5.5 Scout

핵심 정보: 매일의 종목 선정 결과와 conviction 분포, 선정 근거 (factor weights).

정보 위계: 좌측 list 는 날짜별 (현재 처럼). 우측 detail 의 CONTEXT SNAPSHOT JSON raw 노출은 기본 접힘 상태로 (디버깅 토글). Conviction 분포는 막대 그래프 (현재는 텍스트만). Factor weights 는 가로 막대 차트.

데이터 모델: `/api/scout/latest`, `/api/scout/run/{id}`, `/api/scout/candidates/{id}`. 응답 그대로.

### 5.6 News

핵심 정보: 뉴스 필터 + 집계 + 종목별 노출 + 기사 목록. 이미 잘 설계된 페이지.

수정 최소화. 필터 (impact/sentiment/type) 의 토글 UI 만 더 명확하게, 집계 차트의 색을 §4.2 토큰에 맞춰 정리. 기사 목록 표는 그대로.

데이터 모델: 현재 응답 그대로.

### 5.7 LLM Stats

핵심 정보: 오늘 / 이번 달 LLM 호출량, 서비스별 모델 라우팅.

정보 위계: "Today" 카드 안에 7 일 sparkline 한 줄. 0 calls today 일 때도 "지난 7 일 평균 X calls" 같은 컨텍스트가 보이도록. "This Month" 표는 그대로. Features / Model Routing 표는 현재 잘 돼 있다.

데이터 모델: `/api/llm/stats` + 어제부터 6 일 전까지 `/api/llm/stats/{YYYY-MM-DD}` 6 번 호출. 백엔드 보강 불필요.

빈 공간 채우는 방법: Today 카드 우측 절반에 일별 서비스별 stacked bar (지난 7 일). 0 calls today 도 막대가 0 으로 보이면 "오늘 안 부른 게 정상" 인지 "이상" 인지 운영자가 판단 가능.

### 5.8 Logs

핵심 정보: 서비스별 로그 흐름, ERROR 검출, 검색.

정보 위계: 좌측 Services 리스트 유지하되 각 서비스 옆에 최근 5 분 ERROR 카운트 작게 표시. 우측 패널을 크게 재설계 — 상단에 검색 input + 레벨 필터 (INFO/WARN/ERROR) + 시간 범위 토글 (5m/15m/30m/60m/180m). 로그 본문은 level 색 코딩 + 시각 회색 + 메시지 본문 강조.

데이터 모델: `/api/logs/stream?service=X&start=Y&end=Z` 가 현재 — `level`, `q` (search) 파라미터를 백엔드에 **추가 필요**. Loki 쿼리에 `level="error"` 라벨이나 `|~ "regex"` 를 동적 결합하도록 보강. 시안에는 필터 UI 다 그리되, "백엔드 보강 PR 별도" 노트.

빈 공간 채우는 방법: 우측 패널 상단을 검색·필터 헤더로, 본문 영역을 화면 80% 까지. 우측 끝에 미니 ERROR 발생 시계열 sparkline.

### 5.9 Jobs

핵심 정보: 정기 작업 목록, 다음 실행 시각, 최근 상태.

정보 위계: 현재 표 그대로 유지하되 다음 두 가지 손본다. (1) cron 표현 `0 3 * * *` 를 "매일 03:00", `*/5 9-15 * * 1-5` 를 "평일 09–15 시 5 분마다" 로 변환. 원본 cron 은 hover tooltip 으로. (2) job 이름의 prefix (collect_, macro_, daily_) 별로 시각적 그룹핑.

데이터 모델: `/api/jobs/list` 응답 그대로. cron 변환은 프론트 헬퍼.

추가: 상단에 "disabled jobs" 카운트 작게 (`sync_positions off` 처럼 의도적으로 꺼 둔 job 가시화).

### 5.10 System

핵심 정보: 11 개 서비스 헬스, Control 명령.

정보 위계: 11/11 healthy 요약 줄 유지. 카드 그리드 그대로. **heartbeat 색은 §4.2 토큰으로 단계화**. Port 와 Uptime 자리는 §6 의 자리에서 데이터 채움 (`url` 에서 포트 파싱, `uptime_seconds` 그대로 표시).

Control 섹션 버튼이 지금 흐릿한 비활성 톤이다. **Resume 은 녹색 outline, Pause/Dryrun ON 은 노랑 outline, Emergency Stop/Liquidate Arm 은 빨강 outline 으로 위급도 색 코딩**. 클릭 가능함을 색으로 알 수 있게.

상단의 STOP / PAUSE / DRYRUN / LIQUIDATE ARMED 4 개 카드는 현재 단순 텍스트 (`off` 회색). on 일 때 카드 전체 배경이 §4.2 의 위급도 색으로 칠해지도록.

데이터 모델: `/api/system/health` 응답에 `port` 필드 추가 (URL 파싱). `uptime_seconds` 는 이미 있음. Control 은 `/api/system/control` 그대로.

## 6. 데이터 가용성 분류

빈 자리 일곱 군데를 세 분류로 묶었다.

**즉시 노출 가능** (백엔드에 데이터 있음, API 응답 모델만 보강)
- Portfolio Sector: `stock_masters.sector_group` 컬럼 있음. positions API 응답 모델에 필드 추가만.
- System Port: `url` 필드에 포함돼 있음. URL 파싱하거나 응답에 별도 `port` 필드 추가.
- System Uptime: `uptime_seconds` 이미 응답에 있음. 프론트에서 단계 색만 입히면 됨.
- Overview sparkline (Macro/Portfolio/Scout): 모두 백엔드 데이터 있음. 프론트 차트 렌더링만.
- LLM 7 일 트렌드: Redis 에 일별 stats 누적 중. 프론트 for-loop 6 번 호출.
- Macro Timeline tooltip: `MacroRun` 응답에 reasoning, top_risks, confidence 다 있음. UI 인터랙션만 추가.

**백엔드 API 보강 필요** (응답 모델 또는 쿼리 파라미터 추가)
- Logs 레벨 필터·검색: 현재 `/api/logs/stream` 이 `level`, `q` 파라미터를 안 받음. Loki 쿼리 동적 생성으로 보강. PR 1 개 분량.

**데이터 자체 없음** (DB schema 또는 파이프라인부터)
- Trades Tier / Reason: v3 schema 에 컬럼 자체 없음. v2-only 필드라 의도적으로 빠진 자리. 이번 재설계에서는 컬럼 자체를 제거.

## 7. 시안 우선순위

Claude Design 캔버스에 던질 때 한 번에 10 화면을 다 만들지 말고 다음 순서로 간다.

1. **디자인 시스템 토큰 페이지** 하나 — §4 의 색 토큰을 시각화한 토큰 시트. 이게 fix 되지 않으면 다른 시안이 따라오지 않는다.
2. **Overview** — hero band 의 Macro 게이트 + Control flag 가 핵심. 이 화면 디자인 시스템이 fix 되면 §4 가 맞는지 검증된다.
3. **System** — heartbeat 단계 색, Control 버튼 위급도 색이 §4.2 의 검증 대상.
4. **Logs** — 가장 비어 있는 페이지. 검색·필터 헤더 + level 색 코딩의 시각화.
5. **Portfolio + Trades** — 표 위주 화면. Asset History 차트.
6. **Macro + Scout** — 좌측 list + 우측 detail 패턴 정제.
7. **News + LLM Stats + Jobs** — 이미 상대적으로 잘 돼 있는 페이지. 토큰 일관성만.

이 순서대로 시안 받으면 4 번까지 끝났을 때 디자인 시스템이 거의 fix 된다. 5 ~ 7 번은 시스템 적용만 하면 되므로 시안 반복 횟수가 적다.

## 8. Claude Design 캔버스 프롬프트

아래 묶음을 Claude Design 시작 프롬프트에 그대로 붙여 넣는다. 첫 시안은 1 + 2 (토큰 시트 + Overview) 만 받고, 거기서 디자인 시스템이 fix 되면 그 다음 화면들로 넘어간다.

---

### 첫 시안 요청 (토큰 시트 + Overview)

```
나는 자동 주식 거래 시스템 (한국 KOSPI 시장) 의 운영 대시보드 UI 를 재설계하고 있다. 백엔드는 이미 운영 중이고 React + Vite + TypeScript + Tailwind 로 구현된 control UI 의 시안을 새로 받으려 한다.

톤: Datadog · Grafana 의 운영 대시보드. 다크 배경, 정보 밀도 높음, 색으로 상태를 빠르게 구분.

다음 두 가지 시안을 한 번에 만들어 달라.

**(1) 디자인 시스템 토큰 시트** 한 페이지

다섯 묶음의 의미 색을 시각화한 토큰 시트. 각 묶음별로 색 칩 + 라벨 + 사용 맥락 한 줄.

- 배경 3 단계: #0a0e18 (가장 어두움), #0f1724 (카드), #162032 (강조/호버)
- 텍스트 3 단계: #e0e8f0 (주), #7a8ea0 (보조), #3a5070 (희미)
- 액센트 7 색: 파랑 #3a8fff, 청록 #00c8ff, 녹색 #3FB950, 빨강 #F85149, 노랑 #D29922, 보라 #BC8CFF, 주황 #F0883E
- Macro 게이트 상태 3 단계: BULL/open 녹색, NEUTRAL 노랑, BEAR/closed 빨강
- Throttle 5 단계: 1.0 녹색, 0.75 청록, 0.5 노랑, 0.25 주황, 0.0 빨강
- Heartbeat 4 단계: <30s 회색, 30~60s 노랑, 60~120s 주황, >120s 빨강
- P&L 3 부호: + 녹색, 0 회색, - 빨강
- Control flag: off 회색, 보호적 on (DRYRUN/PAUSE) 노랑, 위급 on (STOP/LIQUIDATE) 빨강

폰트는 시스템 기본 (BlinkMacSystemFont, Segoe UI, Noto Sans KR). 숫자는 tabular-nums.

**(2) Overview 페이지** 한 화면

좌측 사이드바 (Prime Jennie / TRADING SYSTEM 로고 + 10 개 메뉴 + v1.0.0) 는 다른 화면과 동일.

본문 구성:

- 상단 hero band (전체 폭 2 카드): 좌측 큰 카드는 Macro 게이트 — 현재 상태 (BULL/NEUTRAL/BEAR 라벨 + 색), size multiplier 큰 숫자, 24 시간 게이트 추이 sparkline. 우측 큰 카드는 Control flag — STOP / PAUSE / DRYRUN / LIQUIDATE ARMED 4 개를 위급도 색으로. on 일 때 카드 배경 자체가 색.
- 중간 4 카드 그리드: Portfolio (현재 평가액 + 30 일 P&L curve sparkline), Latest Scout (마지막 실행 시각 + conviction 분포 미니 막대), System Health (11/11 healthy 또는 X/11 + heartbeat 단계 색 점), LLM Today (오늘 호출 수 + 7 일 sparkline). 각 카드 클릭 시 해당 페이지로 이동 가능함을 시각화.
- 하단 좌측: Macro Timeline (last 10) 막대 차트. 각 막대 색은 그 시점 gate 상태. 호버 시 reasoning 한 줄 tooltip.
- 하단 중앙: Recent Scout Runs 리스트 (날짜 + 종목 수 + 평균 conviction 미니 표시).
- 하단 우측: Recent Trades 표 (시간, 종목, BUY/SELL 배지, Qty, P&L). 7d 토글.

데이터는 실제 운영 중인 시스템의 한 시점 스냅샷 예시로 채워 달라:
- Macro 게이트: BULL, size 0.75, 24 시간 동안 BULL 8 / NEUTRAL 2 분포
- Control flag: STOP off, PAUSE off, DRYRUN off, LIQUIDATE ARMED off (전부 회색)
- Portfolio: 평가액 2.1 억, P&L +1202 만 (+6.0%), 30 일 우상향 curve
- Latest Scout: 18:30 실행, 후보 12 개, 평균 conviction 0.61
- System Health: 11/11 healthy, 모든 점 회색 (정상)
- LLM Today: 0 calls today, 지난 7 일 평균 50 calls 표기

한국어와 영어가 섞이는 라벨 (사이드바 영어, 본문 한국어) 은 자연스러운 운영 도구 톤. 어색하지 않게.
```

---

### 후속 시안 요청 (예: System 페이지)

```
직전 시안의 디자인 시스템을 유지하고 System 페이지를 만들어 달라.

본문 구성:

- 상단: "11/11 services healthy" 요약 줄. 한 서비스라도 unhealthy 면 빨강 카운트.
- 중간: 11 개 서비스 카드 3x4 그리드 (kis-gateway, dashboard, monitor, control-ui, telegram-bot, cloudflared, slow-loop, fast-loop, news-pipeline, price-scheduler, job-worker). 각 카드:
  - 좌측: 서비스 이름 + healthy/unhealthy 배지
  - 우측: Port, Uptime, 마지막 heartbeat (heartbeat 단계 색 점 + 경과 시간)
- 하단 상단: Control 4 카드 (STOP / PAUSE / DRYRUN / LIQUIDATE ARMED). on 일 때 카드 전체 위급도 색 배경.
- 하단 중간: Reason input (audit 로그에 기록).
- 하단 그리드: 6 개 명령 버튼 (Resume 녹색 outline, Pause 노랑, Dryrun ON 노랑, Dryrun OFF 회색, Emergency Stop 빨강, Liquidate Arm 빨강). 클릭 가능함이 색으로 인식 가능.

데이터 스냅샷:
- 11 개 다 healthy. heartbeat 는 fast-loop 3s, slow-loop 25s, news-pipeline 4s, price-scheduler 24s, job-worker 23s — 모두 <30s 라 색 점은 회색
- Port: kis-gateway 8080, dashboard 8000, monitor 8001, control-ui 80, telegram-bot 8002, cloudflared 8003, slow-loop 8090, fast-loop 8091, news-pipeline 8092, price-scheduler 8093, job-worker 8094
- Uptime: 슬로우/패스트/뉴스/스케줄러/워커 모두 3.5h, 나머지는 -
- Control flag 4 개 모두 off
```

---

### Logs 페이지 후속 시안

```
직전 시안의 디자인 시스템을 유지하고 Logs 페이지를 만들어 달라.

본문 구성:

- 좌측 패널 (250px 폭): Services 리스트 11 개. 각 항목 옆에 최근 5 분 ERROR 카운트 작게 (0 이면 안 보임).
- 우측 패널 (나머지 폭):
  - 상단 헤더: 검색 input (placeholder "로그 검색") + 레벨 필터 (INFO/WARN/ERROR 토글 칩) + 시간 범위 (5m/15m/30m/60m/180m 토글) + 우측 끝에 최근 60 분 ERROR 발생 mini sparkline.
  - 본문: 선택 서비스의 로그 흐름. 각 줄은 [시간 회색] [level 색 배지] [메시지]. level INFO 청록, WARN 노랑, ERROR 빨강.
  - 본문 영역이 화면 80% 까지 차지. 빈 공간 최소화.

데이터 스냅샷: kis-gateway 선택 상태, 최근 15 분 로그 30 줄, INFO 28 줄 + WARN 1 줄 + ERROR 1 줄 (ERROR 는 "Connection timeout to upstream"), 우측 sparkline 에 ERROR 한 막대.
```

---

이 셋이 fix 되면 나머지 7 화면은 "직전 시안 디자인 시스템 유지하고 X 페이지 만들어 달라" 한 줄로 받는다. 톤이 잡혀 있어 시안 반복 횟수가 적다.

## 9. 주의사항

세 가지를 의식하고 작업한다.

첫째, Claude Design 의 한계. research preview 라 시안 결과가 production-grade 가 아니다. 차트 영역의 숫자 정렬, 시계열 데이터의 시간축 처리, 한국어 텍스트의 줄바꿈 같은 자리는 시안에서 흐릿하게 나올 가능성이 있다. 시안은 정보 위계와 색 토큰 검증 목적이고, 실제 픽셀 단위 fix 는 Claude Code 핸드오프 단계에서 한다.

둘째, 데이터 밀도가 높은 거래 대시보드는 AI 가 약한 영역이다. Macro Timeline 의 막대 10 개가 시각적으로 너무 좁게 나오거나, Logs 페이지의 검색·필터·시간축 sparkline 이 한 헤더에 안 들어갈 수 있다. 시안 받고 한 번에 ok 라고 보지 말고 인라인 코멘트와 슬라이더로 한두 라운드 손본다.

셋째, 백엔드 보강이 의존성인 자리. Logs 의 level/검색 필터는 백엔드 `/api/logs/stream` 의 파라미터 보강이 선행돼야 한다. 시안에는 UI 다 그리되, 구현 단계에서 백엔드 PR 을 먼저 보내고 프론트는 그 다음에. 이 의존성을 시안 단계에서 잊지 말 것.

## 10. 참조

- 현재 control UI: `/home/youngs75/projects/prime-jennie-control-ui` (React + Vite + TS + Tailwind)
- 백엔드 API: `prime-jennie-runtime/prime_jennie_runtime/dashboard/routers/` (FastAPI)
- 현재 Tailwind 토큰: `prime-jennie-control-ui/tailwind.config.js`
- 공용 컴포넌트: `prime-jennie-control-ui/src/components/` (Card, StatusBadge, LoadingSpinner, Layout, ConfirmDialog)
- 스크린샷 10 장: `/home/youngs75/projects/prime-jennie-dashboard-screenshot/`
- 직전 민지 분석: 이 의뢰서 §1 배경에 인용
