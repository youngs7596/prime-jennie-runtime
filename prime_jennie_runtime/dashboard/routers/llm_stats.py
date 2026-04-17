"""LLM Stats API — LLM 사용량 통계 + 기능별 LLM 매핑.

v2 원본: `prime_jennie/services/dashboard/routers/llm_stats.py`

v3 는 v2 의 Redis 키 포맷을 그대로 사용한다 (`llm:stats:{YYYY-MM-DD}:{service}`,
Hash 필드: calls / tokens_in / tokens_out). 월 집계는 일별 키들의 합으로 계산.

Tier 매핑은 v3 `LLMConfig` (infra/config.py) 를 읽어 표시.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401

from prime_jennie_runtime.infra.config import LLMConfig

from ..deps import get_redis

router = APIRouter(prefix="/llm", tags=["llm"])

# 추적 대상 서비스 목록 (v3 기준 — news_analysis/scout/macro/briefing)
_SERVICES = ["scout", "macro", "news_analysis", "briefing", "unknown"]

# 기능별 LLM 매핑 (정적 정보, v3 tier 매핑과 맞춘 최소 집합)
_FEATURE_MAP = [
    {
        "service": "news_analysis",
        "name": "뉴스 감성 분석",
        "tier": "fast",
        "frequency": "실시간 (배치)",
    },
    {
        "service": "scout",
        "name": "Scout 종목 분석",
        "tier": "strong",
        "frequency": "장중 매 시간",
    },
    {
        "service": "macro",
        "name": "Macro Gate",
        "tier": "reasoning",
        "frequency": "장 시작 전",
    },
    {
        "service": "briefing",
        "name": "데일리 브리핑",
        "tier": "reasoning",
        "frequency": "1일 1회",
    },
]


def _llm_config(request: Request) -> LLMConfig:
    cfg = getattr(request.app.state, "config", None)
    if cfg is not None and hasattr(cfg, "llm"):
        return cfg.llm
    return LLMConfig()


async def _build_daily_stats(target_date: str, r: aioredis.Redis) -> dict:
    services: dict[str, dict] = {}
    total_calls = 0
    total_tokens_in = 0
    total_tokens_out = 0

    for svc in _SERVICES:
        data = await r.hgetall(f"llm:stats:{target_date}:{svc}")
        if not data:
            continue
        calls = int(data.get("calls", 0))
        tokens_in = int(data.get("tokens_in", 0))
        tokens_out = int(data.get("tokens_out", 0))
        services[svc] = {"calls": calls, "tokens_in": tokens_in, "tokens_out": tokens_out}
        total_calls += calls
        total_tokens_in += tokens_in
        total_tokens_out += tokens_out

    return {
        "date": target_date,
        "services": services,
        "total": {
            "calls": total_calls,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
        },
    }


async def _build_monthly_stats(target_month: str, r: aioredis.Redis) -> dict:
    """YYYY-MM 월의 일별 키를 SCAN 으로 모아 합산."""
    services: dict[str, dict] = {
        svc: {"calls": 0, "tokens_in": 0, "tokens_out": 0} for svc in _SERVICES
    }
    total = {"calls": 0, "tokens_in": 0, "tokens_out": 0}

    async for key in r.scan_iter(match=f"llm:stats:{target_month}-*"):
        parts = key.split(":")
        if len(parts) < 4:
            continue
        svc = parts[3]
        data = await r.hgetall(key)
        if not data:
            continue
        bucket = services.setdefault(svc, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
        for field in ("calls", "tokens_in", "tokens_out"):
            val = int(data.get(field, 0))
            bucket[field] += val
            total[field] += val

    # 0 인 서비스는 제거
    services = {
        k: v for k, v in services.items() if v["calls"] or v["tokens_in"] or v["tokens_out"]
    }

    return {"month": target_month, "services": services, "total": total}


@router.get("/features")
def get_features(request: Request) -> list[dict[str, Any]]:
    """기능별 LLM 매핑 — tier → v3 config 모델."""
    cfg = _llm_config(request)
    tier_model = {
        "fast": cfg.fast,
        "default": cfg.default,
        "strong": cfg.strong,
        "reasoning": cfg.reasoning,
    }
    return [{**feat, "model": tier_model.get(feat["tier"], "unknown")} for feat in _FEATURE_MAP]


@router.get("/stats/monthly/{target_month}")
async def get_monthly_stats(
    target_month: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    """월별 LLM 사용량. target_month: YYYY-MM"""
    return await _build_monthly_stats(target_month, r)


@router.get("/stats/monthly")
async def get_current_month_stats(r: aioredis.Redis = Depends(get_redis)) -> dict:
    return await _build_monthly_stats(datetime.now().strftime("%Y-%m"), r)


@router.get("/stats/{target_date}")
async def get_stats(target_date: str, r: aioredis.Redis = Depends(get_redis)) -> dict:
    return await _build_daily_stats(target_date, r)


@router.get("/stats")
async def get_today_stats(r: aioredis.Redis = Depends(get_redis)) -> dict:
    return await _build_daily_stats(date.today().isoformat(), r)
