# INVESTIGATION REPORT — 2026-04-19

> **발단**: `INVESTIGATION_TASKS_2026-04-19.md` (민지, 2026-04-18 저녁 리뷰).
> Phase 2.10~2.13 포팅 + Real mode 전환 과정에서 4개 방어선에 스펙-구현 괴리
> 가능성 제기. Track V (검증, read-only) + Track F (수정) 로 분리 조사.
>
> **범위**: 2026-04-19 세션 동안 코드 + DB + loki + smoke 실측.
>
> **결론**: 민지 지적 중 3건 타당 (F3/F4/F5 수행) · 2건은 전제 오류 (F1/F2 기각) ·
> 1건은 추가 발견 (publisher FK race — 별도 fix). Real mode 운영은 **배포 초기
> 상태** 로 확정, 정기 실행 데이터는 월요일(2026-04-20) 이후 축적 예상.

---

## 0. 실행 요약

| 민지 제안 | Track V 결과 | Track F 조치 |
|---|---|---|
| V1: kospi_20d_vol 58% = 계산 버그 or threshold 실수 | **둘 다 아님**. 코드 정확 · 데이터 정확 · KOSPI 자체가 위기 변동성 | F1/F2 **기각** |
| V2: adapter argv ≠ 스펙 §6.2 | 거의 일치. `--security-opt seccomp` 만 누락 | **F4 수행** (seccomp 프로파일 + env 기반 조건부 argv) |
| V3: seccomp.json 파일 부재 | 확정 (리포에 없음) | **F4 일부로 해결** (Moby v24.0.7 default 포함) |
| V4: Scout → sheet 필터 전무 | 절반만 맞음. validate_candidates + StrategyEngine 룰은 있음. backtest 필터는 없음 | F6 **월요일 이후 결정** (정기 운영 샘플 필요) |
| V5: allowlist 우회 가능 | 정확. 코드 주석도 "Phase 1 한계" 인정 | **F5 수행** (AST Attribute + dunder + pickle 패턴 차단 + M13~M17 테스트) |
| V6: hallucination rate 통계 | 14일 데이터가 전부 smoke 흔적이라 통계 의미 없음 | 월요일 이후 재측정 |
| — (추가 발견) | smoke 중 FK violation (publisher race) | **별도 fix 커밋** (publisher DB upsert) |
| — (추가 개선) | Scout 는 per-ticker 비용 없음 → 고급 모델로 품질 향상 가치 | **Scout primary = Opus, shadow = DeepSeek** 전환 + 비교 데이터 축적 |

---

## 1. Track V — 검증 결과

### V1. kospi_20d_vol 58% 의 진짜 원인

**3층 교차 검증**:

1. 계산 로직 (`feeders/real.py:90-106`):
   ```python
   vol20 = statistics.stdev(changes[:20]) * math.sqrt(252)
   ```
   → `sqrt(252)` 연환산 적용됨. 로그 수익률 아닌 단순 수익률 사용 (평시 오차 작음).
2. DB 저장 값 (`index_daily_prices.change_pct`): 수동 재계산
   `(close - prev_close) / prev_close * 100` 대비 0.00005 이내 일치 (부동소수점 오차).
3. 원본 KOSPI 종가: 2026-04-08 +6.87%, 04-01 +8.44%, 03-23 −6.49%, 03-18 +5.04%
   등 20 영업일 중 6일이 ±4% 이상 변동.

DB 쿼리로 직접 vol 재계산:
```sql
WITH recent AS (
  SELECT change_pct/100.0 AS r FROM index_daily_prices
  WHERE index_code = 'KOSPI' AND change_pct IS NOT NULL
  ORDER BY price_date DESC LIMIT 20
) SELECT STDDEV_SAMP(r) * SQRT(252) FROM recent;
-- 결과: 0.5838 (58.4%)
```

**결론**: **실제 KOSPI 변동성이 위기 수준인 게 맞다**. 사용자 확인: "이란전쟁 종료/재개 뉴스로 하루 수백 포인트씩 출렁이고 있음". 민지의 "계산 버그 or threshold 실수" 가설은 전제부터 틀림. F1 (bypass 단독 해제) · F2 (threshold tiered 재설계) 는 **불필요**.

### V2 + V3. screening-executor 샌드박스

`adapter.py:107-126` 의 실 argv (docker 모드):
```
--rm -i --network=none --read-only --memory=4g --cpus=2
--security-opt=no-new-privileges:true --cap-drop=ALL
--user=1000:1000 --tmpfs=/tmp:size=256m
```

- 스펙 §6.2 대비 누락: `--security-opt=seccomp=<profile>` 만. `-v ./data:/data:ro`
  마운트는 stdin JSON 전달 설계로 의도적 생략.
- seccomp 프로파일 파일은 리포에 **부재**.

**조치 (F4)**: Moby v24.0.7 default seccomp (31 syscall 그룹, `defaultAction=SCMP_ACT_ERRNO`)
를 `infra/screening/seccomp.json` 에 포함. adapter 에 `SCREENING_SECCOMP_PROFILE`
env (호스트 절대경로) 주입 시 조건부 argv 추가. MS-01 `.env` 에 절대경로 세팅 필요
(docker-in-docker 구조라 호스트 기준).

### V4. Scout → position_sheet 필터 체인

pipeline.py 실제 flow (line 204-610):
1. Macro closed → Scout 스킵 ✅
2. validate_candidates → universe 밖 ticker (hallucination) 거부 ✅
3. StrategyEngine.build_sheet_with_reason → 전략 룰별 거부 ✅
4. publisher.publish → stream + DB upsert ✅
5. update_candidate_promotion → FK UPDATE

민지의 "필터 전무" 는 과장. 하지만 **백테스트 기반 필터는 확실히 없음** — 과거 outcome
평균 등으로 걸러내는 층은 Phase 3-Backtest Engine 영역이라 현재 미구현.

### V5. allowlist 우회

`allowlist.py` 검사 범위:
- `ast.Import` / `ast.ImportFrom` → ALLOWED_MODULES prefix 매치
- `ast.Call` → FORBIDDEN_CALLS (`__import__, eval, exec, compile`) 이름 매치

**미검사**:
- `ast.Attribute` 노드 자체 (→ `getattr(x, "__import__")`, `obj.__class__.__mro__` 미차단)
- `ast.Name(ctx=Load)` 에서 `getattr/setattr/globals/vars/locals` (→ builtins 우회)
- pandas `read_pickle` / numpy `load(allow_pickle=True)` (→ pickle 실행 경로)

코드 주석 자체가 "getattr 우회는 화이트리스트 모듈 한정에서 막기 어려우므로 docs
명시 (Phase 1 한계)" 로 자인.

**조치 (F5)**: `FORBIDDEN_ATTRS` + `FORBIDDEN_NAMES` 세트 추가 + `ast.Attribute`
검사 + pickle 패턴 별도 검사. M13~M17 회귀 방지 테스트 10건 추가.

### V6. Hallucination rate

14일 데이터 분포 (방금 smoke 이전 시점):

| trigger_reason | 건수 | 비고 |
|---|---|---|
| scheduled:scout_daily | 5 (macro only) | slow-loop 정기 실행 흔적 — 전부 Macro closed 로 Scout skip |
| smoke | 14 (macro 7 + scout 7) | 사용자 수동 smoke 실행 |

→ **14일간 정기 scout_runs 0건**. scout_daily job 은 success 상태로 끝나지만 Macro
closed 로 Scout phase 진입 없음. hallucination rate 실측 불가.

월요일 08:30 KST 이후 정기 실행에서 Scout 가 돌기 시작하면 재측정 필요.

---

## 2. Track F — 수행한 수정

### F3. REAL_MODE_MIGRATION_CHECKLIST Blocking Pre-check Gates (커밋 `07791bc`)

stop 해제 직전 관문으로 `§0.1 Blocking Pre-check Gates` 신설:

- **G1**: `MACRO_AUTO_OVERRIDE_DISABLED` env 제거/0 확인. bypass 켠 채 stop 해제 시
  고변동성 장세에서 자동 closed 방어 무력화.
- **G2**: 최근 1시간 macro_runs 의 bypass 흔적 미존재.
- **G3**: seccomp 프로파일 파일 실재 + env 주입 (F4 이후).
- **G4**: allowlist M13~M17 우회 테스트 green (F5 이후).

4개 중 하나라도 미충족 시 stop 해제 금지.

### F4. seccomp Profile + Conditional Adapter Argv (커밋 `2fec603`)

- `infra/screening/seccomp.json`: Moby v24.0.7 default (828 줄, 31 syscall 그룹).
- `adapter.py`: `SCREENING_SECCOMP_PROFILE` env (호스트 절대경로) 주입 시 argv 추가.
- `docker-compose.yml`: slow-loop 서비스에 env placeholder.
- 테스트: env 유/무/빈 3 케이스 argv 검증.

**배포 확인**: 2026-04-19, MS-01 `.env` 에 `SCREENING_SECCOMP_PROFILE=/home/youngs75/projects/prime-jennie-runtime/infra/screening/seccomp.json`
추가 + `docker compose --profile full up -d --force-recreate slow-loop` 수행. env 주입 검증 완료.

### F5. allowlist Hardening (커밋 `32e6aeb`)

- `FORBIDDEN_ATTRS`: `__import__`, `__builtins__`, `__globals__`, `__dict__`,
  `__subclasses__`, `__class__`, `__mro__`, `__bases__`
- `FORBIDDEN_NAMES`: `getattr`, `setattr`, `globals`, `vars`, `locals`, `__builtins__`
  (`ast.Name(ctx=Load)` 한정 — user-define 통과)
- pandas `read_pickle` / numpy `load(allow_pickle=True)` 패턴 매치
- executor 에서 `"attr:"` prefix 도 `forbidden_call` 로 매핑
- 테스트 `TestAttributeBypass` 10건 추가 (M13~M17 + dunder 변형 + user-define 통과 확인)

### F1 / F2 — 기각

V1 결과로 근거 소멸. threshold 35% 는 58% vol 장세에 정당히 closed 트리거 작동 중.
bypass 는 stop=1 기간의 의도된 임시 장치 (compose 주석에 명시). 단독 해제는 위험
(F3 의 G1 이 이 시나리오 차단).

### F6 — 보류

backtest stub (recent-outcome 필터) 은 "조건부 임시방편" 성격. 월요일 이후 정기
scout_runs 가 쌓이고 rejection_reason 분포가 의미 있어진 뒤 재평가.

---

## 3. 추가 발견 — Publisher FK Race

### 증상

2026-04-19 오전 smoke 실행 중 발생:
```
IntegrityError: foreign key violation
(promoted_to_sheet_id)=(ps_20260419_035720_275c) is not present in position_sheets
[SQL: UPDATE screening_candidates SET promoted_to_sheet_id = $1, rejection_reason = $2
      WHERE scout_run_id = $3 AND ticker = $4]
```

### 원인

- `PositionSheetPublisher.publish()` 가 Redis Stream 에만 발행 (주석: "DB
  persistence 는 Phase 2 운영화 시 이 클래스에 주입 (별도 Writer 프로토콜)").
- `update_candidate_promotion` 은 stream 발행 직후 `promoted_to_sheet_id` 로
  FK UPDATE 시도.
- fast_loop 의 stream consume + DB insert 는 비동기라 race 필연.

### 영향

이전 14일치 `screening_candidates.rejection_reason='engine_error'` 15건 중 상당수가
이 FK 위반의 catch-all 분류일 가능성. `pipeline.py:534-553` 에서 모든
`StrategyEngine.build_sheet_with_reason` 예외를 통째로 `engine_error` 로 처리 중.

### 조치 (커밋 `0be41c0`)

`publisher.publish()` 내부에 **stream 발행 전** `position_sheets` upsert
(ON CONFLICT DO NOTHING) 를 동기 단계로 추가. fast_loop 중복 insert 는 ON CONFLICT
로 무해. DB 실패는 raise (FK 연계 실패를 조용히 삼키지 않기), stream 실패만 DLQ guard.

---

## 4. 추가 개선 — Scout Primary 교체 (`5f73a6f`)

### 동기

Scout 는 하루 수 회 cron 에 한 번씩 Python 코드를 생성만 함. per-ticker LLM 호출이
없으므로 **고급 모델의 품질 상승이 전체 screening 품질을 결정**. 저렴한 모델 선택의
근거는 더 이상 유효하지 않음.

### 변경

- Scout primary: DeepSeek chat → **Claude Opus 4.7** (Macro 와 동일 인스턴스 재사용)
- Scout shadow: DeepSeek chat (Macro shadow 와 동일 패턴으로 비교 데이터 축적)
- `SCOUT_SHADOW_ENABLED` env (기본 1) + `shadow_strong` tier 신설
- `persist_scout_run(shadow_result=...)` 로 metadata_json.shadow JSON merge
- `scout_shadow` service llm_stats 누적

### 비용 영향

Opus 전환: 평일 정기 실행 7회 기준 **일당 $2~5 추가** 예상. DeepSeek shadow 는 거의
무시 가능. Scout 품질 상승의 투자 대비 합리적.

---

## 5. 검증 Smoke (2026-04-19 오전)

배포 완료 (`0be41c0` head, `SCOUT_SHADOW_ENABLED=1`, `ANTHROPIC_MODEL=claude-opus-4-7`)
후 `smoke_slow_loop_once.py` 실행.

### 결과 비교

| 지표 | 이전 smoke (DeepSeek Scout + FK 버그) | 이번 smoke (Opus + FK fix) |
|---|---|---|
| sheets_published | 5 | **10** |
| sheets_rejected | 0 | **0** |
| screening_candidates promoted | 0/15 (engine_error 100%) | **10/10 (promoted 100%)** |
| 전략 다양성 | SECTOR_MOMENTUM only | SECTOR_MOMENTUM + GAP_UP_REBOUND + EARNINGS_DRIFT |
| Macro size | 0.5 | 0.75 |
| Opus/DeepSeek 병렬 호출 | (없음) | Anthropic 2회 + DeepSeek 2회 |

### Scout 가설 품질 상승 (사례 비교)

**이전 (DeepSeek)**: 『상위 섹터 모멘텀(건설/부동산, 2차전지 등)에 속하는 종목 중
최근 긍정적 뉴스 감성을 보이는 종목은 단기적으로 추가 상승 가능성이 높다.』

**이번 (Opus)**: 『매크로 게이트는 열렸지만 WTI 급락·고변동성 환경이므로, MA20 위
추세 + 긍정 뉴스(≤48h) + 건전한 거래량 증가를 동시 만족하는 종목만 선별한다. RSI
과열/과매도 및 당일 급락(−7%↓)·고변동성(일일std>8%)은 배제하고 상위 10개로 섹터
분산.』

→ **현재 장세(이란전쟁·WTI)를 즉시 인지해 수비적 필터 자동 적용**. 단순 모멘텀
전략에서 리스크 대응이 내장된 정교한 전략으로 질적 도약.

### DB 실태

```
position_sheets (최근 10분) | 10 row
screening_candidates outcome | promoted: 10 (100%)
scout_runs.model_used | claude-opus-4-7
scout_runs.shadow_model | deepseek-chat (latency 35s)
macro_runs.gate/auto_override | open / false (bypass 정상 작동)
```

---

## 6. 남은 운영 TODO

- [ ] **월요일(2026-04-20) 08:30 KST 정기 실행 관찰**
  - `slow_loop.scout_daily` success + scout_runs 축적 확인
  - bypass ON 상태에서 `auto_override=false`, shadow merge 정상 기록
  - Scout 가설의 장세 인지 품질 (vol 58% 반영 여부)
- [ ] **hallucination rate 측정** (V6 재시도) — 14일치 쌓인 후 off_universe 비율
- [ ] **engine_error 잔여 케이스 세분화** — ValidationError/valid_until/after_hours 분리
- [ ] **F6 (backtest stub) 재평가** — 정기 운영 rejection_reason 분포 확인 후
- [ ] **Scout/Macro shadow 비교 평가** — 1~2주 축적 후 gate 일치율, hypothesis
  다양성, code diff 등으로 Opus 대비 DeepSeek 의 실 성능 차이 정량화

---

## 7. 관련 커밋

| 해시 | 주제 |
|---|---|
| `32e6aeb` | fix(screening): harden ast allowlist (F5) |
| `07791bc` | docs(real-mode): add blocking pre-check gates (F3) |
| `2fec603` | feat(screening): ship seccomp profile + adapter argv (F4) |
| `5f73a6f` | feat(slow-loop): Scout primary=Opus + DeepSeek shadow |
| `0be41c0` | fix(slow-loop): position_sheets DB upsert before stream pub (FK race) |

## 8. 참고

- 민지 리뷰 원본: `../INVESTIGATION_TASKS_2026-04-19.md` (projects 폴더)
- 관련 스펙: `MACRO_GATE_SPEC.md`, `SCOUT_CODE_GENERATION.md`, `PHASE_2_13_COMPLETE.md`
- 운영 체크리스트: `REAL_MODE_MIGRATION_CHECKLIST.md`

**문서 끝.**
