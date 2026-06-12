# Control State Redis 키 매핑 (v2 ↔ v3 공존기)

> Phase 2-6 (v2/v3 공존) 동안 유효. v2 영구 종료 시점에 V2_KEY_* 상수 + `_resume`
> 의 v2 키 정리 로직을 삭제한다.

## v3 표준 키 (`control.state:*`)

| 키 | 의미 | Writer | Reader |
|----|------|--------|--------|
| `control.state:stop` | 긴급 정지. 모든 진입/청산 중단. `/resume` 으로만 해제. | `ControlCommandConsumer._emergency_stop` | `SystemState.snapshot`, `BalanceAwareSizer`, `EntryExecutor`, slow_loop pipeline, telegram `/status` |
| `control.state:pause` | 일시 정지. **무확인 진입 차단** — 자동 진입 + 무확인 수동 매수(manual_buy)를 막고, 청산과 2단계 확인을 거친 승인 매수(approved_buy, `/accept`)는 통과 (2026-06-12 시나리오 B 정책). value=pause_reason. | `ControlCommandConsumer._pause` / `_emergency_stop` | 동일 |
| `control.state:dryrun` | DRY_RUN 플래그. 실 주문 금지 (시뮬레이션만). | `_set_dryrun` | `BalanceAwareSizer`, `EntryExecutor`, `_apply_manual_trade` |
| `control.state:liquidate_armed` | 강제 청산 armed flag. | `_liquidate_arm` / `_liquidate_disarm` | TickLoop |

## v2 호환 키 (legacy, deprecated)

| 키 | 의미 (v2) | 현재 v3 동작 |
|----|----------|-------------|
| `trading_flags:stop` | v2 fast-loop 의 매수/매도 진입 차단 (BalanceAwareSizer 등). | v3 fast_loop **읽지 않음**. `/resume` 명령이 v3 키와 함께 DEL 한다. |
| `trading_flags:pause` | v2 사이드의 일시정지 flag. | 동일. v3 미사용. `/resume` 에서 함께 DEL. |

### 왜 남아있나
- `docs/REAL_MODE_MIGRATION_CHECKLIST.md` 가 운영자에게 **양쪽 키를 동시에
  SET** 하는 관행을 권장 (double safety) — v3 가 신규 추가된 환경에서 v2
  컨테이너가 동시 가동되는 기간 대비.
- `_resume` 가 v3 키만 DEL 하던 시기 (~2026-05-10) 에 v2 키 잔존이 confusing
  하다는 피드백 (session 2026-05-08-0001 참조) → `_resume` 가 v2 키도 DEL 하도록
  보강 (2026-05-11).

### 매핑 표 — `/resume` 시 정리되는 키

```
/resume → DEL control.state:stop
        + DEL control.state:pause
        + DEL trading_flags:stop      (v2 호환)
        + DEL trading_flags:pause     (v2 호환)
```

`set_dryrun`, `liquidate_*` 는 v2 와 매핑 없음 — v3 전용.

## 마이그레이션 종료 시점 체크리스트

v2 컨테이너 영구 종료가 확정되면:

1. `prime_jennie_runtime/telegram_bot/control.py` 에서 `V2_KEY_STOP`,
   `V2_KEY_PAUSE` 상수 + `__all__` 항목 제거.
2. `prime_jennie_runtime/control/consumer.py` 의 `_resume` 에서 v2 키 인자를
   `redis.delete()` 호출에서 제거.
3. `docs/REAL_MODE_MIGRATION_CHECKLIST.md` 의 `trading_flags:stop SET 0/1`
   가이드 문구 정리.
4. 본 문서 삭제 또는 historical note 로만 유지.

## 관련 파일

- `prime_jennie_runtime/control/consumer.py` — `_resume` 핸들러
- `prime_jennie_runtime/telegram_bot/control.py` — 키 상수 정의
- `prime_jennie_runtime/fast_loop/app.py` — entry path 진입 차단 게이트
- `prime_jennie_runtime/control/state.py` — `SystemState.snapshot` (v3 키만 읽음)
- `tests/control/test_consumer.py` — `/resume` 가 v3 + v2 키 모두 DEL 검증
