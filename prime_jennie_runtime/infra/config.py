"""Pydantic Settings — 환경변수 기반 설정.

모든 설정은 환경변수 또는 .env 파일에서 로드.
각 섹션은 독립적인 prefix를 가짐.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    gateway_url: str = "http://localhost:8080"

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
