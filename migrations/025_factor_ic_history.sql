-- 025_factor_ic_history.sql
-- 팩터 예측력(IC) 분석 보강 — 2026-06-18.
--
-- 배경: slow_loop 선별의 paper 성과가 절대·상대 모두 마이너스라, "어떤 신호가
-- 앞으로의 수익을 실제로 예측하는가" 를 유니버스 전체에서 재서 선별을 고치려 한다.
-- 필요한 데이터(종목별·시점별 팩터 점수 = daily_quant_scores, forward 수익 =
-- daily_prices)는 이미 쌓이고 있어, 분석 층의 두 공백만 메운다.
--
-- 1) daily_quant_scores 에 sector_momentum_score 컬럼 추가.
--    7팩터 중 이 하나만 스냅샷에 안 들어가 historical IC 분석에서 빠져 있었다.
--    과거 행은 NULL 로 남고(되돌려 채울 원천 없음), 배포 이후 run 부터 채워진다.
-- 2) factor_ic_history — 주간 IC 결과를 영속. 기존엔 Redis 7일 캐시뿐이라
--    팩터 예측력의 시간 변화를 추적할 수 없었다. scope(universe/selected) ×
--    horizon(T+5/T+10) × factor 한 줄씩 쌓아 선별 개선의 ground truth 로 쓴다.

ALTER TABLE daily_quant_scores
    ADD COLUMN IF NOT EXISTS sector_momentum_score DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS factor_ic_history (
    id            BIGSERIAL PRIMARY KEY,
    analysis_date DATE        NOT NULL,        -- 분석 실행일
    period_days   INTEGER     NOT NULL,        -- lookback 윈도우 (기본 30일)
    horizon_days  INTEGER     NOT NULL,        -- forward 수익 구간 (5, 10)
    scope         VARCHAR(16) NOT NULL,        -- 'universe' | 'selected'
    factor_name   VARCHAR(40) NOT NULL,        -- total_quant_score / momentum_score / ...
    ic_spearman   DOUBLE PRECISION,            -- Spearman rank correlation (팩터 vs fwd return)
    sample_count  INTEGER     NOT NULL,        -- 해당 IC 계산에 쓰인 (종목,시점) 표본 수
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_factor_ic UNIQUE (analysis_date, period_days, horizon_days, scope, factor_name)
);
CREATE INDEX IF NOT EXISTS ix_factor_ic_factor_date
    ON factor_ic_history (factor_name, analysis_date);
