"""LLM Stats API — LLM 사용량 통계 + 기능별 LLM 매핑.

v2 원본: `prime_jennie/services/dashboard/routers/llm_stats.py`

v3 는 v2 의 Redis 키 포맷을 그대로 사용한다 (`llm:stats:{YYYY-MM-DD}:{service}`,
Hash 필드: calls / tokens_in / tokens_out). 월 집계는 일별 키들의 합으로 계산.

Tier 매핑은 v3 `LLMConfig` (infra/config.py) 를 읽어 표시.
"""

from __future__ import annotations

import calendar
import os
from datetime import date, datetime
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401

from prime_jennie_runtime.infra.config import LLMConfig

from ..deps import get_redis
from ..llm_pricing import (
    VLLM_AMORTIZED_DAILY_USD,
    calc_shadow_cost,
)

# news_analysis 는 self-hosted vLLM 이라 record_llm_call cost_usd = 0.
# actual cost 합계에서는 0 으로 잡히고, 별도 vllm_amortized_*_usd 필드로 노출.
_VLLM_SERVICES = frozenset({"news_analysis"})

router = APIRouter(prefix="/llm", tags=["llm"])

# 추적 대상 LLM 서비스 목록. 2026-05-22 Phase 1 에서 scout 는 결정론 quant 코어로
# 전환돼 LLM 을 호출하지 않으므로 scout / scout_shadow 는 제외한다. macro_shadow 는
# historical 데이터 보존 위해 유지 (record_llm_call 은 SHADOW_ENABLED=0 이면 호출
# 안 됨 — 신규 누적 0).
_SERVICES = [
    "macro",
    "macro_shadow",
    "news_analysis",
    "news_global_digest",
    "briefing",
    "telegram_intent",
    "unknown",
]

# 기능별 LLM 매핑. tier 는 참고용, 실제 model/provider 는 각 서비스가 쓰는 env 에서 해석.
# 2026-05-22: scout 는 결정론 quant 코어로 전환 — LLM 매핑에서 제외.
# News extractor 는 Qwen3-30B-A3B MoE (2026-04-21 EXAONE 4.0-32B 에서 교체).
# - news_analysis: vLLM Qwen3 (VLLM_LLM_MODEL)
# - news_global_digest: DeepSeek chat (WSJ/Bloomberg/Reuters 24h 요약)
# - macro: DeepSeek chat (DEEPSEEK_MODEL)
# - briefing: DeepSeek chat (DEEPSEEK_MODEL) — 2026-04-24 Opus 에서 전환
# - telegram_intent: DeepSeek chat (사용자 평문 → 슬래시 명령 분류)
_FEATURE_MAP = [
    {
        "service": "news_analysis",
        "name": "뉴스 메타데이터 추출",
        "tier": "fast",
        "frequency": "실시간 (배치, */10min)",
    },
    {
        "service": "news_global_digest",
        "name": "해외 매크로 뉴스 요약",
        "tier": "reasoning",
        "frequency": "평일 07:30 / 11:30 (Macro 10분 전)",
    },
    {
        "service": "macro",
        "name": "Macro Gate",
        "tier": "reasoning",
        "frequency": "평일 08:30~14:30 매시 30분 (7회/일)",
    },
    {
        "service": "briefing",
        "name": "데일리 브리핑",
        "tier": "reasoning",
        "frequency": "1일 1회 (평일 17:00)",
    },
    {
        "service": "telegram_intent",
        "name": "텔레그램 자연어 명령 분류",
        "tier": "fast",
        "frequency": "사용자 평문 메시지 입력 시",
    },
]


def _service_model(service: str, cfg: LLMConfig) -> tuple[str, str]:
    """service → (model_id, provider_label). slow_loop/briefing 은 env 직독, 나머지는 LLMConfig."""
    if service == "news_analysis":
        # v3: news_pipeline 은 vLLM Qwen3-30B-A3B MoE INT4 (2026-04-21 EXAONE 4.0 에서 교체).
        # compose 기본값과 동기화.
        model = os.environ.get(
            "VLLM_LLM_MODEL",
            "jart25/Qwen3-30B-A3B-Instruct-2507-Autoround-Int-4bit-gptq",
        )
        return model, "vLLM (Qwen3)"
    if service == "news_global_digest":
        # 해외 매크로 뉴스 24h 요약. summarizer 가 MACRO_DIGEST_SUMMARIZER_MODEL
        # 또는 DEEPSEEK_MODEL 을 우선 사용.
        model = (
            os.environ.get("MACRO_DIGEST_SUMMARIZER_MODEL")
            or os.environ.get("DEEPSEEK_MODEL")
            or "deepseek-chat"
        )
        return model, "DeepSeek"
    if service == "macro":
        # 2026-04-22 swap: Macro primary = DeepSeek chat (4일 shadow 비교 결과 Gate 100% 일치).
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        return model, "DeepSeek"
    if service == "macro_shadow":
        # Macro shadow = Claude Opus 4.7 (회귀 검증용 병렬 축적, 1~2주 후 종료).
        model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
        return model, "Anthropic"
    if service == "briefing":
        # 2026-04-24: briefing 도 DeepSeek 로 전환 (Opus 월 ~$30-50 절감).
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        return model, "DeepSeek"
    if service == "telegram_intent":
        # Telegram 평문 → 슬래시 명령 분류기 (telegram_bot/llm_intent.py).
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        return model, "DeepSeek"
    return "unknown", "unknown"


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
    total_cost_actual = 0.0
    total_cost_shadow = 0.0
    has_vllm = False

    for svc in _SERVICES:
        data = await r.hgetall(f"llm:stats:{target_date}:{svc}")
        if not data:
            continue
        calls = int(data.get("calls", 0))
        tokens_in = int(data.get("tokens_in", 0))
        tokens_out = int(data.get("tokens_out", 0))
        cost_actual = float(data.get("cost_usd", 0) or 0)
        cost_shadow = calc_shadow_cost(tokens_in, tokens_out)
        services[svc] = {
            "calls": calls,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_actual_usd": cost_actual,
            "cost_shadow_usd": cost_shadow,
        }
        total_calls += calls
        total_tokens_in += tokens_in
        total_tokens_out += tokens_out
        total_cost_actual += cost_actual
        total_cost_shadow += cost_shadow
        if svc in _VLLM_SERVICES and calls > 0:
            has_vllm = True

    return {
        "date": target_date,
        "services": services,
        "total": {
            "calls": total_calls,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "cost_actual_usd": total_cost_actual,
            "cost_shadow_usd": total_cost_shadow,
        },
        # vLLM 호출이 한 번이라도 있었던 날만 amortized 비용을 부과 — 휴장으로
        # 컨테이너 자체가 idle 인 날 amortize 시키면 운영자가 비용 해석을 잘못한다.
        "vllm_amortized_daily_usd": VLLM_AMORTIZED_DAILY_USD if has_vllm else 0.0,
    }


async def _build_monthly_stats(target_month: str, r: aioredis.Redis) -> dict:
    """YYYY-MM 월의 일별 키를 SCAN 으로 모아 합산."""
    base = {
        "calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_actual_usd": 0.0,
        "cost_shadow_usd": 0.0,
    }
    services: dict[str, dict] = {svc: dict(base) for svc in _SERVICES}
    total = dict(base)
    vllm_active_days: set[str] = set()

    async for key in r.scan_iter(match=f"llm:stats:{target_month}-*"):
        parts = key.split(":")
        if len(parts) < 4:
            continue
        day = parts[2]  # YYYY-MM-DD
        svc = parts[3]
        data = await r.hgetall(key)
        if not data:
            continue
        bucket = services.setdefault(svc, dict(base))
        calls = int(data.get("calls", 0))
        tokens_in = int(data.get("tokens_in", 0))
        tokens_out = int(data.get("tokens_out", 0))
        cost_actual = float(data.get("cost_usd", 0) or 0)
        cost_shadow = calc_shadow_cost(tokens_in, tokens_out)
        bucket["calls"] += calls
        bucket["tokens_in"] += tokens_in
        bucket["tokens_out"] += tokens_out
        bucket["cost_actual_usd"] += cost_actual
        bucket["cost_shadow_usd"] += cost_shadow
        total["calls"] += calls
        total["tokens_in"] += tokens_in
        total["tokens_out"] += tokens_out
        total["cost_actual_usd"] += cost_actual
        total["cost_shadow_usd"] += cost_shadow
        if svc in _VLLM_SERVICES and calls > 0:
            vllm_active_days.add(day)

    # vLLM amortized = active 일수 × daily. 미사용 일은 amortize 안 시킴.
    vllm_amortized = VLLM_AMORTIZED_DAILY_USD * len(vllm_active_days)
    # 이번 달의 총 일수도 참고로 (UI 에서 "X / N 일 활성" 표기용).
    try:
        year, month = (int(p) for p in target_month.split("-"))
        days_in_month = calendar.monthrange(year, month)[1]
    except (ValueError, calendar.IllegalMonthError):
        days_in_month = 0

    # 0 인 서비스는 제거
    services = {
        k: v for k, v in services.items() if v["calls"] or v["tokens_in"] or v["tokens_out"]
    }

    return {
        "month": target_month,
        "services": services,
        "total": total,
        "vllm_amortized_monthly_usd": vllm_amortized,
        "vllm_active_days": len(vllm_active_days),
        "days_in_month": days_in_month,
    }


@router.get("/features")
def get_features(request: Request) -> list[dict[str, Any]]:
    """기능별 LLM 매핑 — 각 서비스의 실제 env (VLLM_LLM_MODEL / DEEPSEEK_MODEL / ANTHROPIC_MODEL)
    를 source-of-truth 로 해석. LLMConfig (LITELLM_MODEL_*) 은 fast 티어 fallback 용도로만 남김.
    """
    cfg = _llm_config(request)
    result = []
    for feat in _FEATURE_MAP:
        model, provider = _service_model(feat["service"], cfg)
        result.append({**feat, "model": model, "provider": provider})
    return result


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
