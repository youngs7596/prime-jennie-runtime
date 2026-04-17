"""infra/config.py 테스트."""

from prime_jennie_runtime.infra.config import (
    AppConfig,
    LLMConfig,
    PostgresConfig,
    RedisConfig,
)


def test_postgres_dsn():
    cfg = PostgresConfig(host="db", port=5432, user="u", password="p", db="d")
    assert cfg.dsn == "postgresql+asyncpg://u:p@db:5432/d"


def test_redis_url():
    cfg = RedisConfig(host="r", port=6379, password="pw", db=1)
    assert cfg.url == "redis://:pw@r:6379/1"


def test_llm_defaults():
    cfg = LLMConfig()
    assert "exaone" in cfg.fast
    assert "deepseek" in cfg.reasoning


def test_app_config_compose():
    cfg = AppConfig()
    assert cfg.postgres.port == 5432
    assert cfg.redis.port == 6379
    assert cfg.env == "dev"
