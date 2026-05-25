# Prime Jennie Control UI 재설계 — Claude Code 핸드오프 지시서

**작성**: 2026-05-25
**대상**: Claude Code 워크플로우 입력 자료
**연결**: prime-jennie-control-ui 리포 (React + Vite + TypeScript + Tailwind), 백엔드는 prime-jennie-runtime FastAPI
**기반**: 의뢰서 v2 (`2026-05-24-dashboard-redesign-brief-v2.md`) + Claude Design 시안 11 페이지 (HTML) + 시안 단계 의사결정 30+ 개

---

## 0. 핸드오프 입력 자료 3 묶음

이 지시서를 보기 전에 다음 자료가 같은 작업 폴더에 있어야 한다.

1. **시안 ZIP** — Claude Design 의 "Download as ZIP" 으로 받은 standalone HTML 묶음
   - `Design Tokens.html` (토큰 시트, 디자인 시스템 정의)
   - `Overview.html` / `Overview - Alert.html`
   - `Portfolio.html` (Positions / Asset History 두 탭)
   - `Trades.html`
   - `Macro.html` (좌우 분리 scroll + sticky day separator)
   - `Scout.html` (좌우 분리 scroll + sticky day separator)
   - `News.html`
   - `LLM Stats.html`
   - `Logs.html`
   - `System.html` / `System - Alert.html`
   - `Jobs.html`

2. **의뢰서 v2** — `2026-05-24-dashboard-redesign-brief-v2.md`
   - §4 디자인 시스템 토큰 정의
   - §5 화면별 사양
   - §6 데이터 가용성 분류 (즉시 노출 / 백엔드 보강 / 데이터 없음)
   - §10 코드베이스 위치

3. **이 핸드오프 지시서** — 시안 단계의 모든 의사결정 보존 + 백엔드 보강 명세 + 구현 순서

세 자료 모두 필요하다. 시안만으론 narrative 의도 안 살고, 의뢰서 v2 만으론 시안 단계에서 fix 된 항목 (의뢰서엔 없던 결정 30+ 개) 빠진다.

---

## 1. 입력 → 출력 흐름

```
시안 ZIP (HTML)           의뢰서 v2 (사양/배경)        핸드오프 지시서 (이 문서)
       ↓                          ↓                            ↓
                          [ Claude Code 통합 ]
                                  ↓
                  prime-jennie-control-ui (React/Tailwind)
                  + prime-jennie-runtime 백엔드 보강 PR
```

산출물:
- 시안과 시각적·narrative 일치하는 production React 구현
- 백엔드 API 응답 보강 (별도 PR 단위)
- 공용 컴포넌트 라이브러리 (`src/components/`)

---

## 2. 시안 단계 의사결정 사항

의뢰서 v2 에 명시되지 않은 결정 사항만 정리. 시안 라운드를 거치며 fix 된 항목들이고, 휘발성 채팅 메모리에 남아있던 결정 사항을 보존하기 위한 자료다.

### 2.1 디자인 시스템 토큰 결정

| 항목 | 결정 | 이유 |
| --- | --- | --- |
| Grey 토큰 분기 | `fg-2 #7A8EA0` 텍스트용 + `accent/grey #5A6878` 상태용 (분리) | 텍스트 회색과 상태 배지 회색이 같으면 카드 안에서 시각 충돌 |
| P&L 색 컨벤션 | 글로벌 (상승=녹 / 하락=빨) 유지. 한국 시장 관례 (상승=빨) 와 다름 | 운영 대시보드는 알람 일관성 우선. 빨강은 위험·손실 전용. 부호 (+/-) 명시로 색 충돌 시에도 식별 가능 |
| Alert 위계 | yellow → orange → red 3-stage 일관 적용 (throttle / heartbeat / impact / latency) | 운영자가 위급도 단계 단일 멘탈모델로 인지 |
| 사이드바 그룹핑 | RUNTIME (Overview/Portfolio/Trades/Macro/Scout) / INTEL (News/LLM Stats/Logs) / OPS (Jobs/System) | 평면 10 메뉴 → 의미별 묶음으로 인지 부담 감소 |
| Badge 카운트 | Trades 28, News 12, Jobs 1 — 사이드바 메뉴 옆 표시 | 즉시 새 정보 인지 |
| `● live` 점 룰 | dashboard / control-ui 자체 heartbeat 임계 초과 시에만 빨강. SYSTEM ALERT 와 독립 | "대시보드 자체가 죽었는가" 는 다른 모든 신호보다 위계 위. 도메인 alert 와 분리 |
| Numeric font stack | `D2Coding` 포함 — `"BlinkMacSystemFont", "SF Mono", "JetBrains Mono", "D2Coding"` | 한국어 환경 코딩 폰트 fallback |
| LLM Stats 서비스 색 | news cyan / macro **blue** / scout purple / briefing yellow (macro 는 red 가 아님) | macro 가 빨강이면 차트에서 위험 신호로 잘못 인지. blue 는 primary 의미로 적합 |

### 2.2 페이지별 결정 사항

#### Overview

- **Hero band 1 카드** — Macro Gate 단독, 전체 폭. 의뢰서 v2 의 "Macro + Control flag 2 카드 hero" 안에서 Macro 1 카드로 변경. 평시 Control flag 4 회색 배지가 우측 hero 카드를 비어 보이게 하는 문제 해소.
- **Size multiplier 폰트 크기** — 38px mono. BULL/NEUTRAL/BEAR 라벨 (36px) 과 동등 위계.
- **5 카드 그리드 균등 폭** — `grid-template-columns: repeat(5, 1fr)`.
- **Macro Timeline 시간축 chronological** — 좌=과거, 우=현재. 마지막 막대에 "now" 라벨. 24h sparkline 과 시간축 통일.
- **상단 헤더** — `Overview / RUNTIME` breadcrumb + `KOSPI · OPEN` pill + `KST 시계 · tick -Xs · refresh` 버튼.
- **NEXT REFRESH 카운트다운** — Hero band 내. 운영 도구로서 다음 갱신 시점 가시화.
- **Control Flags 카드 "all clear" 캡션** — 평시 `● all clear · 자동 매매 정상 가동` 한 줄. STOP on 시 `● STOP engaged · 자동 매매 중단 상태` 로 변경.
- **System Health dot indicator** — heartbeat 단계 색 점 11 개 가로 정렬. 라벨 11 개는 추상화 (System 페이지에선 정확한 11 개 실제 서비스명 노출).
- **Hover affordance** — 5 카드 hover 시 우상단 "→" arrow icon. 클릭 시 해당 페이지로 이동.

#### Overview Alert variant

- **SYSTEM ALERT pill** — 우상단 빨강 펄스 pill `● SYSTEM ALERT · N issues`.
- **KOSPI OPEN pill 녹색 유지** — 시장 상태와 시스템 상태 분리.
- **운영 가이드 캡션** — Hero band 하단 `● 신규 진입 차단 · 청산 권고` 빨강.
- **24h sparkline 그라데이션** — 좌측 BULL 녹 → 중간 NEUTRAL 노 → 우측 BEAR 빨. `BEAR ↓ 11:30` 전환 마커.
- **Macro Timeline 색 그라데이션** — 앞 4 BULL 녹 / 중간 3 NEUTRAL 노 / 마지막 3 BEAR 빨. chronological 좌→우.
- **STOP on 카드** — pill 배지뿐 아니라 카드 전체 빨강 그라데이션.

#### System

- **상단 summary strip 5 슬롯** — `N / 11 services healthy` + `● N heartbeats elevated` 노랑 부주의 + KPI 4 (restarts · avg hb · worst hb · alerts).
- **12 번째 셀 AGGREGATE 카드** — 11 서비스 + 12 번째에 24h 통계 카드 (점선 테두리 + 청록 SUMMARY 배지 + RESTARTS / DEPLOYS / HB BREACHES / ALERTS FIRED / CPU·P95 / MEM·P95).
- **heartbeat glow 강도** — 위급도 단계별 box-shadow. 회색 glow 없음 / 노랑 약함 / 주황 중간 / 빨강 8px glow.
- **HEALTHY 배지와 heartbeat 색의 독립** — job-worker heartbeat 150s 빨강이어도 HEALTHY 배지 녹색 유지 가능. 두 신호 다른 차원.
- **Heartbeat thresholds 인라인 legend** — 상단 우측 `≤30s 정상 · 30-60s 주의 · 60-120s 경고 · ≥120s 위험`.
- **Control Flag 4 카드** — 각 카드에 한국어 설명 한 줄 + `last change · 시각 · by op-1` audit 메타. on 시 카드 전체 배경이 위급도 색.
- **Reason input placeholder** — `audit 로그에 기록될 사유 (선택) — 예: 11:30 KOSPI 1.4% 급락 대응`.
- **Command Center 6 버튼** — Resume 녹 (1-step confirm) / Pause 노 (confirm) / Dryrun ON 노 (confirm) / Dryrun OFF 회 / Emergency Stop 빨 (2-step) / Liquidate Arm 빨 + 자물쇠 (2-step). 각 버튼 하단에 한국어 설명 한 줄.

#### System Alert variant

- **회복 시나리오 통합** — 직전 노랑/주황/빨강 heartbeat 였던 서비스가 alert 시안에선 재시작 후 회색 <30s + uptime <1h + "recovered" 라벨. RESTARTS KPI 와 일관성.
- **STOP 빨강 + PAUSE 노랑 나란히 배치** — 위급 vs 보호 단계 색 차이 검증 통과.
- **auto-triggered 메타** — Control Flags 우상단 `auto-triggered` 표기 + Command Center footer `last action · auto · "slow-loop hb expired → STOP + PAUSE"`.
- **Reason input prefill** — 직전 자동 발동 사유 표시 (예: `2026-05-24 14:30 slow-loop heartbeat 만료로 STOP 자동 발동`).

#### Logs

- **3 채널 ERROR 가시화** (검증 통과 필수):
  1. 좌측 패널 서비스 옆 ERROR 카운트 빨강 배지
  2. 우측 헤더 ERROR · 60M sparkline 의 빨강 spike (시간대 위치 시각화)
  3. 본문 로그 줄 빨강 배경 + 좌측 stripe + ERROR 빨강 배지
- **헤더 3 줄 분리** — 1줄 검색 input + Regex/Case 토글 / 2줄 레벨 토글 (INFO/WARN/ERROR + 카운트) / 3줄 시간 범위 토글 (5m/15m/30m/60m/180m) + sparkline.
- **log body header 메타** — `● tailing · lines 30 · window 15m · rate ~2.0/s` + 우측 `↓ export · ‖ Pause tail`.
- **본문 syntax coloring** — IP 회색 / METHOD 보라 / path 파랑 / 200 녹 / 숫자 노랑. ERROR 줄 좌측 빨강 border bar.
- **백엔드 의존성** — `/api/logs/stream` 에 `level` (multi) + `q` (substring/regex) 파라미터 보강 필요 (§3.2).

#### Portfolio

- **Summary strip 6 슬롯** — Holdings / 평가액 / UNREALIZED P&L / TODAY P&L / CASH / TURNOVER 7D. 의뢰서 v2 의 3 슬롯에서 확장.
- **차트 단위 만원** (Asset History). Summary strip 은 원 단위 + 보조 표기 `(2.12억)`. 두 표기 같이 사용.
- **Asset History 듀얼 axis 차트** — 좌축 평가액 청록 실선 + 영역 fill, 우축 P&L% 점선 (부호별 녹/빨, 단일 zero-crossing). 30d/60d/YTD 토글. 차트 하단 4 KPI (start / peak / max drawdown / current).
- **P&L 컬럼 mini bar** — 값 + 시각 bar 동시.
- **Eval 컬럼 포트폴리오 비중 %** — 단일 종목 집중도 가시화 (예: `KODEX 200 ... 93.0% of port.`).
- **Sector chip 회색 단일 톤** — 색 토큰 시스템과 충돌 방지. 섹터에 임의 색 부여 금지.

#### Trades

- **BUY/SELL split bar 시각화** — 단순 카운트가 아니라 비율 (28.6% / 71.4%).
- **Day-group separator 매크로 메타** — `BULL · size 0.75×` / `BEAR · size 0.00× · auto-liquidate` (빨강). 매크로 결정과 거래 행동을 같은 행에 묶음.
- **BEAR auto-liquidate event banner — 4 채널 narrative**:
  1. Day separator metadata 빨강 (`5 RUNS · CLOSED · AUTO-LIQUIDATE`)
  2. Event banner (빨강 그라데이션 + ⚠ + 9 fills 통계 + `macro run 05-18 09:00 →` cross-link)
  3. 9 fills 행 빨강 미세 tint
  4. 각 행 `AUTO` 인라인 태그
- **footer 메타** — `avg fill latency 218 ms` 운영 가시성. `realized P&L` 3 군데 (Summary / Event banner / Footer) 일관성.

#### Macro

- **좌우 분리 scroll** — 좌측 list 자체 scroll + 우측 detail 자체 scroll. `.main` 전체가 스크롤되면 안 된다. 운영자가 좌측 list 위치 유지하며 우측만 스크롤 가능해야 함.
- **좌측 list day separator sticky** — 스크롤 중 현재 날짜 상시 노출.
- **Day separator metadata** — `5 RUNS · CLOSED · AUTO-LIQUIDATE` 빨강 / `3 RUNS · RECOVERY` / `7 RUNS · ALL OPEN` day 단위 narrative.
- **5-19 회복 시나리오 색 그라데이션** — `×0.25 주황 → ×0.50 노랑 → ×0.75 청록 → 5-21 ×1.00 녹` 시간순.
- **5-17 14:30 NEUTRAL × 0.50 노랑 점** — BEAR 직전 신호 시각화 (의뢰서 외 보강).
- **Reasoning 블록 사양** — 다크 배경 mono **13px** (12px → 13px 변경, 한국어 가독성) 줄바꿈 보존 + syntax coloring (결론 녹강조 / step 번호 회색 / 숫자 노랑 / 음수 빨강 / 양수 녹).
- **Top Risks 2x2 grid** — 카테고리 mono + 우상단 severity 배지 (HIGH 빨강 / MEDIUM 노랑) + 카드 테두리 톤.
- **Diff bar inline diff** — 정상시 `=` 녹색, 변경시 `변경` 빨강 + 델타 (예: `-0.75`).

#### Scout

- **좌우 분리 scroll + sticky day separator** — Macro v2 패턴 그대로.
- **Conviction 시각 위계** — 12 candidates 가로 막대, 상위 3 녹 / 중위 청록 / 하위 2 노랑.
- **Factor Weights 한국어 sub-label** — `news 뉴스/이벤트`, `value 밸류에이션`, `quality 재무 건전성` 등.
- **Factor weights 합 100** (시안 수정 후): news 10 / value 20 / quality 20 / momentum 20 / technical 10 / supply_demand 10 / sector_momentum 10.
- **5-22 run 상태 다양성** — 정상 macro 상태에서도 `0 cand · NO PASS` 빨강 / `1 cand 0.48 노랑 warn` 같은 변동성 시각화.
- **5-18 BEAR 클러스터** — Day separator `MON 05-18 · 5 BEAR LOCKED · NO SCREENING` 빨강. 5 runs 모두 `0 cand · skip` 빨강.
- **Macro cross-link** — `macro_run_id` KV 에서 Macro 페이지 동일 run_id 로 jump (역방향 cross-link).
- **Context Snapshot JSON 기본 접힘** — preview 한 줄 + `expand →` 토글. 펼침 시 syntax highlighting (key 청록 / string 녹 / number 노랑 / 음수 빨강).
- **universe 배지** — BEAR locked 행에서도 `U-200` 표시 유지 (universe 는 계산되지만 screening 단계 skip).

#### News

- **카테고리 색 의미 토큰 매핑** — 지정학 빨강 / 시장동향 청록 / 규제 노랑 / 파업 주황 / 수주·실적·주주환원 녹 / 제품·자금조달 파랑 / M&A·리포트 보라 / 인사·기타 회색 / ETF·펀드 청록.
- **카테고리 정렬: 건수 내림차순** (영석 결정).
- **`● live · ingestion 12.3/s · last 4s ago` 펄스 표기** — 실시간 ingestion rate 가시화.
- **카테고리 막대의 H:N 카운트** — 각 카테고리의 high impact 건수 별도.
- **TYPE chip 카운트 표시** — `실적 87`, `규제 347` 식.
- **Top Tickers 1 위 청록 highlight**.
- **태그 `#` 접두 시각화**.

#### LLM Stats

- **macro 색 red → blue** (`#3A8FFF`). primary 의미.
- **Today 카드 next 호출 예정 시각** — 우상단 `next: macro_quick 09:30 KST · global_news_crawl 10:00 KST · briefing 17:00 KST` 한 줄. 평일 장 시작 전 0 calls 가 정상 상태임을 컨텍스트로 즉시 인지.
- **vLLM news_analysis 사용량 + 비용 추적** — actual cost (self-hosted GPU runtime 환산, 회색) + shadow cost (if migrated to DeepSeek 환산, 점선 outline) 두 차원 동시 노출.
- **Cost by Service 차트 토글** — `view: actual / shadow / both (default)`.
- **briefing NEW 배지 + placeholder** — 첫 실행 전 (5-25 17:00 이전) `—` placeholder.
- **0 calls today 회색 stub** — 평일 장 외 시간 / 주말 정상. 노랑 alert 처리 금지.

#### Jobs

- **Cron human-readable 변환 primary** — `0 3 * * *` → "매일 03:00" 등. raw cron 은 mono 점선 underline secondary, hover tooltip.
  - `0 3 * * *` → "매일 03:00"
  - `0 6 * * 1,4` → "주 2회 (월 / 목) 06:00"
  - `*/5 9-15 * * 1-5` → "평일 09:00 – 15:00 · 5 분마다"
  - `45 18 * * 1-5` → "평일 18:45"
  - `0 4 15 1,4,7,10 *` → "분기별 15 일 04:00"
  - 6+ 패턴 발생 시 `cronstrue` 같은 라이브러리 권장
- **prefix 별 그룹핑** — collect_* / macro_* / daily_* / sync_* / refresh_* / global_news_*. 그룹 헤더에 chevron + count + 한국어 라벨 + meta.
- **Upcoming runs 패널** — summary strip 우측에 다음 5 runs 미리보기 (relative time + 정확 시각).
- **sync_positions OFF 시각화** — 행 전체 회색 톤 + `disabled · 20d` 노랑 pill + next run `—` + `last 5-05 20:45`.
- **Status latency 단계** — 정상 회색 / 느림 18s+ 노랑 / 매우 느림 25s+ 주황 / 미실행 회색 dash.
- **Next Run 단계** — `in 15m` 노랑 (임박) / `in 28m` 청록 (곧) / `in 6h+` 회색 (먼 미래).

---

## 3. 백엔드 보강 항목

프론트 구현 전에 백엔드 보강이 선행되어야 한다. 안 그러면 mock 데이터로 프론트 만들고 나중에 다시 통합해야 함. 각 항목은 별도 PR 단위.

### 3.1 portfolio sector_group 응답 추가

- 경로: `prime-jennie-runtime/dashboard/routers/portfolio.py`
- 변경: Positions API 응답 모델에 `sector_group: Optional[str]` 필드 추가. `stock_masters.sector_group` 컬럼에서 읽음.
- 예외: ETF (KODEX 200 등) 는 `sector_group = "ETF"` 표시.

### 3.2 logs filter 파라미터 추가

- 경로: `prime-jennie-runtime/dashboard/routers/logs.py`
- 변경: `/api/logs/stream` 에 다음 파라미터 추가
  - `level: list[str]` — `["INFO", "WARN", "ERROR"]` multi-select
  - `q: str` — message 본문 substring 또는 regex (`re` 모듈)
- 백엔드 구현: Loki 쿼리 동적 생성 (LogQL `{service="X"} |~ "regex" | level=~"INFO|ERROR"`)

### 3.3 system port 응답 + heartbeat 임계 정의

- 경로: `prime-jennie-runtime/dashboard/routers/system.py`
- 변경 1: `/api/system/health` 응답에 각 서비스의 `port: int` 필드 추가
- 변경 2: 백엔드 `heartbeat_status_level` 계산 로직 추가
  - `<30s` → `"healthy"` (회색)
  - `30-60s` → `"attention"` (노랑)
  - `60-120s` → `"warn"` (주황)
  - `>120s` → `"danger"` (빨강)
- 프론트는 이 값으로 색 결정 (직접 임계 비교 금지). 임계 변경 시 백엔드 한 군데에서 조정 가능.

### 3.4 vLLM shadow cost 계산 로직

- 경로: `prime-jennie-runtime/dashboard/services/llm_stats.py` + `config/llm_pricing.yaml`
- 변경 1: `/api/llm/stats` 응답에 vLLM 호출 누적 추적 (`calls / tok_in / tok_out`).
- 변경 2: shadow cost 계산 — provider 별 가격 테이블 기반

```python
# config/llm_pricing.yaml
deepseek:
  chat:
    in_per_million_usd: 0.27
    out_per_million_usd: 1.10
openai:
  gpt-4o-mini:
    in_per_million_usd: 0.15
    out_per_million_usd: 0.60

# services/llm_stats.py
def calc_shadow_cost(tok_in, tok_out, provider="deepseek", model="chat"):
    price = pricing[provider][model]
    return (tok_in * price.in_per_million_usd
          + tok_out * price.out_per_million_usd) / 1_000_000
```

- actual cost (self-hosted GPU runtime) 는 별도 환산. RTX 3090 24/7 가동 전력 + 감가 → 일별 비용 산출.

### 3.5 audit log + auto-triggered 메타

- 경로: `prime-jennie-runtime/dashboard/routers/system.py`
- 변경: `/api/system/control` 응답에 `last_change` 메타 추가
  - `timestamp: datetime`
  - `triggered_by: str` (`"op-1"` 또는 `"auto"`)
  - `reason: str`
- 자동 발동 이력 (예: heartbeat 만료로 STOP) 도 동일 형식으로 audit log 에 기록.

### 3.6 회복 판정 임계 (recovered 라벨)

- 경로: `prime-jennie-runtime/dashboard/services/system_health.py`
- 변경: 서비스 카드의 `recovered` 라벨 판정 로직
  - 조건: `uptime < 1h` AND `last_24h_heartbeat_breach > 0` AND `current_heartbeat < 30s`
  - 만족 시 `status_label = "recovered"`

### 3.7 Trades P&L 데이터 정합성 점검 (데이터 조사)

- KB금융 SELL 72 주 65,500 의 P&L `-` 표시 — lot 매칭 로직 점검. 평단가 152,600 과 SELL 가격 65,500 차이가 비정상.
- 가능성 셋: (A) 액면분할 / (B) 다른 lot 의 매도 / (C) 데이터 입력 오류. 확인 후 정정.

---

## 4. 공용 컴포넌트 정의

`prime-jennie-control-ui/src/components/` 에 다음 컴포넌트 분해. Props 및 variant 정의.

### 4.1 기존 (의뢰서 v2 §10)

- `Card` — 기본 카드 컨테이너. variant: `default | elevated | hover`. background `--bg-1` / `--bg-2`.
- `StatusBadge` — 상태 배지. variant: `healthy | unhealthy | warning | attention | danger | info | new`.
- `LoadingSpinner`
- `Layout` — 사이드바 + 본문 분리
- `ConfirmDialog` — 명령 확인 dialog. variant: `single_step | two_step`.

### 4.2 신규 컴포넌트

- `HeartbeatDot` — 색 점 + 경과 시간. Props: `seconds: number`. 자동으로 4 단계 색 + glow 강도 계산.
- `SparkLine` — 시계열 미니 차트. Props: `data: number[]`, `color: string`, `unit?: string`.
- `MacroGateBar` — Macro 상태 막대 (chronological 10 개). Props: `runs: MacroRun[]`. hover tooltip + click jump.
- `CommandButton` — 6 운영 명령 버튼. variant: `resume | pause | dryrun_on | dryrun_off | emergency_stop | liquidate_arm`. 자동으로 색 + confirm 단계 결정.
- `CronCell` — cron raw → human-readable 변환 셀. Props: `cron: string`. tooltip 으로 raw 표시.
- `AggregateCard` — System 12 번째 셀 같은 통계 카드. variant: `summary | aggregate`.
- `EventBanner` — Trades 의 BEAR auto-liquidate banner. Props: `severity: 'critical' | 'warn'`, `title`, `meta`, `cross_link`.
- `LogRow` — Logs 본문 줄. Props: `time`, `level`, `message`. syntax coloring 자동 (IP / METHOD / path / 숫자).
- `FilterChip` — 필터 토글 칩. variant: `single_select | multi_select`. Props: `label`, `count?`, `active`.
- `RunListRow` — Macro/Scout 좌측 list 의 row. Props: `time`, `state_color`, `size_multiplier?`, `meta`. active 클래스 swap.
- `DiffBadge` — Macro diff bar 의 inline diff. Props: `before`, `after`, `delta?`. 자동으로 `=` 녹 / `변경` 빨강 + 델타.
- `TopRiskCard` — Macro Top Risks 카드. Props: `category`, `severity: 'HIGH' | 'MEDIUM'`, `description`.
- `ControlFlagCard` — STOP / PAUSE / DRYRUN / LIQUIDATE 카드. state: `off | on_protective | on_critical`.

### 4.3 Tailwind config 통합

`tailwind.config.js` 의 `theme.extend.colors` 에 시안 토큰 시트의 9 묶음 그대로 옮김:

```js
colors: {
  bg: { 0: '#0A0E18', 1: '#0F1724', 2: '#162032' },
  fg: { 1: '#E0E8F0', 2: '#7A8EA0', 3: '#3A5070' },
  accent: {
    blue:   '#3A8FFF',
    cyan:   '#00C8FF',
    green:  '#3FB950',
    red:    '#F85149',
    yellow: '#D29922',
    purple: '#BC8CFF',
    orange: '#F0883E',
    grey:   '#5A6878',
  },
  // 의미 색 (semantic aliases)
  macro:    { bull: '#3FB950', neutral: '#D29922', bear: '#F85149' },
  throttle: { '1.0': '#3FB950', '0.75': '#00C8FF', '0.5': '#D29922',
              '0.25': '#F0883E', '0.0': '#F85149' },
  heartbeat:{ healthy: '#7A8EA0', attention: '#D29922',
              warn: '#F0883E', danger: '#F85149' },
  pl:       { profit: '#3FB950', flat: '#7A8EA0', loss: '#F85149' },
  flag:     { off: '#5A6878', protective: '#D29922', critical: '#F85149' },
  service:  { news: '#00C8FF', macro: '#3A8FFF', scout: '#BC8CFF', briefing: '#D29922' },
}
```

폰트 stack:

```js
fontFamily: {
  sans: ['BlinkMacSystemFont', '"Segoe UI"', '"Noto Sans KR"', 'sans-serif'],
  mono: ['ui-monospace', '"SF Mono"', '"JetBrains Mono"', '"D2Coding"', 'monospace'],
}
```

---

## 5. 데이터 바인딩 + 라우팅

### 5.1 API 호출

- `src/api/` 에 endpoint 별 hook (`useMacroRuns()`, `usePortfolioPositions()`, `useSystemHealth()` 등).
- React Query 권장. cache + 자동 refresh + stale-while-revalidate.
- 실시간 streaming (Logs tail, Overview 카운트다운) 은 SSE 또는 WebSocket. 백엔드 prime-jennie-runtime 의 기존 streaming 패턴 확인.

### 5.2 Cross-link 라우팅

다음 cross-link 들이 실제 동작해야 한다. 단순 `<Link>` 가 아니라 URL 파라미터로 detail panel state 복원:

- Trades `5-18 09:00` event banner → `/macro?run_id=mr_20260518_0900_xxx`. Macro 페이지가 해당 run_id 로 좌측 list 자동 선택 + 우측 detail swap.
- Macro detail 의 KV → Scout 페이지 (역방향, scout 가 어느 macro 결정 기반인지).
- Scout detail 의 `macro_run_id` KV → Macro 페이지 동일 run.
- Logs ERROR 줄의 host info → System 페이지 해당 서비스 카드 (선택적).

### 5.3 State management

- 페이지 간 state 는 URL 파라미터로. localStorage 캐싱은 최소.
- 사이드바 group expand/collapse 는 localStorage.
- Logs tail pause/resume 은 컴포넌트 local state.

---

## 6. 구현 순서

작은 단위 PR 권장. 각 PR 별로 review 가능한 분량.

### Phase 1 — 기반 (1-2 PR)

1. **디자인 시스템 토큰 → Tailwind config 통합** (§4.3)
2. **공용 컴포넌트 분해** (§4.1 + §4.2)

### Phase 2 — 백엔드 보강 (각 PR 단위, §3)

3. portfolio sector_group 응답 (§3.1)
4. logs filter 파라미터 (§3.2)
5. system port + heartbeat 임계 (§3.3)
6. vLLM shadow cost 계산 (§3.4)
7. audit log + auto-triggered (§3.5)
8. recovered 판정 (§3.6)

### Phase 3 — 페이지별 구현 (작은 페이지 → 큰 페이지)

작은 페이지로 컴포넌트 라이브러리와 데이터 바인딩 패턴 검증 후, 큰 페이지에서 narrative 통합. 반대 순서로 가면 큰 페이지 만들다가 컴포넌트 설계 잘못된 거 발견하고 리팩토링 부담.

9. **News** — 카테고리 색 매핑만 적용. 가장 작은 변화
10. **LLM Stats** — 차트 + shadow cost 토글
11. **Jobs** — cron human-readable + 그룹핑
12. **Portfolio** — Positions 표 + Asset History 듀얼 차트
13. **Trades** — BEAR event banner narrative
14. **System** — heartbeat 4 단계 + Command Center 6 버튼
15. **Logs** — 3 채널 ERROR 가시화 + 헤더 3 줄
16. **Macro** — 좌우 분리 scroll + sticky + diff bar
17. **Scout** — Conviction 차트 + Factor Weights
18. **Overview** — Hero band + 5 카드 + Macro Timeline + Recent Trades

### Phase 4 — 통합 검증

19. Cross-link narrative 연결 검증
20. Alert state 시각 검증 (Overview alert, System alert)

---

## 7. 검증 기준 (narrative 일치 체크리스트)

구현 완료 후 다음 체크리스트로 시안과 일치 여부 점검.

### 7.1 색 토큰 일관성

- [ ] 빨강 = BEAR / STOP / 손실 / heartbeat>120s / LIQUIDATE 5 곳에 같은 hex (`#F85149`)
- [ ] 녹 = BULL / throttle 1.0 / P&L+ 3 곳에 같은 hex (`#3FB950`)
- [ ] 노랑 = NEUTRAL / DRYRUN / PAUSE / heartbeat 30-60s / throttle 0.5 5 곳 일관
- [ ] macro 서비스 색 = blue (`#3A8FFF`) — LLM Stats / Overview LLM Today / 모든 차트
- [ ] Service color 매핑 (news cyan / macro blue / scout purple / briefing yellow) 모든 페이지 일관

### 7.2 Narrative cross-link

- [ ] Trades 5-18 banner 클릭 → Macro 5-18 09:00 BEAR run detail 표시
- [ ] Macro detail 의 macro_run_id 클릭 → Scout 동일 run 의 context_snapshot
- [ ] System alert 발동 → Overview SYSTEM ALERT pill 즉시 노출

### 7.3 운영 narrative 통합

- [ ] Trades 5-18 사건이 4 채널로 가시화 (separator + banner + 빨강 tint + AUTO 태그)
- [ ] System alert variant 의 회복 시나리오 (price-scheduler / job-worker recovered) 가 RESTARTS KPI 와 일관
- [ ] Macro 좌측 list 의 5-17 NEUTRAL × 0.50 노랑 점 (BEAR 직전 신호) 표시
- [ ] Scout 좌측 list 5-18 클러스터 `NO SCREENING` 빨강

### 7.4 UX 패턴

- [ ] Macro / Scout 좌우 분리 scroll (`.detail-body` 자체 scroll, `.main` 전체 스크롤 X)
- [ ] 좌측 list day separator sticky
- [ ] 모든 confirm dialog — Resume 1 단계, Emergency Stop / Liquidate Arm 2 단계
- [ ] Logs tail pause/resume 동작
- [ ] cron raw → human-readable 5+ 패턴 변환 정확

### 7.5 데이터 정확성

- [ ] Portfolio Sector 컬럼 채워짐 (모든 종목, ETF 는 "ETF")
- [ ] System Port 표시 (11 개 모든 서비스)
- [ ] Heartbeat 4 단계 색 백엔드 임계와 일치 (<30s / 30-60s / 60-120s / >120s)
- [ ] vLLM shadow cost 계산 정확 (token × price table)

---

## 8. 참조

- 시안 ZIP: `/home/youngs75/projects/prime-jennie-v3-dashboard-renewal/` (11 페이지 HTML)
- 의뢰서 v2: `prime-jennie-runtime/.ai/designs/2026-05-24-dashboard-redesign-brief.md`
- 코드베이스 위치 (의뢰서 v2 §10 과 동일):
  - 프론트: `prime-jennie-control-ui/` (React + Vite + TS + Tailwind)
  - 백엔드: `prime-jennie-runtime/dashboard/routers/` (FastAPI)
- 시안 단계 의사결정 채팅 로그: 본 핸드오프 지시서가 휘발성 채팅의 보존본

---

## 9. Claude Code 시작 명령 예시

영석이 Claude Code 에 던질 시작 명령:

```
prime-jennie-control-ui 재설계 핸드오프. 다음 자료 모두 참고:

1. 시안 ZIP: <시안 압축 풀린 경로>
2. 의뢰서 v2: ./2026-05-24-dashboard-redesign-brief-v2.md
3. 핸드오프 지시서: ./2026-05-25-dashboard-handoff-brief.md (이 문서)

§6 Phase 1.1 (디자인 시스템 토큰 → Tailwind config 통합) 부터 시작.
구체 작업:
- 시안 ZIP 의 Design Tokens.html 에서 CSS variables 추출
- 핸드오프 지시서 §4.3 의 Tailwind config 구조로 변환
- prime-jennie-control-ui/tailwind.config.js 에 통합
- 기존 색 사용 부분을 새 토큰으로 마이그레이션 (전수 검색 후 PR)

각 Phase 완료 시 PR 단위로 commit, 다음 Phase 진행 전 review 대기.
```

이거 던지면 Phase 1.1 완료 → Phase 1.2 → Phase 2 → ... 순서로 자동 진행.

각 Phase 가 PR 한 단위. PR 별로 review 시 §7 검증 체크리스트 해당 항목 점검.
