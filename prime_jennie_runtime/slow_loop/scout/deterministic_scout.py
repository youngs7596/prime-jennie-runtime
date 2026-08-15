"""결정론 선정 오케스트레이터 — v2 scout 파이프라인의 LLM 없는 포팅.

2026-05-22 Phase 1 (a). `.ai/decisions/2026-05-22-selection-architecture-decision.md`.

v2 `scout/app.py::run_pipeline` 의 Phase 1~6 (LLM Phase 4 제외) 을 한 함수로 옮긴다:
  enrich → 섹터 모멘텀/밸류 백분위 → 가격 하한 필터 → quant 스코어 → MA 평활 →
  히스테리시스 선정 → ScreeningCandidate 매핑.

선정 경로 LLM 호출 0회 — macro gate(regime)·뉴스 감성은 상류가 만든 *데이터*로서
결정론 스코어러가 소비할 뿐이다 (v2 와 동일 구조).

LLM codegen(`code_loop.generate_validated_code`) 의 라이브 대체물. 반환은
`DeterministicScoutResult` — pipeline 이 `.scout_out` / `.result.candidates` 로 소비.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from prime_jennie_runtime.screening_executor.schemas import ScreeningResult

from .enrichment import EnrichedCandidate, enrich_universe
from .quant import (
    V2_WEIGHTS,
    QuantScore,
    _compute_rsi,
    score_candidate,
    sector_momentum_baseline,
)
from .schemas import EntryHint, ScoutContext, ScoutOutput, ScreeningCandidate
from .selection import MA_WINDOW, compute_ma_scores, select_with_hysteresis

logger = logging.getLogger(__name__)

# 스코어러 버전 — scout_runs.code_hash 안정화용 (synthetic screening_code 마커).
# 팩터 수식/가중치/임계 의미 변경 시 bump.
# @2 (2026-08-15): 섹터 모멘텀 일별 중심화. 이 버전 앞뒤로 점수 수준이 달라지므로
# 코호트 비교 시 섞지 말 것.
SCORER_VERSION = "deterministic-quant-v2-port@2"


def _apply_candidate_cap(survivors: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """후보 하드캡 적용 (validators/executor 와 동일한 상한).

    상한에 걸리면 몇 개를 버렸는지 반드시 남긴다 — 조용히 자르면 실행 기록의
    "선정 20" 이 후보 전부인 것처럼 읽힌다. 2026-08-14 아침 실측으로 통과 29 →
    선정 20 이었는데 사라진 9개가 어디에도 안 남아 있었다.
    """
    if len(survivors) <= MAX_CANDIDATES:
        return survivors
    kept = survivors[:MAX_CANDIDATES]
    logger.warning(
        "후보 하드캡: 통과 %d → 상한 %d, %d개 버림 (채택 최저 MA=%.1f, 탈락 최고 MA=%.1f)",
        len(survivors),
        MAX_CANDIDATES,
        len(survivors) - MAX_CANDIDATES,
        kept[-1][1],
        survivors[MAX_CANDIDATES][1],
    )
    return kept


def compute_code_hash(code: str) -> str:
    """결정론 스코어러 마커(screening_code)의 sha256 — scout_runs.code_hash 안정화용.

    `sha256:<64hex>` 형식. 공백/줄바꿈 정규화 없음 (같은 소스 → 같은 해시).
    2026-05-29 LLM-at-core 잔재 정리 때 code_hasher.py 에서 옮겨 왔다.
    """
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# v2 ScoutConfig 기본값 — env override.
MIN_PRICE: int = int(os.environ.get("SCOUT_MIN_PRICE", "10000"))
MAX_CANDIDATES: int = int(os.environ.get("SCOUT_MAX_CANDIDATES", "20"))
SECTOR_PCTILE_MIN_SAMPLES = 5  # 섹터 내 5종목 미만이면 백분위 미산출 (v2 동일)


@dataclass
class DeterministicScoutResult:
    """`run_deterministic_scout` 산출 — pipeline 이 `.scout_out` / `.result` 로 소비.

    LLM PipelineStepResult 가 없다 (결정론 — codegen 은퇴). scout_out 은 합성
    ScoutOutput, result 는 ScreeningResult.
    """

    scout_out: ScoutOutput
    result: ScreeningResult
    # daily_quant_scores 영속 성공 여부. False 면 pipeline 이 이벤트로 표면화한다.
    quant_persisted: bool = True


# =====================================================================
# 섹터 밸류에이션 백분위 — v2 app.py _compute_sector_valuation_pctiles 포팅
# =====================================================================


def _effective_per(candidate: EnrichedCandidate) -> float | None:
    """Forward PER 우선, 없으면 trailing PER (v2 app.py 동명 헬퍼)."""
    cons = candidate.consensus
    ft = candidate.financial_trend
    if cons and cons.forward_per is not None and cons.forward_per > 0:
        return cons.forward_per
    if ft and ft.per is not None and ft.per > 0:
        return ft.per
    return None


def _percentile_rank(value: float, sorted_values: list[float]) -> float:
    """값의 백분위 순위 (0=최저, 100=최고). 동점은 평균 순위 (v2 app.py 동명 헬퍼)."""
    n = len(sorted_values)
    if n == 0:
        return 50.0
    count_less = sum(1 for v in sorted_values if v < value)
    count_equal = sum(1 for v in sorted_values if v == value)
    return (count_less + count_equal / 2) / n * 100


def _compute_sector_pctiles(enriched: dict[str, EnrichedCandidate]) -> None:
    """섹터별 PBR/PER 분포 → 종목별 백분위 (v2 app.py _compute_sector_valuation_pctiles).

    섹터 내 유효 데이터 5종목 이상일 때만 백분위 산출. in-place 로
    candidate.sector_pbr_pctile / sector_per_pctile 채움.
    """
    sector_pbr: dict[str, list[float]] = {}
    sector_per: dict[str, list[float]] = {}

    for candidate in enriched.values():
        group = candidate.master.sector_group
        if not group:
            continue
        ft = candidate.financial_trend
        if not ft:
            continue
        if ft.pbr is not None and ft.pbr > 0:
            sector_pbr.setdefault(group, []).append(ft.pbr)
        eper = _effective_per(candidate)
        if eper is not None:
            sector_per.setdefault(group, []).append(eper)

    _min = SECTOR_PCTILE_MIN_SAMPLES
    sorted_pbr = {g: sorted(v) for g, v in sector_pbr.items() if len(v) >= _min}
    sorted_per = {g: sorted(v) for g, v in sector_per.items() if len(v) >= _min}

    for candidate in enriched.values():
        group = candidate.master.sector_group
        if not group:
            continue
        ft = candidate.financial_trend
        if not ft:
            continue
        if group in sorted_pbr and ft.pbr is not None and ft.pbr > 0:
            candidate.sector_pbr_pctile = _percentile_rank(ft.pbr, sorted_pbr[group])
        if group in sorted_per:
            eper = _effective_per(candidate)
            if eper is not None:
                candidate.sector_per_pctile = _percentile_rank(eper, sorted_per[group])

    logger.info("sector pctiles: PBR %d sectors, PER %d sectors", len(sorted_pbr), len(sorted_per))


# =====================================================================
# strategy_tag 배정 — v2 에 없던 어댑터 (v3 ScreeningCandidate 필수 필드)
# =====================================================================


def _assign_strategy_tag(candidate: EnrichedCandidate) -> str:
    """결정론 quant 후보에 v3 strategy_tag 배정.

    v2 QuantScore 에는 strategy_tag 가 없었다 (v2 는 scanner 가 장중 전략을 배정).
    v3 ScreeningCandidate 는 strategy_tag 필수 → 단순 결정론 룰로 배정한다.

    룰 근거:
      - RSI ≤ 30 (과매도): 반등 후보 → MEAN_REVERT_RSI
      - 최근 30일 forward EPS 상향(eps_revision_pct ≥ 5): 실적 모멘텀 → EARNINGS_DRIFT
      - 그 외: 다팩터 추세 복합 → SECTOR_MOMENTUM (기본값)
    GAP_UP_REBOUND 은 장중 갭 정보가 필요 — 결정론 일봉 스코어러는 배정하지 않음.
    """
    prices = candidate.daily_prices
    if len(prices) >= 15:
        rsi = _compute_rsi([p.close_price for p in prices], period=14)
        if rsi is not None and rsi <= 30:
            return "MEAN_REVERT_RSI"
    cons = candidate.consensus
    if cons is not None and cons.eps_revision_pct is not None and cons.eps_revision_pct >= 5:
        return "EARNINGS_DRIFT"
    return "SECTOR_MOMENTUM"


def _to_screening_candidate(
    candidate: EnrichedCandidate, quant: QuantScore, ma_score: float
) -> ScreeningCandidate:
    """QuantScore + MA 점수 → v3 ScreeningCandidate.

    conviction = MA 점수 / 100 (0-1 클램프). entry_hint=market (v2 watchlist 는
    진입 힌트가 없었고 scanner 가 장중 진입 — v3 는 engine 정책 default 가 안전망).
    exit_hint=None → engine 이 strategy_tag 별 정책 default 적용.
    """
    conviction = max(0.0, min(1.0, ma_score / 100.0))
    return ScreeningCandidate(
        ticker=candidate.master.stock_code,
        strategy_tag=_assign_strategy_tag(candidate),
        conviction=conviction,
        entry_hint=EntryHint(trigger="market"),
        exit_hint=None,
        factors={
            "momentum": quant.momentum_score,
            "quality": quant.quality_score,
            "value": quant.value_score,
            "technical": quant.technical_score,
            "news": quant.news_score,
            "supply_demand": quant.supply_demand_score,
            "sector_momentum": quant.sector_momentum_score,
            "quant_total": quant.total_score,
            "ma_score": ma_score,
        },
        notes=f"deterministic quant total={quant.total_score:.1f} ma={ma_score:.1f}",
        thesis_spec=None,
    )


# =====================================================================
# 점수 이력 — daily_quant_scores read/write (MA 평활 + 히스테리시스 입력)
# =====================================================================


async def _load_score_history(
    engine: AsyncEngine | None,
    *,
    as_of_dt: datetime,
    window: int,
) -> tuple[dict[str, list[float]], set[str] | None]:
    """직전 scout run 들의 quant 점수 이력 + 직전 선정 종목 조회.

    v2 는 lookback_dates 였으나 v3 는 7회/일 cadence 라 run 단위가 자연스럽다 —
    직전 (window-1) 개 run 의 total_quant_score 를 MA 입력으로, 가장 최근 run 의
    is_final_selected 종목을 히스테리시스 previous_codes 로 쓴다.

    Returns:
        (historical, previous_codes). historical={code:[과거점수 오래된→최신]}.
        previous_codes=직전 run 선정 종목 (없으면 None — cold start).
    """
    if engine is None:
        return {}, None
    try:
        async with engine.connect() as conn:
            # 직전 (window-1) run — scout_runs.generated_at 기준.
            # macro 게이트가 닫힌 날(shadow run)은 점수·후보를 분석용으로 영속하되
            # 매매 경로 입력에서는 제외한다 — MA 평활·히스테리시스 incumbency 가
            # "실제 매매 안 한 가짜 선정"을 물려받지 않도록(2026-06-19). 매크로 영향
            # 자체는 각 run 의 점수(is_bull·섹터 모멘텀)에 이미 반영돼 있으므로,
            # 여기서 거르는 건 닫힌 날의 *지속성 전파*일 뿐 매크로 반영이 아니다.
            # context_snapshot_json 의 macro_gate 로 판별(없는 옛 행은 open 간주 =
            # 종전 동작 유지).
            # 스코어러 버전이 다른 run 도 뺀다 — 점수 눈금이 달라진 뒤에도 옛 점수를
            # 평균에 섞으면 며칠간 엉뚱한 MA 가 나온다. 2026-08-15 섹터 모멘텀
            # 중심화처럼 수준이 통째로 옮겨가는 변경이 있으면 특히 그렇다. 같은
            # 버전 이력이 없으면 cold start 로 떨어져 그 run 만 임계로 거른다.
            run_rows = await conn.execute(
                text(
                    "SELECT scout_run_id FROM scout_runs "
                    "WHERE generated_at < :now "
                    "AND COALESCE(context_snapshot_json->>'macro_gate', 'open') = 'open' "
                    "AND prompt_version = :scorer_version "
                    "ORDER BY generated_at DESC LIMIT :n"
                ),
                {
                    "now": as_of_dt,
                    "n": max(0, window - 1),
                    "scorer_version": SCORER_VERSION,
                },
            )
            run_ids = [r[0] for r in run_rows.fetchall()]  # 최신→오래된
            if not run_ids:
                return {}, None

            score_rows = await conn.execute(
                text(
                    "SELECT dqs.run_id, dqs.stock_code, dqs.total_quant_score, "
                    "       dqs.is_final_selected, sr.generated_at "
                    "FROM daily_quant_scores dqs "
                    "JOIN scout_runs sr ON sr.scout_run_id = dqs.run_id "
                    "WHERE dqs.run_id = ANY(:ids) AND dqs.is_active "
                    "ORDER BY sr.generated_at"
                ),
                {"ids": run_ids},
            )
            rows = score_rows.mappings().all()
    except Exception:
        logger.exception("_load_score_history 실패 — cold start 로 진행 (fail-open)")
        return {}, None

    historical: dict[str, list[float]] = {}
    for row in rows:  # generated_at 오름차순 → 오래된→최신
        historical.setdefault(row["stock_code"], []).append(float(row["total_quant_score"]))

    # 가장 최근 run 의 선정 종목 = previous_codes
    latest_run_id = run_ids[0]
    previous_codes = {
        row["stock_code"]
        for row in rows
        if row["run_id"] == latest_run_id and row["is_final_selected"]
    }
    return historical, (previous_codes or None)


async def _persist_quant_scores(
    engine: AsyncEngine | None,
    *,
    run_id: str,
    as_of: date,
    quant_scores: dict[str, QuantScore],
    selected_codes: set[str],
) -> bool:
    """전 종목 quant 서브점수를 daily_quant_scores 에 upsert.

    MA 평활의 다음 run 입력 + weekly_factor_analysis(daily_quant_scores 의존)
    의 IC 분석 입력. 7팩터 서브점수 + total 을 저장한다 — sector_momentum_score
    는 migration 025 로 컬럼이 생겨 2026-06-18 부터 같이 적재한다(그 전 행은 NULL).

    반환값: 영속 성공 또는 스킵(엔진 없음/빈 점수)이면 True, INSERT 가 예외로
    실패하면 False. 매매를 막지 않으려고 예외는 여기서 삼키되, False 를 위로 올려
    pipeline 이 event_log 에 표면화한다 — 컨테이너 로그만으론 6일씩 숨었던 전례
    (2026-06-29 시퀀스 desync 사고) 재발 방지.
    """
    if engine is None or not quant_scores:
        return True
    sql = text(
        """
        INSERT INTO daily_quant_scores (
            score_date, stock_code, stock_name, total_quant_score,
            momentum_score, quality_score, value_score, technical_score,
            news_score, supply_demand_score, sector_momentum_score,
            is_tradable, is_final_selected, run_id, is_active
        ) VALUES (
            :score_date, :stock_code, :stock_name, :total,
            :momentum, :quality, :value, :technical,
            :news, :supply_demand, :sector_momentum,
            TRUE, :selected, :run_id, TRUE
        )
        ON CONFLICT (score_date, stock_code, run_id) DO UPDATE SET
            stock_name = EXCLUDED.stock_name,
            total_quant_score = EXCLUDED.total_quant_score,
            momentum_score = EXCLUDED.momentum_score,
            quality_score = EXCLUDED.quality_score,
            value_score = EXCLUDED.value_score,
            technical_score = EXCLUDED.technical_score,
            news_score = EXCLUDED.news_score,
            supply_demand_score = EXCLUDED.supply_demand_score,
            sector_momentum_score = EXCLUDED.sector_momentum_score,
            is_final_selected = EXCLUDED.is_final_selected
        """
    )
    params = [
        {
            "score_date": as_of,
            "stock_code": code,
            "stock_name": qs.stock_name,
            "total": qs.total_score,
            "momentum": qs.momentum_score,
            "quality": qs.quality_score,
            "value": qs.value_score,
            "technical": qs.technical_score,
            "news": qs.news_score,
            "supply_demand": qs.supply_demand_score,
            "sector_momentum": qs.sector_momentum_score,
            "selected": code in selected_codes,
            "run_id": run_id,
        }
        for code, qs in quant_scores.items()
    ]
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, params)
        logger.info("daily_quant_scores upsert: %d rows (run_id=%s)", len(params), run_id)
        return True
    except Exception:
        logger.exception("_persist_quant_scores 실패 (run_id=%s) — MA 이력 누락 가능", run_id)
        return False


# =====================================================================
# 메인 — run_deterministic_scout
# =====================================================================


async def run_deterministic_scout(
    *,
    db_engine: AsyncEngine | None,
    scout_ctx: ScoutContext,
    as_of: date,
    as_of_dt: datetime,
    scout_run_id: str,
) -> DeterministicScoutResult:
    """v2 결정론 선정 코어 — universe → 후보 list[ScreeningCandidate].

    Args:
        db_engine: v3 postgres AsyncEngine.
        scout_ctx: universe / sector_momentum / macro_state 등 (ScoutContextBuilder 산출).
        as_of: 기준 거래일.
        as_of_dt: 기준 시각 (점수 이력 조회 컷오프).
        scout_run_id: 이번 run 식별자.

    Returns:
        DeterministicScoutResult.
    """
    t0 = time.monotonic()
    universe = list(scout_ctx.universe)

    if db_engine is None or not universe:
        logger.warning("run_deterministic_scout: db_engine/universe 없음 — 빈 결과")
        return DeterministicScoutResult(
            scout_out=_empty_scout_out(),
            result=ScreeningResult(ok=True, candidates=[]),
        )

    # --- Phase 2: enrichment ---
    enriched = await enrich_universe(db_engine, universe, as_of=as_of)
    if not enriched:
        logger.warning("run_deterministic_scout: enrichment 0건")
        return DeterministicScoutResult(
            scout_out=_empty_scout_out(),
            result=ScreeningResult(ok=True, candidates=[]),
        )

    # --- Phase 2.5: 섹터 모멘텀(상류 macro feeder 산출) 주입 ---
    # scout_ctx.sector_momentum 은 decimal (0.05=+5%). v2 _sector_momentum_score 는
    # percent(-5~15) 를 기대 → ×100 변환.
    for candidate in enriched.values():
        group = candidate.master.sector_group
        if group and group in scout_ctx.sector_momentum:
            candidate.sector_avg_return_20d = scout_ctx.sector_momentum[group] * 100.0

    # --- Phase 2.5b: 섹터 PBR/PER 백분위 ---
    _compute_sector_pctiles(enriched)

    # --- Phase 2.9: 현재가 하한 필터 ---
    before = len(enriched)
    enriched = {
        code: c
        for code, c in enriched.items()
        if c.snapshot is not None and c.snapshot.price >= MIN_PRICE
    }
    if len(enriched) != before:
        logger.info("price filter: %d → %d (min=%d)", before, len(enriched), MIN_PRICE)

    # --- Phase 3: quant 스코어링 ---
    # is_bull: v3 macro 는 5단계 regime 이 없음 — size_multiplier 1.0(full risk-on)
    # 을 강세장 대용으로 환산 (RSI 70-80 페널티 면제 조건).
    is_bull = scout_ctx.macro_state.size_multiplier >= 1.0
    # 섹터 모멘텀은 절대 수익률이라 시장이 통째로 오르면 전 종목 점수를 같이 밀어
    # 올린다 (8-05→8-14 평균 0.68→6.31, 임계 통과 4→33 종목). 그날 채점 대상의
    # 평균을 기준선으로 빼서 섹터 간 우열만 남긴다.
    sector_baseline = sector_momentum_baseline(enriched.values())
    if sector_baseline is not None:
        logger.info("섹터 모멘텀 중심화: 기준선 %.2f (%d종목)", sector_baseline, len(enriched))
    else:
        logger.info("섹터 모멘텀 중심화 생략 — 섹터 데이터 표본 부족")
    quant_scores: dict[str, QuantScore] = {}
    for code, candidate in enriched.items():
        quant_scores[code] = score_candidate(
            candidate, is_bull=is_bull, sector_baseline=sector_baseline
        )

    # --- Phase 4.5: MA 평활 ---
    historical, previous_codes = await _load_score_history(
        db_engine, as_of_dt=as_of_dt, window=MA_WINDOW
    )
    current_scores = {code: qs.total_score for code, qs in quant_scores.items()}
    ma_scores = compute_ma_scores(current_scores, historical, window=MA_WINDOW)

    # --- Phase 6: 히스테리시스 선정 ---
    survivors = select_with_hysteresis(ma_scores, previous_codes=previous_codes)
    passed = len(survivors)
    survivors = _apply_candidate_cap(survivors)
    selected_codes = {code for code, _ in survivors}

    candidates = [
        _to_screening_candidate(enriched[code], quant_scores[code], ma)
        for code, ma in survivors
        if code in enriched
    ]

    # --- 점수 이력 영속 (다음 run MA 입력 + factor_analysis 재가동) ---
    quant_persisted = await _persist_quant_scores(
        db_engine,
        run_id=scout_run_id,
        as_of=as_of,
        quant_scores=quant_scores,
        selected_codes=selected_codes,
    )

    elapsed = time.monotonic() - t0
    logger.info(
        "run_deterministic_scout: universe=%d enriched=%d scored=%d passed=%d selected=%d (%.1fs)",
        len(universe),
        before,
        len(quant_scores),
        passed,
        len(candidates),
        elapsed,
    )

    scout_out = ScoutOutput(
        screening_code=f"# {SCORER_VERSION}\n",
        hypothesis=(
            f"결정론 quant v2 포팅: universe {len(universe)} → 채점 {len(quant_scores)} "
            f"→ 통과 {passed} → 선정 {len(candidates)} "
            f"(MA{MA_WINDOW}+히스테리시스, is_bull={is_bull}, "
            f"섹터중심화={sector_baseline is not None})"
        )[:200],
        expected_candidates=min(len(candidates), 20),
        factor_weights={k: float(v) for k, v in V2_WEIGHTS.items()},
        strategy_tags_used=sorted({c.strategy_tag for c in candidates}),
        fallback_strategy="skip_today",
        estimated_runtime_seconds=round(elapsed, 1),
    )
    return DeterministicScoutResult(
        scout_out=scout_out,
        result=ScreeningResult(ok=True, candidates=candidates, exec_time_ms=int(elapsed * 1000)),
        quant_persisted=quant_persisted,
    )


def _empty_scout_out() -> ScoutOutput:
    """후보 0 — 합성 ScoutOutput."""
    return ScoutOutput(
        screening_code=f"# {SCORER_VERSION}\n",
        hypothesis="결정론 quant: universe/enrichment 데이터 없음 — 후보 0",
        expected_candidates=0,
        factor_weights={k: float(v) for k, v in V2_WEIGHTS.items()},
        strategy_tags_used=[],
        fallback_strategy="skip_today",
    )


__all__ = [
    "SCORER_VERSION",
    "DeterministicScoutResult",
    "run_deterministic_scout",
]
