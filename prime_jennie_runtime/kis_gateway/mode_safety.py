"""KIS Gateway paper/real 모드 안전장치.

설계 의도:
  ``KIS_IS_PAPER`` 환경변수 오류 시 자동으로 실계좌에 닿을 위험을 차단.
  실계좌 모드는 별도 명시적 confirm 환경변수가 정확히 일치해야만 부팅 허용.

게이트 정책:
  - paper 모드 (is_paper=True): 추가 확인 없이 부팅. 단 account_no 가 실계좌
    패턴(앞자리가 ``5`` 가 아닌 경우)이면 경고 로그만 (블로킹 X — KIS 모의계좌가
    반드시 50000000 대인지 100% 보장 어려움).
  - real 모드 (is_paper=False): ``KIS_REAL_CONFIRMED`` 가 정확히
    ``YES_I_ACCEPT_REAL_TRADING`` 이어야 통과. 그 외엔 RuntimeError 로 부팅 거부.

부팅 시 항상 박스 배너를 INFO 로 찍어 운영자가 모드를 한눈에 확인 가능.
"""

from __future__ import annotations

import logging
import os

from prime_jennie_runtime.infra.config import KISConfig

logger = logging.getLogger(__name__)

#: 실계좌 모드 진입에 요구되는 confirm 환경변수 (정확 일치).
REAL_CONFIRM_ENV = "KIS_REAL_CONFIRMED"
REAL_CONFIRM_VALUE = "YES_I_ACCEPT_REAL_TRADING"

#: KIS 모의계좌 번호는 통상 ``5`` 로 시작 (50000000 대). 100% 보장은 아님.
PAPER_ACCOUNT_PREFIX = "5"


def enforce_mode_safety(config: KISConfig, *, env: dict[str, str] | None = None) -> None:
    """KIS Gateway 부팅 전 모드 일관성 검증 + 배너 출력.

    Args:
        config: KIS 설정.
        env: 환경변수 dict (테스트용 주입). 기본은 ``os.environ``.

    Raises:
        RuntimeError: real 모드인데 confirm 환경변수가 없거나 잘못된 경우.
    """
    environ = env if env is not None else os.environ

    if config.is_paper:
        _log_banner("PAPER (모의투자)", config)
        _sanity_check_paper_account(config.account_no)
        return

    confirm = environ.get(REAL_CONFIRM_ENV, "")
    if confirm != REAL_CONFIRM_VALUE:
        # 부팅 거부 — 운영자가 환경변수를 명시적으로 세팅해야 통과.
        msg = (
            f"REAL trading mode requested (KIS_IS_PAPER=false) but "
            f"{REAL_CONFIRM_ENV} is not set to '{REAL_CONFIRM_VALUE}'. "
            f"Refusing to start as safety guard. "
            f"Set {REAL_CONFIRM_ENV}={REAL_CONFIRM_VALUE} to proceed, "
            f"or set KIS_IS_PAPER=true for paper trading."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    _log_banner("REAL (실계좌 — 실제 자금)", config)


def _sanity_check_paper_account(account_no: str) -> None:
    """paper 모드인데 계좌번호가 실계좌 패턴이면 경고."""
    if not account_no:
        return  # 빈 값은 다른 곳에서 검증.
    if not account_no.startswith(PAPER_ACCOUNT_PREFIX):
        logger.warning(
            "KIS_IS_PAPER=true but account_no %s does not start with '%s' — "
            "this may be a real account misconfigured as paper. Double-check.",
            _mask_account(account_no),
            PAPER_ACCOUNT_PREFIX,
        )


def _mask_account(account_no: str) -> str:
    """계좌번호 마스킹 (앞 2자리 + **** + 뒤 2자리)."""
    if len(account_no) <= 4:
        return "****"
    return f"{account_no[:2]}****{account_no[-2:]}"


def _log_banner(mode_label: str, config: KISConfig) -> None:
    """기동 모드를 박스 배너로 INFO 로깅."""
    masked = _mask_account(config.account_no) if config.account_no else "(unset)"
    line = "=" * 60
    logger.info(line)
    logger.info("  KIS Gateway mode: %s", mode_label)
    logger.info("  base_url        : %s", config.base_url)
    logger.info("  account_no      : %s", masked)
    logger.info(line)
