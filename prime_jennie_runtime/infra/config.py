"""Pydantic Settings — 환경변수 기반 설정.

모든 설정은 환경변수 또는 .env 파일에서 로드.
각 섹션은 독립적인 prefix를 가짐.
"""

from __future__ import annotations

from pydantic import Field
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
    """KIS Gateway 설정 (Track C에서 확장)."""

    model_config = SettingsConfigDict(env_prefix="KIS_")

    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""
    account_product_code: str = "01"
    gateway_url: str = "http://localhost:8080"


class TelegramConfig(BaseSettings):
    """Telegram Bot 설정 (Track C에서 확장)."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")

    bot_token: str = ""
    chat_id: str = ""


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
