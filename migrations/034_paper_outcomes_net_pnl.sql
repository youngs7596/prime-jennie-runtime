-- paper_outcomes v2 (2026-10-09): 비용 차감 손익 칸 추가.
-- pnl_pct 는 비용 전 값 그대로, net_pnl_pct = pnl_pct − 왕복 비용(0.41%).
-- v1 행은 다음 측정 잡이 simulator_version 차이를 보고 다시 재서 채운다.
ALTER TABLE paper_outcomes ADD COLUMN IF NOT EXISTS net_pnl_pct numeric;
