# `fast_loop/` — 빠른 루프 (Executor)

Track C 소유. **절대 원칙: LLM 호출 금지.** 모든 결정은 결정론.

## 책임

- Redis Stream `v3:position_sheets` 소비 → `PendingEntryQueue` 적재 (보유/큐 dedup + 24h 손절 쿨다운)
- KIS WebSocket 틱 수신 → BarEngine 1분봉 집계 → 진입 조건 평가 → entry_executor
- 보유 sheet 의 9 exit rule 매 tick 평가(first_match) → exit_executor
- `executions` / `positions` / **`outcomes` 영속화** (2026-05-22 outcomes 복원)
- Intraday Risk Throttle 5단계 (Redis publish → slow_loop 도 구독)
- 부분 체결 처리 (entry/exit 양쪽 — 잔량 취소 + 실체결량 재확정)
- 강제 청산 (`forced_liquidation:stocks` set + `control.state:liquidate_armed`)

## 핵심 파일

| 파일 | 역할 |
|---|---|
| `consumer.py` | `v3:position_sheets` 소비 → PendingEntryQueue 적재 (4단계 게이트) |
| `tick_loop.py` | 틱 1회 생명주기 (bar 집계 → 강제청산 → exit → entry) |
| `pending_entry.py` | 진입 대기 큐 + `EntryConditionEvaluator` (6 condition) |
| `entry_executor.py` | 시장가/지정가 매수 + 부분체결 취소·재확정 |
| `exit_evaluator.py` | 9 exit rule first_match (time_stop · death_cross 등) |
| `exit_executor.py` | 시장가 매도 + 부분체결 처리 + fully_closed → outcomes 적재 |
| `bar_engine.py` | 1분봉 누적 + ma20/RSI(14)/recent_high/intraday_cum_volume |
| `risk_throttle.py` · `risk_updater.py` | NORMAL/CAUTION/WARNING/DANGER/CRITICAL 5단계 |
| `position_tracker.py` | sheet 별 PositionState (Redis 영속) |
| `cooldown_check.py` | 24h `metadata_json->>'exit_reason'` IN (fixed_sl, stop_loss, breakeven_stop) |
| `persistence.py` | PostgresTradeRecorder — executions / positions / **outcomes** UPSERT, `compute_outcome` 공용 함수 |
| `notifier.py` | TradeNotification → Redis stream + (옵션) Slack mirror |
| `gateway_subscriber.py` | KIS 실시간 구독 add-only |
| `position_sync_check.py` | 기동 시 KIS ↔ Redis state 수량 비교 |

## 진입 / 청산 path 요약

```
[v3:position_sheets stream] → consumer (보유/큐/24h 손절 쿨다운 dedup)
                            → PendingEntryQueue (valid_until TTL)
                            → KIS subscribe(ticker)

[kis:prices tick] → tick_loop._process
   ├─ bar_engine.update                  # 1분봉 누적
   ├─ _evaluate_forced_liquidation       # armed && ticker∈set → 즉시 매도
   ├─ _evaluate_exits                    # 보유 sheet → 9 rule first_match
   │     → exit_executor.execute → KIS sell → executions INSERT
   │     → fully_closed 이면 outcomes UPSERT (record_sell 내부)
   └─ _evaluate_pending_entries          # 대기 sheet → conditions 평가
         → BalanceAwareSizer (cash/total_asset 클램프)
         → entry_executor.execute → KIS buy → executions INSERT + positions UPSERT
```

## outcomes 적재 (2026-05-22)

`PostgresTradeRecorder.record_sell(... fully_closed=False)` 가 호출자가 `fully_closed=True` 로 전달했을 때, executions INSERT 이후 **별도 트랜잭션**에서 `_record_outcome(sheet_id)` 호출.

- 같은 sheet 의 모든 buy/sell 행을 다시 읽어 `compute_outcome` 으로 가중평균 entry/exit · 거래비용(매수 0.014%/매도 0.214%) 차감 net pnl · 마지막 매도의 exit_reason 추출
- outcomes UPSERT (sheet_id PK, ON CONFLICT DO UPDATE)
- best-effort — outcome 실패가 executions/positions 기록을 절대 롤백시키지 않음
- 부분 청산(scale_out 중간)은 기록 X — 최종 청산에서만 1행

`compute_outcome` 은 backfill 스크립트 / 미래 재계산에도 동일 로직 공유.

## v2 포팅 대상

| 컴포넌트 | v2 원본 경로 |
|---|---|
| Executor 프레임 | `prime_jennie/services/buyer/executor.py`, `seller/executor.py` |
| Risk Throttle | `prime_jennie/services/jobs/app.py::_check_intraday_risk()` |
| Position sizing | `prime_jennie/services/buyer/position_sizing.py` |
| Scanner 지표 | `prime_jennie/services/scanner/` — bar_engine 으로 부분 포팅 (5-11) |

## 설계 원칙

- 결정론적 코드만. 모든 판단은 PositionSheet `exit.rules[]` / `entry.conditions[]` 에 이미 정의됨
- KIS 복잡도는 `kis_gateway/` 로 분리. 여기서는 얇은 HTTP 클라이언트만
- 틱마다 exit rules 배열 순서대로 평가, first_match 청산 — rule 순서가 중요 (예: BreakevenRule 이 앞에 있으면 같은 tick 의 후속 rule 건너뜀)
- STOP 키는 글로벌 킬스위치 — 진입 + 청산 + 강제청산 모두 차단. 청산 우선이 필요하면 `/resume` 선행 필요
- PAUSE 는 진입만 차단, 청산 허용
- 매매 path 의 try/except 는 격리 (KIS 호출 실패가 tick loop 자체를 안 죽임)
