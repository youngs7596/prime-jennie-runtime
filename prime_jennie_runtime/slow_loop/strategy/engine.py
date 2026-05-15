"""Strategy Engine — ScreeningCandidate를 PositionSheet로 조립.

결정론 전수. LLM 사용 금지 (Phase 0 design §1.1).
Scout candidates × Macro state × RiskThrottle snapshot → PositionSheet | None.

None이 반환되는 경우:
- Macro gate == "closed"
- final_pct < MIN_POSITION_PCT
- strategy_tag 정책 미존재 (폐기된 RSI_REBOUND 등)
- 같은 ticker 오늘 활성 시트 이미 존재 (중복 방지, T10)

price_above 자동 부착 (2026-05-11):
- GAP_UP_REBOUND 는 시초 갭상승 후 직전 봉 high 돌파 확인 후 진입이 안전.
- scout 가 `price_hint` 를 채워주면, scout 의 conditions_hint 가 price_above 를
  포함하지 않는 한 engine 이 자동으로 `price_above price_hint × 1.001` 조건을
  부착해 EntryConditionEvaluator (fast_loop/pending_entry) 가 돌파 확인 후
  실 매수를 트리거하게 한다.
- scout 가 price_hint 미설정 / 정책 yaml 의 default_entry_conditions 에 명시한
  경우엔 본 자동부착이 안 일어남 — scout 의도 우선.
- 추후 Scout 가 price_above 를 conditions_hint 로 정확히 박아주면 본 자동부착
  로직은 제거 검토 (Scout F agent 의 별도 작업).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from prime_jennie_runtime.position_sheet.schema import (
    ALLOWED_STRATEGY_TAGS,
    DEPRECATED_STRATEGY_TAGS,
    KST,
    MIN_POSITION_PCT,
    EntrySection,
    ExitSection,
    MacroStateSnapshot,
    PositionSheet,
    ProvenanceSection,
    SizeSection,
)

from ..scout.schemas import ScreeningCandidate
from .policy import StrategyPolicy
from .risk_throttle import RiskThrottleSnapshot
from .sheet_id import generate_sheet_id

logger = logging.getLogger(__name__)


# GAP_UP_REBOUND price_above 자동 부착 시 사용하는 breakout 배수.
# +10bps (1.001) — scout price_hint 위 호가 살짝 돌파 확인 임계. v2 buy-scanner
# 의 "직전 봉 high + 0.1%" 휴리스틱과 동등. 운영 데이터 누적 후 조정 검토.
PRICE_ABOVE_BREAKOUT_MULT: float = 1.001


# exit rules 권장 순서 (POSITION_SHEET_SPEC §5.3)
_EXIT_RULE_ORDER: dict[str, int] = {
    "overextension_exit": 0,
    "profit_floor": 1,
    "trailing_tp": 2,
    "fixed_tp": 3,
    "scale_out": 4,
    "breakeven": 5,
    "death_cross": 6,
    "fixed_sl": 7,
    "time_stop": 8,
}


def _sort_exit_rules(rules: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """POSITION_SHEET_SPEC §5.3 권장 순서로 정렬. first_match 평가 순."""
    # 안정 정렬 — 같은 type이 여러 개라도 입력 순서 유지
    return sorted(rules, key=lambda r: _EXIT_RULE_ORDER.get(r["type"], 99))


def _normalize_scout_exit_hint(
    raw_rules: list[dict[str, Any]],
    default_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Scout LLM exit_hint 정규화 + 필수 룰 보충.

    LLM 이 schema 와 다른 별칭/형식으로 룰을 만들어내도 흡수하기 위한 방어 레이어:
    - ``trailing_stop`` → ``trailing_tp`` 별칭 변환 (pct=drop_pct 가정, activate_pct
      기본 0.05). v0.3 prompt 결함으로 LLM 이 학습한 옛 형식.
    - ``time_stop`` 의 ``days`` → ``mode=hold_days, value=N`` 정정.
    - ``stop_loss`` → ``fixed_sl`` 별칭.
    - ``take_profit`` → ``fixed_tp`` 별칭.
    - 결과에 ``fixed_sl`` / ``time_stop`` 중 하나라도 없으면 default_rules 에서
      해당 type 을 가져와 보충 (PositionSheet model_validator 강제 요구).
    - 알 수 없는 type 은 제외 (silently drop — schema 가 어차피 거절하므로 사전 차단).

    근본 fix 는 prompts.py 의 v0.4 가이드. 본 함수는 LLM 변동성 흡수용.
    """
    known_types = set(_EXIT_RULE_ORDER.keys())
    alias_map: dict[str, str] = {
        "trailing_stop": "trailing_tp",
        "stop_loss": "fixed_sl",
        "take_profit": "fixed_tp",
    }

    normalized: list[dict[str, Any]] = []
    for rule in raw_rules:
        if not isinstance(rule, dict) or "type" not in rule:
            continue
        rule = dict(rule)  # 입력 dict 비파괴
        rtype = rule["type"]

        if rtype in alias_map:
            new_type = alias_map[rtype]
            if new_type == "trailing_tp":
                # 옛 형식 {pct: X} → {activate_pct: 0.05 default, drop_pct: X}
                pct = rule.pop("pct", None)
                rule.setdefault("activate_pct", 0.05)
                if pct is not None:
                    rule["drop_pct"] = pct
                rule.setdefault("drop_pct", 0.03)
            rule["type"] = new_type
            rtype = new_type

        if rtype == "time_stop":
            # 옛 형식 {days: N} → {mode: hold_days, value: N}
            days = rule.pop("days", None)
            if "mode" not in rule:
                rule["mode"] = "hold_days" if days is not None else "eod"
            if days is not None and "value" not in rule:
                rule["value"] = days

        if rtype not in known_types:
            continue  # schema 가 거절할 type 사전 제외
        normalized.append(rule)

    # 필수 룰 보충: fixed_sl + time_stop + trailing_tp 중 빠진 게 있으면 default 에서 가져옴.
    # trailing_tp 강제 부착 (2026-05-15) — scout 가 exit_hint 로 trailing 제외하면
    # 익절 trigger 가 fixed_sl 외 0 인 sheet 가 발행되어 변동성에 취약. 본 주
    # 운영 데이터 (KB금융/HD현대중공업/SK스퀘어 EARNINGS_DRIFT 익절 0건) 에서
    # 정량 확인된 패턴.
    have_types = {r["type"] for r in normalized}
    for required in ("fixed_sl", "time_stop", "trailing_tp"):
        if required in have_types:
            continue
        for d in default_rules:
            if d.get("type") == required:
                normalized.append(dict(d))
                break

    return normalized


def _normalize_scout_entry_hint(
    trigger: str, price_hint: float | None, *, ticker: str = ""
) -> tuple[str, float | None]:
    """Scout LLM entry_hint 정규화 — limit + null/0 price 패턴을 market 으로 변환.

    PositionSheet schema 는 ``trigger=="limit"`` 일 때 ``price > 0`` 강제 (schema.py:283).
    그러나 LLM 이 limit 으로 가이드 후 price_hint 를 비워두는 케이스가 발생
    (2026-05-13 MEAN_REVERT_RSI 2건 사고). 이때 sheet 거절되어 매수 손실.

    정규화 정책: limit + (price_hint is None or <= 0) → market 으로 변환 + WARN 로깅.
    가장 보수적 fallback (market 은 schema 도 항상 통과, fast_loop 가 안전하게 처리).

    근본 fix 는 prompts.py 의 entry_hint 가이드 보강. 본 함수는 LLM 변동성 흡수용.
    """
    if trigger == "limit" and (price_hint is None or price_hint <= 0):
        logger.warning(
            "scout entry_hint limit+null price → market fallback (ticker=%s)", ticker or "?"
        )
        return "market", None
    return trigger, price_hint


# =====================================================================
# 중복 체크 Protocol (Track A migrations의 position_sheets 테이블 조회)
# =====================================================================


@runtime_checkable
class ActiveSheetChecker(Protocol):
    """같은 날 같은 ticker 시트 + 24h 내 손절 이력 검사.

    audit B1 / B2 / C fix (2026-05-15): default 가 NullActiveSheetChecker 라
    실 enforcement 가 없던 dead code. PgActiveSheetChecker (DB 기반) 가
    실제 구현. fail-open 정책 — DB 장애 시 False 반환 (sheet 발행 진행).
    """

    async def has_active_sheet_today(self, ticker: str, as_of_date: datetime) -> bool: ...

    async def has_recent_stop_loss(self, ticker: str) -> bool: ...


class NullActiveSheetChecker:
    """항상 False 반환 — fallback / 테스트용. 운영에서는 PgActiveSheetChecker 사용."""

    async def has_active_sheet_today(self, ticker: str, as_of_date: datetime) -> bool:
        return False

    async def has_recent_stop_loss(self, ticker: str) -> bool:
        return False


# 24h 내 stop sell 검사 reason — fast_loop/cooldown_check.py 와 동일 contract.
_STOP_REASONS = ("fixed_sl", "stop_loss", "breakeven_stop")

_SQL_HAS_ACTIVE_TODAY = (
    "SELECT EXISTS (SELECT 1 FROM position_sheets "
    "WHERE ticker = $1 "
    "  AND (generated_at AT TIME ZONE 'Asia/Seoul')::date = "
    "      ($2::timestamptz AT TIME ZONE 'Asia/Seoul')::date)"
)

_SQL_HAS_RECENT_STOP = (
    "SELECT EXISTS ("
    "  SELECT 1 FROM executions e "
    "  JOIN position_sheets ps USING (sheet_id) "
    "  WHERE ps.ticker = $1 "
    "    AND e.side = 'sell' "
    "    AND (e.metadata_json->>'reason') = ANY($2) "
    "    AND e.executed_at > now() - interval '24 hours'"
    ")"
)


class PgActiveSheetChecker:
    """SQLAlchemy AsyncEngine 기반 — 운영 구현체.

    slow_loop 의 db_engine (sqlalchemy.ext.asyncio.AsyncEngine) 을 그대로 사용해
    feeder/screening_executor 와 일관성 유지. fail-open 정책 — DB 장애 시 False
    반환 (sheet 발행 진행, 매매 path 보호).

    Parameters
    ----------
    engine:
        SQLAlchemy AsyncEngine.
    stop_reasons:
        cooldown trigger 가 되는 executions.metadata_json->>'reason' 목록.
        기본 fast_loop/cooldown_check.py 와 동일.
    """

    def __init__(self, engine, *, stop_reasons=_STOP_REASONS) -> None:
        self._engine = engine
        self._stop_reasons = list(stop_reasons)

    async def has_active_sheet_today(self, ticker: str, as_of_date: datetime) -> bool:
        from sqlalchemy import text

        sql = text(
            "SELECT EXISTS (SELECT 1 FROM position_sheets "
            "WHERE ticker = :ticker "
            "  AND (generated_at AT TIME ZONE 'Asia/Seoul')::date = "
            "      (:as_of_date AT TIME ZONE 'Asia/Seoul')::date)"
        )
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(sql, {"ticker": ticker, "as_of_date": as_of_date})
                return bool(result.scalar())
        except Exception:
            logger.exception(
                "has_active_sheet_today failed ticker=%s — fail-open (return False)", ticker
            )
            return False

    async def has_recent_stop_loss(self, ticker: str) -> bool:
        from sqlalchemy import text

        # ANY() with text array binding: cast list to text[] via SQLAlchemy.
        sql = text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM executions e "
            "  JOIN position_sheets ps USING (sheet_id) "
            "  WHERE ps.ticker = :ticker "
            "    AND e.side = 'sell' "
            "    AND (e.metadata_json->>'reason') = ANY(:reasons) "
            "    AND e.executed_at > now() - interval '24 hours'"
            ")"
        )
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(sql, {"ticker": ticker, "reasons": self._stop_reasons})
                return bool(result.scalar())
        except Exception:
            logger.exception(
                "has_recent_stop_loss failed ticker=%s — fail-open (return False)", ticker
            )
            return False


# =====================================================================
# Strategy Engine
# =====================================================================


@dataclass(frozen=True)
class StrategyEngineInputs:
    """build_sheet 호출 시 candidate 외 불변 입력 묶음."""

    macro_state: MacroStateSnapshot
    scout_run_id: str
    scout_code_hash: str
    scout_hypothesis: str
    generated_at: datetime
    news_score: float | None = None


class StrategyEngine:
    """결정론 시트 조립기. LLM 사용 금지."""

    def __init__(
        self,
        policy: StrategyPolicy,
        risk_throttle: RiskThrottleSnapshot,
        active_checker: ActiveSheetChecker | None = None,
        generated_by: str = "prime-jennie-runtime@v3.0.1",
    ):
        self._policy = policy
        self._risk = risk_throttle
        self._active_checker = active_checker or NullActiveSheetChecker()
        self._generated_by = generated_by

    async def build_sheet(
        self,
        candidate: ScreeningCandidate,
        inputs: StrategyEngineInputs,
    ) -> PositionSheet | None:
        """단일 candidate에서 시트를 조립. 거부 사유가 있으면 None.

        거부 사유는 logger.info로 남긴다 (테스트가 검증). 구조화된 사유가
        필요하면 build_sheet_with_reason 사용.
        """
        sheet, _ = await self.build_sheet_with_reason(candidate, inputs)
        return sheet

    async def build_sheet_with_reason(
        self,
        candidate: ScreeningCandidate,
        inputs: StrategyEngineInputs,
    ) -> tuple[PositionSheet | None, str | None]:
        """build_sheet 의 상세 버전 — (sheet, rejection_reason) 반환.

        rejection_reason 은 screening_candidates.rejection_reason 컬럼에 직접
        저장되는 코드: deprecated_tag | unknown_tag | no_policy | macro_closed |
        duplicate_today | size_below_min. 시트가 성공적으로 생성되면 None.
        """
        tag = candidate.strategy_tag

        # 1. strategy_tag 유효성
        if tag in DEPRECATED_STRATEGY_TAGS:
            logger.info("sheet_rejected: deprecated tag ticker=%s tag=%s", candidate.ticker, tag)
            return None, "deprecated_tag"
        if tag not in ALLOWED_STRATEGY_TAGS:
            logger.info("sheet_rejected: unknown tag ticker=%s tag=%s", candidate.ticker, tag)
            return None, "unknown_tag"
        if not self._policy.has(tag):
            logger.info("sheet_rejected: no policy ticker=%s tag=%s", candidate.ticker, tag)
            return None, "no_policy"

        # 2. Macro gate == "closed"면 발행 안 함
        if inputs.macro_state.gate == "closed":
            logger.info(
                "sheet_rejected: macro closed ticker=%s gate=%s",
                candidate.ticker,
                inputs.macro_state.gate,
            )
            return None, "macro_closed"

        # 3. 중복 체크 (같은 날 같은 ticker 이미 활성) — audit B2
        if await self._active_checker.has_active_sheet_today(candidate.ticker, inputs.generated_at):
            logger.info(
                "sheet_rejected: duplicate ticker=%s date=%s",
                candidate.ticker,
                inputs.generated_at.date(),
            )
            return None, "duplicate_today"

        # 3b. 24h 내 손절 이력 — audit C (recent stop-loss cooldown).
        # fast_loop cooldown 가드와 동일 logic 을 sheet 발행 단계에서도 검사 —
        # 발행 자체를 차단하면 후행 path (fast_loop, KIS) 도 안 거침.
        if await self._active_checker.has_recent_stop_loss(candidate.ticker):
            logger.info(
                "sheet_rejected: recent_stoploss_cooldown ticker=%s",
                candidate.ticker,
            )
            return None, "recent_stoploss_cooldown"

        # 4. size 계산
        entry = self._policy.get(tag)
        macro_mult = inputs.macro_state.size_multiplier
        risk_mult = self._risk.current_multiplier()
        final_pct = entry.base_pct * macro_mult * risk_mult

        if final_pct < MIN_POSITION_PCT:
            logger.info(
                "sheet_rejected: final_pct below min ticker=%s final=%.6f",
                candidate.ticker,
                final_pct,
            )
            return None, "size_below_min"

        size = SizeSection(
            base_pct=entry.base_pct,
            macro_multiplier=macro_mult,
            risk_multiplier=risk_mult,
            final_pct=final_pct,
            max_notional_krw=entry.max_notional_krw,
            max_notional_pct=entry.max_notional_pct,
        )

        # 5. entry 조립 — Scout entry_hint 반영. conditions_hint 가 비어있으면
        # 정책의 default_entry_conditions 로 보강 (호가 안전장치 등).
        # entry trigger/price 정규화 — limit + null price 같은 LLM 결함 흡수.
        entry_trigger, entry_price = _normalize_scout_entry_hint(
            candidate.entry_hint.trigger,
            candidate.entry_hint.price_hint,
            ticker=candidate.ticker,
        )
        entry_valid_until = _entry_valid_until(inputs.generated_at)
        scout_conditions = list(candidate.entry_hint.conditions_hint or [])
        if not scout_conditions and entry.default_entry_conditions:
            scout_conditions = [dict(c) for c in entry.default_entry_conditions]
        # GAP_UP_REBOUND price_above 자동 부착 — scout 가 price_hint 를 주고
        # conditions_hint 에 price_above 가 없으면, price_hint × 1.001 임계로
        # breakout 확인. PRICE_ABOVE_BREAKOUT_MULT 는 정적 fallback (1.001 = +10bps).
        if (
            tag == "GAP_UP_REBOUND"
            and entry_price is not None
            and entry_price > 0
            and not any(c.get("type") == "price_above" for c in scout_conditions)
        ):
            scout_conditions.append(
                {
                    "type": "price_above",
                    "value": float(entry_price) * PRICE_ABOVE_BREAKOUT_MULT,
                }
            )
        entry_section = EntrySection(
            trigger=entry_trigger,
            price=entry_price,
            valid_until=entry_valid_until,
            conditions=scout_conditions,  # type: ignore[arg-type]
        )

        # 6. exit 조립 — Scout exit_hint 있으면 정규화 후 사용, 없으면 policy 기본값
        if candidate.exit_hint is not None:
            raw_rules = _normalize_scout_exit_hint(
                list(candidate.exit_hint.rules_hint),
                [dict(r) for r in entry.default_exit_rules],
            )
        else:
            raw_rules = [dict(r) for r in entry.default_exit_rules]
        rules = _sort_exit_rules(raw_rules)
        exit_section = ExitSection(rules=rules, priority="first_match")  # type: ignore[arg-type]

        # 7. provenance
        provenance = ProvenanceSection(
            scout_run_id=inputs.scout_run_id,
            scout_code_hash=inputs.scout_code_hash,
            scout_hypothesis=inputs.scout_hypothesis,
            macro_state_snapshot=inputs.macro_state,
            macro_run_id=inputs.macro_state.gate_run_id,
            news_score_at_generation=inputs.news_score,
            strategy_policy_version=self._policy.version,
            generated_by=self._generated_by,
            conviction=float(candidate.conviction)
            if getattr(candidate, "conviction", None) is not None
            else None,
        )

        # 8. sheet 조립
        sheet_id = generate_sheet_id(candidate.ticker, inputs.generated_at)
        valid_until = _sheet_valid_until(inputs.generated_at)

        sheet = PositionSheet(
            sheet_id=sheet_id,
            generated_at=inputs.generated_at,
            valid_until=valid_until,
            ticker=candidate.ticker,
            strategy_tag=tag,
            size=size,
            entry=entry_section,
            exit=exit_section,
            provenance=provenance,
        )
        logger.info(
            "sheet_published: ticker=%s tag=%s final_pct=%.4f sheet_id=%s",
            candidate.ticker,
            tag,
            final_pct,
            sheet_id,
        )
        return sheet, None


# =====================================================================
# helpers
# =====================================================================


def _sheet_valid_until(generated_at: datetime) -> datetime:
    """시트 유효기간: 같은 날 15:30 KST. 생성 시각이 이미 15:30 이후면 60초 뒤."""
    kst_gen = generated_at.astimezone(KST)
    eod = kst_gen.replace(hour=15, minute=30, second=0, microsecond=0)
    if eod <= kst_gen:
        return kst_gen + timedelta(seconds=60)
    return eod


def _entry_valid_until(generated_at: datetime) -> datetime:
    """entry 유효기간: 생성 후 1시간, 또는 15:30 KST 중 빠른 쪽. schema는 60초 이상을 요구."""
    kst_gen = generated_at.astimezone(KST)
    one_hour = kst_gen + timedelta(hours=1)
    eod_kst = kst_gen.replace(hour=15, minute=30, second=0, microsecond=0)
    candidate = min(one_hour, eod_kst)

    # 최소 60초는 보장 (schema 요구). 장 마감 직전이면 60초 뒤로.
    if candidate - kst_gen < timedelta(seconds=60):
        return kst_gen + timedelta(seconds=60)
    # entry_valid_until은 sheet.valid_until(=eod 또는 gen+60s)을 넘으면 안 됨.
    # 하지만 sheet.valid_until의 최대는 15:30이고 candidate도 그 값을 최대로 하므로 OK.
    return candidate


__all__ = [
    "ActiveSheetChecker",
    "NullActiveSheetChecker",
    "StrategyEngine",
    "StrategyEngineInputs",
]
