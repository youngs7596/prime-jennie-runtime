# monitor/ — v3 Observability & Dashboard Feeder (Track C, Phase 2.10)

v2 `prime_jennie/services/monitor/` 포팅. **재작성이 아님** — v3 는 기능이 분할됐다:

| v2 monitor 책임 | v3 위치 |
|---|---|
| 포지션 실시간 감시 + 매도 시그널 | `fast_loop/tick_loop.py` + `exit_evaluator.py` (이미 구현) |
| 기술적 지표 계산 (RSI/ATR/death_cross) | `fast_loop/indicators.py` (이미 구현) |
| Redis live snapshot (dashboard용) | **여기** (monitor/) |
| Prometheus metrics (Grafana/Loki용) | **여기** (monitor/) |
| Watermark DB sync | `fast_loop` 포지션 tracker (포팅 완료) |

즉 v3 monitor 는 **표면적으로는 축소된 observability agent**:
- KIS Gateway `/balance` polling → Redis `monitoring:live_positions` 주기 갱신
- `/health` + `/metrics` (Prometheus text format, Grafana scrape)
- 모든 exit-rule 로직은 fast_loop 가 소유. monitor 는 snapshot 만.

## 구조

```
monitor/
  app.py          # FastAPI lifespan + background polling task
  poller.py       # KIS Gateway /balance → Redis 쓰기 주기 루프
  metrics.py      # Prometheus text exposition + metric collectors
```

## 환경

- `KIS_GATEWAY_URL` (default `http://kis-gateway:8080`)
- `MONITOR_POLL_INTERVAL_SEC` (default 30)
- `REDIS_*` 공통
