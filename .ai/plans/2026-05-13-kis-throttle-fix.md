# 5-13 KIS Throttle 사고 — Fix A/B/C/D 플랜

## 사고 요약
stale `position_state` (외부 청산된 028050/241560) → exit_executor 가 미체결 시 state 미정정 → 같은 sheet 무한 매도 재시도 → tick burst 가 trade_limiter sliding window 통과 → KIS `EGW00201` throttle → circuit breaker open → fast_loop 가 503 받고 task die → restart → 동일 stale state 재load → 루프 반복.

## 즉시 차단 (이미 완료)
- Redis `DEL position_state:ps_20260506_028050_b46f` + `..._241560_75c6`
- `docker restart prime-jennie-runtime-fast-loop-1` → in-memory 8 → 6 states

## Fix A — sell_not_filled 연속 N회 후 sheet auto-close

**의도**: 같은 sheet 의 매도 실패가 누적되면 자동으로 sheet 를 close 해서 무한 루프 차단.

**위치**: `prime_jennie_runtime/fast_loop/exit_executor.py`

**설계**:
- `PositionState` dataclass 에 `exit_fail_count: int = 0` 필드 추가 (`fast_loop/domain.py`)
- `ExitExecutor.execute()` 의 `sell_not_filled` / `sell_rejected` 경로에서 `state.exit_fail_count += 1` 후 `tracker.persist(sheet_id)`
- threshold (default 3) 도달 시 `tracker.close(sheet_id)` + 텔레그램 알림 ("exit_abandoned" notification)
- 성공 매도 (filled_qty > 0) 시 `state.exit_fail_count = 0` 리셋

**테스트**:
- 미체결 3회 후 auto-close 확인
- 성공 매도 후 fail_count 리셋 확인
- 부분 체결도 성공 처리

**risk**: threshold 너무 낮으면 정상 미체결 (장 마감 직전) 도 close. 3회 + 텔레그램 alert 가 적절.

## Fix B — fast_loop 부팅 시 KIS-position diff 알림

**의도**: 외부 청산 (수동 매도) 으로 인한 stale state 를 자동 정리하지는 않음 (사용자 메모리 `feedback_sync_positions_manual.md` 준수). 대신 부팅 시 mismatch 를 즉시 알림.

**위치**: `prime_jennie_runtime/fast_loop/app.py` startup 직후 + 새 헬퍼 (`fast_loop/position_sync_check.py`)

**설계**:
- `position_tracker.load_from_redis()` 직후, KIS `/api/balance` 호출
- KIS 잔고에 없는 ticker 의 position_state 가 있으면 sheet_id 목록 수집
- 텔레그램 알림 ("startup_state_mismatch: 028050, 241560" 형태) — 자동 close 는 하지 않음
- 사용자가 텔레그램 명령으로 수동 close 또는 redis DEL 수행

**테스트**:
- mismatch 발생 시 alert payload 검증
- 일치 시 silent

**risk**: 알림 빈도. 정상 매도 후에도 짧은 시간 mismatch 가능 → startup 한 번만 검사 (지속 모니터링 X).

## Fix C — fast_loop task die 방지 (503/CircuitBreakerError catch)

**의도**: tick_loop 단일 sheet 처리 실패가 전체 fast_loop 종료로 이어지지 않게.

**위치**: `prime_jennie_runtime/fast_loop/tick_loop.py` `_evaluate_exits` / `_evaluate_pending_entries`

**설계**:
- `await self._exit_executor.execute(state, decision)` 를 `try/except Exception` 으로 감쌈
- HTTPStatusError / TimeoutError / ConnectionError 는 WARN 로깅 후 continue
- 예상치 못한 Exception 은 ERROR 로깅 후 continue (단 KeyboardInterrupt 등 BaseException 은 propagate)
- `_evaluate_pending_entries` 도 동일 처리

**테스트**:
- `exit_executor.execute` mock 이 503 raise → tick_loop 가 task die 안 함
- 같은 sheet 후속 tick 에서 다시 try 가능

**risk**: exception 삼키면 진짜 버그가 silent. WARN/ERROR 로깅 + Fix A 의 fail_count 누적으로 보완.

## Fix D — trade_limiter burst 방지

**의도**: sliding window 의 윈도우 경계 burst (1초당 5건 → 200ms 안에 10건 가능) 차단.

**위치**: `prime_jennie_runtime/kis_gateway/rate_limiter.py`

**설계 옵션**:
- (D1) Token bucket: capacity=5, refill=5/sec — burst 최대 5, 평균 5/sec. 가장 안전
- (D2) 현재 sliding window 유지하되 `rate_limit_trade_per_sec` 를 5→3 으로 강하 — 단순하나 정상 거래 처리량 감소

**선택**: D1 token bucket. 코드량 비슷, burst 안전성 큼.

**테스트**:
- 1초에 5건 허용, 6번째는 대기
- 윈도우 경계 burst 차단 (10건 200ms 시도 → 5 통과 / 5 대기)

**risk**: 신규 구현 버그 가능. 기존 단위 테스트 (있다면) 통과 + 신규 테스트 추가.

## 작업 순서
1. Fix A (state 정정 + auto-close) — root cause 직접 차단
2. Fix C (exception catch) — A 의 fail_count 누적 보조
3. Fix B (startup mismatch 알림) — 사용자 인지 보조
4. Fix D (token bucket) — 가장 외부 layer 의 burst 방지

## Deploy
- 모든 fix 동일 PR 로 묶어 push to main
- MS-01 자동 deploy (GHCR build → docker compose up)
- deploy 후 `position_tracker restored N` 로그 + 텔레그램 알림 (Fix B) 확인

## 미포함 (다음 세션 또는 별도 작업)
- exit_executor 의 정상 매도 path 의 KIS 응답 race condition (있는지 검증 필요)
- monitoring dashboard 의 stale state 시각화
- sync_positions 의 자동화 재검토 (현재 OFF, Fix B 알림으로 보완)
