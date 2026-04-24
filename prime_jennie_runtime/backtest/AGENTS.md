# `backtest/` — 백테스트 엔진 (v3 전용)

Phase 3 자가진화의 전제 도구. meta 가 생성한 PR (Scout 프롬프트/코드 변경, exit
rule 파라미터 조정 등) 이 과거 데이터에서 어떤 성과를 냈을지 재현한다.

## 핵심 원칙 — fast_loop 재사용

운영 중인 `fast_loop.exit_evaluator.evaluate()` 를 **그대로** 호출한다. v2 처럼
별도 `SimulatedPortfolio` 를 짜면 exit rule 로직이 두 곳 (prod / backtest) 에
생겨 파라미터 변경 때마다 drift 위험. v3 는 단일 엔진.

## 구조

- `domain.py` — `DailyBar`, `Trade`, `SheetBacktestResult`, `BacktestConfig`
- `runner.py` — `simulate_sheet(sheet, bars, ...)`. 하루를 open/high/low/close
  4 tick 으로 분해해서 evaluator 에 먹인다. death_cross 는 close tick 에서만
  daily_closes 전달 (causally correct)
- `metrics.py` — `summarize(results)` + `format_report(summary)`. 시트 단위
  분포만. 포트폴리오 MDD 는 시트가 중첩될 수 있어 v1 에선 계산 안 함
- `data_loader.py` — v3 `daily_prices` → `DailyBar` 로더 1 개. v2 adapters
  (quant_scores / macro_days / watchlists) 전부 제거

## 책임 밖

- **grid search 2,800 조합** — Phase 3 meta 레이어가 담당. runner 를 N 번 호출
- **portfolio equity curve** — 시트 중첩 처리는 driver (scripts/) 책임
- **실시간/분봉 백테스트** — 일봉만. `overextension_exit` 은 `rsi_1m=None` 이면
  자연스럽게 건너뛴다
- **`SpreadUnderBpsCondition`** — OHLCV 만으로 평가 불가. 체결 가정하고 스킵

## 일봉 tick 분해 주의

운영 환경은 실시간 tick stream 이지만 백테스트는 일봉 OHLCV 만 있다. 하루를
open → high → low → close 순서로 4 tick 을 emit 하는데, 실제 분봉 순서는 알 수
없으므로 **pessimistic** — 고점 activate 후 저점 trigger 가 같은 날 가능하다.
이건 의도적 보수 가정.

## 공개 API

```python
from prime_jennie_runtime.backtest import (
    BacktestConfig, DailyBar, SheetBacktestResult, Trade,
    BacktestSummary, ReasonStats,
    simulate_sheet, summarize, format_report,
    load_daily_bars,
)
```
