"""Intraday Risk Throttle — 5단계 장중 리스크 조정.

v2 prime_jennie/services/jobs/app.py L1196-1452 포팅. sync threading → async.

구현 포인트:
  - `_calc_intraday_multiplier_sync`는 v2 순수함수 그대로 (KOSPI/VIX 기반)
  - `_apply_recovery_logic_async`는 Redis recovery_start 관리 (비동기)
  - 악화는 즉시 반영 + recovery_start 리셋
  - 회복은 단계적 + 지연 (CAUTION 5분 / WARNING 10분 / DANGER 20분 / CRITICAL 30분)
  - SOX 하한선: -2% → CAUTION / -3% → WARNING 강제 상향
  - `current_multiplier()`로 `RiskThrottleSnapshot` Protocol 구현
  - clock 주입 가능: 테스트에서 freezegun 없이 시간 제어 가능

Slow loop는 시트 발행 시 `current_multiplier()` 스냅샷만 읽는다
(POSITION_SHEET_SPEC §3.1). Fast loop는 exit 평가 시 읽지 않지만,
risk_updater가 주기적으로 갱신한다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import redis.asyncio as aioredis

# ─── 상수 (v2 그대로) ─────────────────────────────────────────────
LEVEL_MULTIPLIER: dict[str, float] = {
    "NORMAL": 1.0,
    "CAUTION": 0.8,
    "WARNING": 0.6,
    "DANGER": 0.3,
    "CRITICAL": 0.0,
}

# 심각도 (높을수록 나쁨)
RISK_LEVEL_SEVERITY: dict[str, int] = {
    "NORMAL": 0,
    "CAUTION": 1,
    "WARNING": 2,
    "DANGER": 3,
    "CRITICAL": 4,
}

# severity → level 역매핑 (dict는 insertion-order 보장)
_SEVERITY_TO_LEVEL: dict[int, str] = {v: k for k, v in RISK_LEVEL_SEVERITY.items()}

# 회복 시 필요 사이클 수 (1사이클 = 5분)
RECOVERY_CYCLES: dict[str, int] = {
    "CRITICAL": 6,  # 30분
    "DANGER": 4,  # 20분
    "WARNING": 2,  # 10분
    "CAUTION": 1,  # 5분
}

RECOVERY_CYCLE_SEC: int = 300  # 5분

# Redis 키
REDIS_LEVEL_KEY = "intraday:risk:level"
REDIS_RECOVERY_KEY = "intraday:risk:recovery_start"
_REDIS_TTL_SEC = 86400  # 24시간


# ─── 결과 타입 ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RiskUpdateResult:
    """`update()` 결과 — level/multiplier 계산 + council 최종값."""

    level: str
    intraday_multiplier: float
    council_final: float  # min(council_mult, intraday_mult)
    prev_level: str  # 이전 레벨 (알림 판단용)
    level_changed: bool
    raw_level: str  # SOX floor 반영 전 원시 level


# ─── 순수함수: KOSPI/VIX → (level, mult) ──────────────────────────


def calc_intraday_multiplier(kospi_change: float, vix: float) -> tuple[str, float]:
    """KOSPI 일중 등락률과 VIX로 intraday risk level + multiplier 계산.

    v2 `_calc_intraday_multiplier` 그대로. 경계값 포함 (<=, >=).

    Returns:
        (risk_level, intraday_multiplier)
    """
    if kospi_change <= -4.0:
        return "CRITICAL", 0.0
    if kospi_change <= -3.0 or vix >= 40:
        return "DANGER", 0.3
    if kospi_change <= -2.0 or vix >= 35:
        return "WARNING", 0.6
    if kospi_change <= -1.0 or vix >= 30:
        return "CAUTION", 0.8
    return "NORMAL", 1.0


def apply_sox_floor(raw_level: str, sox_change_pct: float | None) -> str:
    """SOX 오버나이트 시그널: -2% → CAUTION / -3% → WARNING 하한선.

    raw_level이 하한선보다 낮으면 상향, 아니면 그대로.
    """
    sox_floor_level = "NORMAL"
    if sox_change_pct is not None:
        if sox_change_pct <= -3.0:
            sox_floor_level = "WARNING"
        elif sox_change_pct <= -2.0:
            sox_floor_level = "CAUTION"

    if RISK_LEVEL_SEVERITY.get(sox_floor_level, 0) > RISK_LEVEL_SEVERITY.get(raw_level, 0):
        return sox_floor_level
    return raw_level


# ─── Throttle 본체 ────────────────────────────────────────────────


class IntradayRiskThrottle:
    """5단계 level + multiplier. Redis로 상태 백업 + in-memory 캐시.

    Attributes:
        _client: redis asyncio 클라이언트
        _clock: 현재시각 반환 callable (주입 가능, 기본 datetime.now)
        _current_level: 마지막으로 계산된 level (in-memory 캐시)
        _current_mult: 마지막으로 계산된 multiplier (Protocol 구현용)
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = redis_client
        self._clock = clock if clock is not None else datetime.now
        # 초기값 — Redis에서 load되지 않는 한 NORMAL
        self._current_level: str = "NORMAL"
        self._current_mult: float = 1.0

    # ─── Protocol 구현 ────────────────────────────────────────────

    def current_multiplier(self) -> float:
        """`RiskThrottleSnapshot` Protocol 구현.

        마지막 알려진 multiplier를 반환 (동기 호출 — in-memory).
        """
        return self._current_mult

    @property
    def current_level(self) -> str:
        """마지막 알려진 level."""
        return self._current_level

    # ─── 상태 로드/저장 ───────────────────────────────────────────

    async def load_state(self) -> None:
        """Redis에서 이전 level을 복원. 재시작 후 호출."""
        raw = await self._client.get(REDIS_LEVEL_KEY)
        if raw is None:
            return
        level = raw.decode() if isinstance(raw, bytes) else str(raw)
        if level in LEVEL_MULTIPLIER:
            self._current_level = level
            self._current_mult = LEVEL_MULTIPLIER[level]

    async def _save_level(self, level: str) -> None:
        await self._client.set(REDIS_LEVEL_KEY, level, ex=_REDIS_TTL_SEC)

    # ─── 메인 업데이트 ────────────────────────────────────────────

    async def update(
        self,
        kospi_pct: float,
        vix: float | None,
        sox_pct: float | None = None,
        council_mult: float = 1.0,
        now: datetime | None = None,
    ) -> RiskUpdateResult:
        """시장 지표 받아 level 계산 + 회복 지연 적용 + Redis 저장.

        Args:
            kospi_pct: KOSPI 일중 등락률 (예: -2.5)
            vix: VIX 지수 (None이면 0.0 취급)
            sox_pct: SOX 변동률 (None이면 floor 미적용)
            council_mult: Council 배치 multiplier (raw)
            now: 현재 시각 (None이면 clock 사용)

        Returns:
            RiskUpdateResult
        """
        now = now if now is not None else self._clock()

        # 1) raw level 계산
        raw_level, raw_mult = calc_intraday_multiplier(
            kospi_pct,
            vix if vix is not None else 0.0,
        )

        # 2) SOX 하한선 적용
        effective_raw_level = apply_sox_floor(raw_level, sox_pct)
        if effective_raw_level != raw_level:
            raw_mult = LEVEL_MULTIPLIER[effective_raw_level]

        # 3) Redis에 저장된 이전 level — in-memory 캐시보다 우선
        stored = await self._client.get(REDIS_LEVEL_KEY)
        if stored is not None:
            prev_level = stored.decode() if isinstance(stored, bytes) else str(stored)
            if prev_level not in LEVEL_MULTIPLIER:
                prev_level = self._current_level
        else:
            prev_level = self._current_level

        # 4) 회복 지연 적용
        new_level, intraday_mult = await self._apply_recovery_logic(
            prev_level=prev_level,
            raw_level=effective_raw_level,
            raw_multiplier=raw_mult,
            now=now,
        )

        # 5) 상태 저장
        await self._save_level(new_level)
        self._current_level = new_level
        self._current_mult = intraday_mult

        # 6) council 최종값 — min() 역전 방지
        council_final = min(council_mult, intraday_mult)

        return RiskUpdateResult(
            level=new_level,
            intraday_multiplier=intraday_mult,
            council_final=council_final,
            prev_level=prev_level,
            level_changed=(new_level != prev_level),
            raw_level=effective_raw_level,
        )

    # ─── 회복 로직 (async) ────────────────────────────────────────

    async def _apply_recovery_logic(
        self,
        prev_level: str,
        raw_level: str,
        raw_multiplier: float,
        now: datetime,
    ) -> tuple[str, float]:
        """v2 `_apply_recovery_logic` async 포팅.

        - 악화/동일: 즉시 적용 + recovery_start 삭제
        - 회복: recovery_start 기록 → 경과 시간 체크 → 한 단계씩 완화
        """
        prev_sev = RISK_LEVEL_SEVERITY.get(prev_level, 0)
        raw_sev = RISK_LEVEL_SEVERITY[raw_level]

        # 악화 또는 동일 — 즉시 적용, 회복 타이머 초기화
        if raw_sev >= prev_sev:
            await self._client.delete(REDIS_RECOVERY_KEY)
            return raw_level, raw_multiplier

        # ─── 조건 개선 (회복 모드) ───
        now_ts = now.timestamp()
        recovery_start_raw = await self._client.get(REDIS_RECOVERY_KEY)

        if recovery_start_raw is None:
            # 회복 타이머 시작 — 현재 레벨 유지
            await self._client.set(REDIS_RECOVERY_KEY, str(now_ts), ex=_REDIS_TTL_SEC)
            return prev_level, LEVEL_MULTIPLIER[prev_level]

        start_str = (
            recovery_start_raw.decode()
            if isinstance(recovery_start_raw, bytes)
            else str(recovery_start_raw)
        )
        try:
            recovery_start = float(start_str)
        except ValueError:
            # 손상된 값 — 리셋하고 현재 유지
            await self._client.set(REDIS_RECOVERY_KEY, str(now_ts), ex=_REDIS_TTL_SEC)
            return prev_level, LEVEL_MULTIPLIER[prev_level]

        elapsed = now_ts - recovery_start
        required_cycles = RECOVERY_CYCLES.get(prev_level, 1)
        required_sec = required_cycles * RECOVERY_CYCLE_SEC

        if elapsed >= required_sec:
            # 한 단계 회복 (단계 건너뛰기 금지)
            new_sev = max(prev_sev - 1, 0)
            new_level = _SEVERITY_TO_LEVEL[new_sev]
            # 다음 단계 회복용 타이머 리셋
            await self._client.set(REDIS_RECOVERY_KEY, str(now_ts), ex=_REDIS_TTL_SEC)
            return new_level, LEVEL_MULTIPLIER[new_level]

        # 아직 회복 시간 미충족 — 현재 레벨 유지
        return prev_level, LEVEL_MULTIPLIER[prev_level]
