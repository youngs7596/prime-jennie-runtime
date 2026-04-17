"""Strategy Engine — ScreeningCandidate를 PositionSheet로 조립.

결정론 전수. LLM 사용 금지 (Phase 0 design §1.1).
Scout candidates × Macro state × RiskThrottle snapshot → PositionSheet | None.

None이 반환되는 경우:
- Macro gate == "closed"
- final_pct < MIN_POSITION_PCT
- strategy_tag 정책 미존재 (폐기된 RSI_REBOUND 등)
- 같은 ticker 오늘 활성 시트 이미 존재 (중복 방지, T10)
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


# =====================================================================
# 중복 체크 Protocol (Track A migrations의 position_sheets 테이블 조회)
# =====================================================================


@runtime_checkable
class ActiveSheetChecker(Protocol):
    """같은 날 같은 ticker에 활성 시트가 있는지 확인."""

    async def has_active_sheet_today(self, ticker: str, as_of_date: datetime) -> bool: ...


class NullActiveSheetChecker:
    """항상 False 반환 — Phase 1 기본 (DB 미연결 환경)."""

    async def has_active_sheet_today(self, ticker: str, as_of_date: datetime) -> bool:
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

        거부 사유는 logger.info로 남긴다 (테스트가 검증).
        """
        tag = candidate.strategy_tag

        # 1. strategy_tag 유효성
        if tag in DEPRECATED_STRATEGY_TAGS:
            logger.info("sheet_rejected: deprecated tag ticker=%s tag=%s", candidate.ticker, tag)
            return None
        if tag not in ALLOWED_STRATEGY_TAGS:
            logger.info("sheet_rejected: unknown tag ticker=%s tag=%s", candidate.ticker, tag)
            return None
        if not self._policy.has(tag):
            logger.info("sheet_rejected: no policy ticker=%s tag=%s", candidate.ticker, tag)
            return None

        # 2. Macro gate == "closed"면 발행 안 함
        if inputs.macro_state.gate == "closed":
            logger.info(
                "sheet_rejected: macro closed ticker=%s gate=%s",
                candidate.ticker,
                inputs.macro_state.gate,
            )
            return None

        # 3. 중복 체크 (같은 날 같은 ticker 이미 활성)
        if await self._active_checker.has_active_sheet_today(candidate.ticker, inputs.generated_at):
            logger.info(
                "sheet_rejected: duplicate ticker=%s date=%s",
                candidate.ticker,
                inputs.generated_at.date(),
            )
            return None

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
            return None

        size = SizeSection(
            base_pct=entry.base_pct,
            macro_multiplier=macro_mult,
            risk_multiplier=risk_mult,
            final_pct=final_pct,
            max_notional_krw=entry.max_notional_krw,
        )

        # 5. entry 조립 — Scout entry_hint 반영
        entry_valid_until = _entry_valid_until(inputs.generated_at)
        entry_section = EntrySection(
            trigger=candidate.entry_hint.trigger,
            price=candidate.entry_hint.price_hint,
            valid_until=entry_valid_until,
            conditions=candidate.entry_hint.conditions_hint,  # type: ignore[arg-type]
        )

        # 6. exit 조립 — Scout exit_hint 있으면 그것, 없으면 policy 기본값
        if candidate.exit_hint is not None:
            raw_rules = list(candidate.exit_hint.rules_hint)
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
            news_score_at_generation=inputs.news_score,
            strategy_policy_version=self._policy.version,
            generated_by=self._generated_by,
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
        return sheet


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
