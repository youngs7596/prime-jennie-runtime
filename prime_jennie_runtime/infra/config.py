"""Pydantic Settings — 환경변수 기반 설정.

모든 설정은 환경변수 또는 .env 파일에서 로드.
각 섹션은 독립적인 prefix를 가짐.
"""

from __future__ import annotations

import logging
import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# docker-compose 의 KIS gateway 서비스 이름 + 포트 — env 누락 시 fallback.
_DEFAULT_KIS_GATEWAY_URL = "http://kis-gateway:8080"


class PostgresConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_")

    host: str = "localhost"
    port: int = 5432
    user: str = "pj_runtime"
    password: str = "dev_password"
    db: str = "prime_jennie_v3"

    @property
    def dsn(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class RedisConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_")

    host: str = "localhost"
    port: int = 6379
    password: str = "dev_redis"
    db: int = 0

    @property
    def url(self) -> str:
        return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"


class LLMConfig(BaseSettings):
    """LiteLLM 모델 티어 매핑."""

    model_config = SettingsConfigDict(env_prefix="LITELLM_MODEL_")

    fast: str = "ollama/exaone3.5:32b"
    default: str = "deepseek/deepseek-chat"
    strong: str = "deepseek/deepseek-chat"
    reasoning: str = "deepseek/deepseek-reasoner"


class LangfuseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LANGFUSE_")

    public_key: str = ""
    secret_key: str = ""
    host: str = "https://cloud.langfuse.com"

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self.secret_key)


class KISConfig(BaseSettings):
    """KIS Gateway 설정 (Track C)."""

    model_config = SettingsConfigDict(env_prefix="KIS_")

    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""
    account_product_code: str = "01"
    # docker-compose 의 서비스 이름 기본값 — env 누락 시 localhost 로 가서 ConnectError
    # 나는 사고 (2026-04 자산 스냅샷 사고) 재발 방지. 로컬 개발에서 다른 호스트면
    # KIS_GATEWAY_URL 명시 필수.
    gateway_url: str = _DEFAULT_KIS_GATEWAY_URL

    # KIS OpenAPI 서버 (모의 vs 실계좌)
    base_url: str = "https://openapivts.koreainvestment.com:29443"  # 모의
    is_paper: bool = True

    # 토큰 캐시 파일 — v2와 충돌 방지 위해 v3 별도 경로 사용
    token_file_path: str = "./data/kis_token/v3_kis_token.json"

    # 시세 공급 모드: "websocket" | "poller" | "both"
    streamer_mode: str = "websocket"
    polling_interval_sec: float = 1.0

    # Rate limit (KIS 공식 상한)
    rate_limit_market_per_sec: int = 19
    rate_limit_trade_per_sec: int = 5

    # 전역 합산 rate limit — KIS 는 시세/매매 구분 없이 앱키 단위로 초당 호출을
    # 합산해 세므로, 모든 KIS 호출 (재시도 포함) 이 공유하는 한도가 따로 필요하다.
    # 위 시세/매매 분리 버킷만으로는 19 + 5 = 24/sec 까지 허용되어 합산 한도를
    # 넘는다 (2026-06-02 EGW00201 연쇄 사고 원인).
    #
    # 기본값이 문서상 한도 (실전 20/sec) 보다 훨씬 보수적인 이유: 2026-06-03 실측에서
    # 이 계좌는 초당 4~6건 수준에서도 간헐 거부됐다. 장기간 한도 위반 누적 (v2 시절
    # WebSocket 차단 전력 포함) 으로 KIS 측이 민감 상태로 보이며, 위반 0 을 유지해
    # 상태가 풀리는 것을 확인한 뒤에 env 로 올린다.
    rate_limit_global_per_sec: int = 3
    rate_limit_global_burst: int = 1

    # Circuit breaker
    circuit_fail_max: int = 20
    circuit_reset_sec: int = 60


class TelegramConfig(BaseSettings):
    """Telegram Bot 설정 (Track C)."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")

    bot_token: str = ""
    chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    parse_mode: str = "HTML"
    dry_run: bool = False  # True면 실제 전송 없이 로그만

    # 양방향 명령 allowlist — 빈 리스트면 기동 거부 (fail-safe, Phase 2 D4).
    # 환경변수 형식: "TELEGRAM_ALLOWED_CHAT_IDS=123456,7891011"
    allowed_chat_ids: list[str] = Field(default_factory=list)
    # long-poll 간격 (getUpdates timeout)
    long_poll_timeout_s: int = 30
    # 동일 chat 명령 최소 간격
    command_min_interval_s: int = 5

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def _parse_allowed_chat_ids(cls, v: object) -> object:
        """환경변수에서는 콤마 구분 문자열도 허용 (JSON 리스트 포맷 병행)."""
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return v  # JSON 은 기본 파서가 처리
            return [s.strip() for s in stripped.split(",") if s.strip()]
        return v


class AppConfig(BaseSettings):
    """루트 설정 — 모든 하위 설정을 조합."""

    model_config = SettingsConfigDict(env_prefix="APP_")

    timezone: str = "Asia/Seoul"
    log_level: str = "INFO"
    env: str = Field(default="dev", description="dev | staging | production")

    postgres: PostgresConfig = PostgresConfig()
    redis: RedisConfig = RedisConfig()
    llm: LLMConfig = LLMConfig()
    langfuse: LangfuseConfig = LangfuseConfig()
    kis: KISConfig = KISConfig()
    telegram: TelegramConfig = TelegramConfig()


def validate_kis_gateway_url(cfg: KISConfig, *, strict: bool = False) -> str:
    """KIS_GATEWAY_URL 환경변수 + ``KISConfig.gateway_url`` 검증.

    호출 시점:
      - fast_loop / telegram_bot / slow_loop 등 KisClient 를 사용하는 서비스의
        startup. compose 가 env 를 주입하지 않은 채 컨테이너가 뜨면 localhost 로
        가서 ConnectError 가 발생, retry/circuit breaker 가 무의미하게 카운트됨.

    동작:
      - 명시 URL (env or config) 이 비어 있거나 localhost 인데 ``strict=True``
        (예: env=production) 이면 ``RuntimeError`` 로 startup 중단.
      - 그렇지 않으면 docker-compose 서비스 이름 (`http://kis-gateway:8080`) 로
        fallback 하고 WARN 로그.

    Returns: 검증/정규화된 gateway URL (trailing slash 제거).
    """
    raw = os.environ.get("KIS_GATEWAY_URL", "") or cfg.gateway_url
    raw = raw.strip()
    if not raw:
        if strict:
            raise RuntimeError(
                "KIS_GATEWAY_URL 가 비어 있다. 컨테이너 env 또는 KISConfig.gateway_url 을 "
                f"설정해라 (production fallback: {_DEFAULT_KIS_GATEWAY_URL})."
            )
        logger.warning(
            "KIS_GATEWAY_URL 누락 — docker-compose 기본값 %s 로 fallback", _DEFAULT_KIS_GATEWAY_URL
        )
        return _DEFAULT_KIS_GATEWAY_URL

    # localhost 는 compose 내부에서 호출 시 거의 항상 잘못된 설정 (gateway 컨테이너가
    # 별도 호스트). production 에선 차단, dev 는 경고만.
    normalized = raw.rstrip("/")
    if "localhost" in normalized or "127.0.0.1" in normalized:
        if strict:
            raise RuntimeError(
                f"KIS_GATEWAY_URL={normalized} 가 localhost 를 가리킨다. "
                "compose 환경에선 서비스 이름 (예: http://kis-gateway:8080) 을 사용해라. "
                "강제로 localhost 를 쓰고 싶다면 strict=False 로 호출."
            )
        logger.warning(
            "KIS_GATEWAY_URL=%s 가 localhost — compose 내부 호출이면 ConnectError 가능, "
            "서비스 이름 사용 권장",
            normalized,
        )

    return normalized
