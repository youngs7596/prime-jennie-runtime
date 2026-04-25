"""(removed) v2 거래 로그 기반 성과 분석/피드백 잡.

migration 016 으로 `legacy_trade_logs` 가 drop 되면서 `analyze_ai_performance`
및 `analyst_feedback` 도 제거됨. v3 PnL 산출은 `outcomes` (sheet-level closed
PnL) 가 담당하므로 v2 trade_logs 기반 sell_reason × regime × score_bin 집계는
의미가 없다.

대체 경로 (필요 시 구현):
- 성과 집계: outcomes 테이블 기반 새 잡 (Track B 후속).
- 분석가 피드백: scout_runs / outcomes 조합 기반 LLM 리포트.
"""

from __future__ import annotations

__all__: list[str] = []
