# Cooldown 가드 + B2/C Advisory Design (2026-05-15)

> **2026-05-23 갱신**: 5-22 결정론 코어 전환 후에도 **active**. L1 enforcement 는 fast_loop 에 도입 완료, L2 advisory 는 event_log 적재 정상. 자세한 배경 — `.ai/designs/2026-05-23-post-llm-at-core-realignment.md` §8.1, §9.

> 본 문서는 `.ai/designs/2026-05-14-agent-coordinator.md` §5 의 정책 B2/C 를
> 오늘 (5-15) 실제 손실 케이스를 트리거로 **immediate hotfix + Stage 2 advisory**
> 두 layer 로 구체화한다.

## 1. Background — 오늘 발생한 손실 패턴

KST 5-15 거래일 시초 흐름:
- **5-14 보유 3건 시초 갭다운 fixed_sl 매도** (KST 09:00~09:03):
  - 009540 -5.17% / 001440 -3.96% / 062040 -4.17% — 합 **-137만원**
- **5-15 09:30 sheet 재발행 → fast_loop entry 9:30:45**:
  - 001440 → @ 61,600 × 126주 **같은 날 재진입**
  - 062040 → @ 279,000 × 27주 **같은 날 재진입**

원인: **trading-domain.md:48 의 cooldown 룰이 v3 미포팅**. v2 의 "3일 + 24h" cooldown 이 v3 에선 `ActiveSheetChecker` Protocol + NullChecker 만 있고 RealChecker 0건 (audit B2). 결과적으로 손절 직후 동일 종목 재진입을 막는 게이트가 전 path 에 없음.

→ 같은 패턴이 매일 발생할 수 있는 구조적 결함. 운영 첫 데이터로 가시화됨.

## 2. Two-layer 접근

| Layer | 목적 | 위치 | 동작 | 영향 |
|---|---|---|---|---|
| **L1 Enforcement** | 실제 차단 | `fast_loop/consumer.py` 의 sheet 처리 직전 | 손절 24h 내 ticker → enqueue skip | 매매 path 직접 변경 |
| **L2 Observation** | 관찰 + advisory | `coordinator/policies/` 의 정책 | 동일 logic 평가 후 event publish | 매매 영향 0 (advisory only) |

두 layer 의 logic 은 **동일해야** — drift 시 자동 검출 가능 (L1 reject 횟수 vs L2 advisory 횟수). Stage 2 → Stage 3 (enforce mode) 진화 시 L2 가 L1 을 대체할 수 있도록 같은 입력 / 같은 출력 contract.

## 3. L1 Cooldown 가드 (immediate hotfix)

### Trigger
`fast_loop/consumer.py` 의 sheet consumer 가 새 sheet 를 받았을 때 — 기존 reject 분기 (`sheet rejected (already holding)` 옆자리) 에 **`sheet rejected (recent_stoploss_cooldown)`** 추가.

### Data source
```sql
SELECT 1 FROM executions e
JOIN position_sheets ps USING (sheet_id)
WHERE ps.ticker = $1
  AND e.side = 'sell'
  AND (e.metadata_json->>'reason') IN ('fixed_sl', 'stop_loss', 'breakeven_stop')
  AND e.executed_at > now() - interval '24 hours'
LIMIT 1;
```

장점:
- DB 가 단일 truth source. redis flush 등 cache invalidation 문제 없음.
- 인덱스: `executions(sheet_id, executed_at)` 활용. ticker 는 join 통해 ps 에서.
- 9건 sheet burst 시 9 query — 5ms × 9 = 45ms 미만, 무시할 수준.

### 범위 (강도 결정)
| Scope | 차단 강도 | 권장 |
|---|---|---|
| (ticker, strategy_tag) | 약 (전략 바꿔서 진입 가능) | ❌ |
| ticker 단독 | 중 (24h 내 같은 종목 어떤 전략도 차단) | ✅ 권장 |
| ticker + 섹터 | 강 (해당 섹터 모두 차단) | 과함 |

**ticker 단독, 24h** — trading-domain.md 의 v2 "24h" 룰 그대로. 3일은 너무 길어 회복 기회 차단. 운영 데이터 누적 후 조정.

### 출력
- log: `sheet rejected (recent_stoploss_cooldown): ticker=X last_exit=Y reason=Z`
- coordinator stream 으로 `entry_rejected` event publish (reason="recent_stoploss_cooldown")
- positions 미변경, KIS 미호출

### 변경 파일
- `prime_jennie_runtime/fast_loop/cooldown_check.py` 신규 (~60줄)
- `prime_jennie_runtime/fast_loop/consumer.py` 수정 (+10줄, 기존 reject 분기 옆)
- `tests/fast_loop/test_consumer_cooldown.py` 신규 (~120줄)

## 4. L2 B2 + C Advisory (Stage 2 첫 use case)

Stage 2 design (`2026-05-14-agent-coordinator.md` §5) 의 B2 / C 항목 implementation.

### 두 정책의 차이
| 정책 | trigger 이벤트 | 검사 |
|---|---|---|
| **C recent_stoploss_cooldown** | `sheet_published` (slow_loop) OR `entry_queued` (fast_loop) | 종목의 24h 내 stop sell 이력 |
| **B2 duplicate_today** | `sheet_published` (slow_loop) | 같은 거래일 같은 ticker 이미 sheet 있음 (active든 closed든) |

오늘 케이스는 C 가 핵심 — 어제 진입한 종목을 오늘 손절 후 재진입. B2 도 별개로 의미 있음 (같은 거래일 같은 종목 재발행).

### 위치
- `prime_jennie_runtime/coordinator/policies/__init__.py` 신규
- `prime_jennie_runtime/coordinator/policies/recent_stoploss_cooldown.py` 신규 (~80줄)
- `prime_jennie_runtime/coordinator/policies/duplicate_today.py` 신규 (~70줄)

### 발화
sheet_published 또는 entry_queued 이벤트가 listener 에 도착 → event_log INSERT 후 → policy evaluation → 위반 시 `GenericAlertNotification(severity="warning", title="...", body="...")` publish (notifier 경유).

**enforcement 0** — fast_loop 는 L1 게이트만 신뢰. L2 는 alert 만 발화.

### decision_log 기록
- 모든 policy evaluation 결과 (OK/NOT_OK) 가 `decision_log` 에 row 로 남음
- Stage 1 에서 빈 채로 둔 테이블이 처음 채워지기 시작
- Phase 0 #1 (conviction-outcome correlation) 의 입력 데이터 원천

## 5. 일관성 검증 (drift detection)

매일 batch 검증 (예: 21:00 의 `contract_smoke_test` cron 옆자리):
- 오늘 L1 reject count (`recent_stoploss_cooldown` reason)
- 오늘 L2 advisory count (decision_log 에서 같은 정책 NOT_OK)
- **두 수치가 일치하지 않으면 drift** → telegram alert

이게 Stage 3 (enforce) 이전 단계의 신뢰 빌딩. 충분한 일치 누적 후 L1 폐기하고 L2 가 enforce 권한 가져감.

## 6. Implementation plan (KST 15:30 이후)

| 순서 | 항목 | 예상 라인 | 예상 시간 |
|---|---|---|---|
| 1 | `fast_loop/cooldown_check.py` 신규 + consumer wire | 60+10 | 30분 |
| 2 | `tests/fast_loop/test_consumer_cooldown.py` | 120 | 30분 |
| 3 | `coordinator/policies/*.py` 2개 + dispatcher | 80+70+30 | 1시간 |
| 4 | `tests/coordinator/test_policies.py` | 150 | 30분 |
| 5 | ruff + pytest + commit + push (1 commit) | — | 10분 |
| 6 | Deploy 검증 + dry run 시나리오 (event 인위 발화) | — | 30분 |

**총 약 3시간**. 장 마감 (15:30) → 18:30 사이에 완료 가능.

### 핵심 안전성
- L1 추가는 fast_loop consumer 의 1개 분기 추가 — happy path 영향 0
- L2 는 coordinator 안 — 매매 path 와 분리
- Deploy 는 모든 컨테이너 재시작 가능성 있음 — 18:00 이전 (장 마감 후 30분 안정화 보장)

## 7. 미결정 / 후속

- cooldown 기간 24h 가 충분히 보수적인가? 3일 가능성도 — 운영 1주 누적 후 결정.
- `breakeven_stop` reason 도 cooldown 대상에 포함할지? 일단 fixed_sl / stop_loss 만 (breakeven 은 -0% 손익이라 보수 불필요).
- B1 (Scout history blindness) — slow_loop scout 가 같은 종목 매시간 반복 추천. 별개 정책, 본 design 범위 밖.
- E2 (FIRST_COMPLETED race) — Stage 4 supervisor 영역.

## 8. 참조

- 글로벌 메모리: `trading-domain.md:48` — "Cooldown 3일 + 24h v3 미포팅"
- 로컬 메모리: `feedback_trading_hour_deploy_gate.md` (2026-05-15 학습)
- 상위 design: `.ai/designs/2026-05-14-agent-coordinator.md` §5, §8 Stage 2~3
- audit: `project_audit_2026_05_14.md` B2, C
