# `backtest/` — 백테스트 엔진

Track D 소유 (Phase 2.10).

## 책임

- 일봉 레벨 포트폴리오 시뮬레이션 (슬리피지 + 수수료 반영)
- 성과 지표 계산 (총수익, MDD, Sharpe, 전략/매도사유별)
- 결과 CSV export (`trades.csv`, `daily_snapshots.csv`)
- v2 와 동일한 `BacktestMetrics` 포맷 (Track C dashboard 안정 참조)

## 책임 밖 (Phase 3 로 이관)

- **매수/매도 전략 결정 로직**: v2 `engine.py` + `daily_strategies.py` 는 v2 설정 (Risk/Sell config) 에 강결합. v3 에선 `slow_loop/strategy/` 가 전략 엔진이므로, 백테스트 strategy plug-in 은 Phase 3 자가진화 엔진 요구 확정 후 작성
- **실시간/분봉 백테스트**: 일봉만
- **Grid Search 2,800 조합**: Phase 3 meta 영역

## v2 원본

`prime-jennie/prime_jennie/services/backtest/` (172KB, 7 파일, 1,657 줄)

## 포팅 규칙

- `domain.py` = v2 `prime_jennie.domain.enums` 에서 필요한 6종만 로컬 복사 (StrEnum, 값은 v2 와 1:1)
- `models.py` / `metrics.py` / `portfolio.py` = v2 그대로 포팅. v2 config 전역 의존(`get_config().risk` / `.sell`) 은 `BacktestConfig` 에 편입
- `data_loader.py` = v3 테이블 어댑터:
  - v2 `stock_daily_prices` → v3 `daily_prices`
  - v2 `daily_quant_scores` → v3 `legacy_quant_scores`
  - v2 `stock_masters` → v3 에 없음. sector_group 은 None (섹터 제약 비활성화)
  - v2 `daily_macro_insights` → v3 `macro_runs` 에 인사이트 필드 없음. regime 은 SIDEWAYS 고정
  - v2 `watchlist_histories` → v3 `legacy_quant_scores` 의 `is_final_selected` 필터

## 공개 API

```python
from prime_jennie_runtime.backtest import (
    BacktestConfig, BacktestMetrics,
    SimulatedPortfolio, PriceCache,
    load_prices, load_quant_scores, get_trading_dates,
    calculate_metrics, print_report, export_csv,
)
```

Strategy plug-in 은 `SimulatedPortfolio.execute_buy / execute_sell` 을 호출하는 외부 루프가 Phase 3 에서 작성.
