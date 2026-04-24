"""과거 scout_runs 를 새 피드/새 prompt 로 재실행하는 driver.

2026-04-25 뉴스 피드 재설계 (NewsScoreEntry → NewsEventEntry, 점수 평균 제거 +
events_by_impact 기반) 이후, Qwen3 전환일 (2026-04-21) 이후의 scout_run 들을
새 구조로 다시 돌려 "구 scout code 가 0 반환하던 날 새 code 는 뭘 내놓는지" 측정.

**DB 는 건드리지 않는다**. scout_runs / screening_candidates / position_sheets
모두 read-only. 결과는 JSON 파일로만 출력.

사용 예::

    # 단건
    docker compose exec job-worker uv run python -m scripts.scout_replay \\
        --scout-run-id sr_20260424_0830_b0b689e3

    # 날짜별 첫 run 씩 재실행 (추천 — 동일 날짜에 context 가 비슷해 중복 비용 아끼기)
    docker compose exec job-worker uv run python -m scripts.scout_replay \\
        --since 2026-04-21 --per-day-first \\
        --output reports/scout-replay-2026-04-25.json

    # 전수 재실행 (비용 더 든다)
    docker compose exec job-worker uv run python -m scripts.scout_replay \\
        --since 2026-04-21 --output reports/scout-replay-full.json

컨테이너 안에서 돌려야 DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 가 풀리고 DB/stock
masters 가 접근 가능.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from minyoung_mah import (
    NullHITLChannel,
    NullMemoryStore,
    NullObserver,
    Orchestrator,
    RoleRegistry,
    TieredModelRouter,
    ToolRegistry,
    default_resilience,
)
from sqlalchemy import text

from prime_jennie_runtime.infra.config import PostgresConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.screening_executor.adapter import ScreeningToolAdapter
from prime_jennie_runtime.slow_loop.pipeline import _run_pipeline_with_retry, _scout_pipeline
from prime_jennie_runtime.slow_loop.scout.context_builder import ScoutContextBuilder
from prime_jennie_runtime.slow_loop.scout.feeders.real import (
    RealMarketSummaryFeeder,
    RealNewsEventFeeder,
    RealSectorMomentumFeeder,
    RealUniverseFeeder,
)
from prime_jennie_runtime.slow_loop.scout.market_data_loader import load_market_data_records
from prime_jennie_runtime.slow_loop.scout.role import ScoutRole
from prime_jennie_runtime.slow_loop.scout.schemas import (
    MacroStateForScout,
    ScoutOutput,
)

logger = logging.getLogger("scout_replay")


def _build_deepseek_only_tiers() -> dict[str, Any]:
    """replay 전용 — DeepSeek 만으로 strong/reasoning 채움. shadow 없음.

    ``_try_build_tiered_router`` 는 DEEPSEEK + ANTHROPIC 둘 다 요구하는데 (shadow 용)
    job-worker 엔 ANTHROPIC 안 깔려있을 수 있음. replay 는 primary 만 필요하므로
    가벼운 버전.
    """
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if not deepseek_key:
        raise RuntimeError("DEEPSEEK_API_KEY 누락 — 컨테이너 안에서 실행 필요")
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:
        raise RuntimeError("langchain_openai 미설치 — slow_loop extras 필요") from e

    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

    class _DeepSeekChatOpenAI(ChatOpenAI):
        def with_structured_output(self, schema, *, method=None, **kwargs):  # type: ignore[override]
            return super().with_structured_output(
                schema, method=method or "function_calling", **kwargs
            )

    chat = _DeepSeekChatOpenAI(model=model, api_key=deepseek_key, base_url=base, temperature=0.1)
    return {"strong": chat, "reasoning": chat, "default": chat}


# ---------------------------------------------------------------- load


@dataclass
class _ScoutRunRow:
    scout_run_id: str
    generated_at: datetime
    hypothesis: str | None
    candidates_count: int | None
    code_text: str
    code_hash: str
    context_snapshot: dict[str, Any]
    original_candidate_tickers: list[str]


async def _load_scout_runs(
    engine,
    *,
    scout_run_id: str | None,
    since: date | None,
    per_day_first: bool,
) -> list[_ScoutRunRow]:
    """scout_runs + screening_candidates 조인해서 원본 정보 수집."""
    if scout_run_id:
        where = "WHERE sr.scout_run_id = :sid"
        params: dict[str, Any] = {"sid": scout_run_id}
    elif since:
        where = "WHERE sr.generated_at >= :since"
        params = {"since": datetime.combine(since, datetime.min.time())}
    else:
        raise ValueError("--scout-run-id 또는 --since 중 하나 필요")

    query = text(
        f"""
        SELECT
            sr.scout_run_id, sr.generated_at, sr.hypothesis, sr.candidates_count,
            sr.code_text, sr.code_hash, sr.context_snapshot_json,
            COALESCE(
                (SELECT array_agg(sc.ticker ORDER BY sc.rank)
                 FROM screening_candidates sc
                 WHERE sc.scout_run_id = sr.scout_run_id),
                ARRAY[]::text[]
            ) AS candidate_tickers
        FROM scout_runs sr
        {where}
        ORDER BY sr.generated_at ASC
        """
    )
    async with engine.connect() as conn:
        result = await conn.execute(query, params)
        rows = result.mappings().all()

    out: list[_ScoutRunRow] = []
    seen_days: set[date] = set()
    for r in rows:
        gen = r["generated_at"]
        # KST 기준 날짜 (cron 이 KST 시간으로 돌므로)
        from zoneinfo import ZoneInfo

        kst_day = gen.astimezone(ZoneInfo("Asia/Seoul")).date()
        if per_day_first and kst_day in seen_days:
            continue
        seen_days.add(kst_day)

        ctx_snap = r["context_snapshot_json"]
        if isinstance(ctx_snap, str):
            ctx_snap = json.loads(ctx_snap)
        out.append(
            _ScoutRunRow(
                scout_run_id=r["scout_run_id"],
                generated_at=gen,
                hypothesis=r["hypothesis"],
                candidates_count=r["candidates_count"],
                code_text=r["code_text"],
                code_hash=r["code_hash"],
                context_snapshot=ctx_snap or {},
                original_candidate_tickers=list(r["candidate_tickers"] or []),
            )
        )
    return out


# ---------------------------------------------------------------- replay


def _macro_state_from_snapshot(snap: dict[str, Any]) -> MacroStateForScout:
    """context_snapshot_json 에서 MacroStateForScout 복원."""
    gate_raw = snap.get("macro_gate", "open")
    gate = "open" if gate_raw == "open" else "closed"
    size_raw = snap.get("macro_size_multiplier", 1.0)
    try:
        size = float(size_raw)
    except (TypeError, ValueError):
        size = 1.0
    return MacroStateForScout(
        gate=gate,  # type: ignore[arg-type]
        size_multiplier=max(0.0, min(1.0, size)),
        gate_run_id=snap.get("macro_run_id", "replay_unknown"),
        top_risks_summary="",
    )


async def _replay_one(
    engine,
    orch: Orchestrator,
    screening: ScreeningToolAdapter,
    row: _ScoutRunRow,
    *,
    market_data_lookback: int,
) -> dict[str, Any]:
    """단일 scout_run 을 새 구조로 재실행 + 결과 dict 반환."""
    as_of = row.generated_at.date()
    logger.info(
        "replaying %s (generated_at=%s, as_of=%s)",
        row.scout_run_id,
        row.generated_at.isoformat(),
        as_of.isoformat(),
    )

    # context 재구성 — news_events 만 새 feeder 로, 나머지는 snapshot 재활용
    universe_feeder = RealUniverseFeeder(engine=engine)
    news_feeder = RealNewsEventFeeder(engine=engine)
    sector_feeder = RealSectorMomentumFeeder(engine=engine)
    market_feeder = RealMarketSummaryFeeder(engine=engine)

    builder = ScoutContextBuilder(
        universe=universe_feeder,
        news=news_feeder,
        sector=sector_feeder,
        market=market_feeder,
    )
    macro_state = _macro_state_from_snapshot(row.context_snapshot)
    scout_ctx = await builder.build(as_of=as_of, macro_state=macro_state)

    # universe hash sanity (변경됐으면 경고만)
    original_hash = row.context_snapshot.get("universe_hash")
    if original_hash:
        new_hash = hashlib.sha256(",".join(sorted(scout_ctx.universe)).encode("utf-8")).hexdigest()
        if new_hash != original_hash:
            logger.warning(
                "universe hash drift for %s (original=%s..., replay=%s...)",
                row.scout_run_id,
                original_hash[:8],
                new_hash[:8],
            )

    # Scout 재호출
    res = await _run_pipeline_with_retry(
        orch,
        _scout_pipeline(scout_ctx),
        NullObserver(),
        role_name="scout",
        user_request="scout replay",
    )
    try:
        new_scout_out: ScoutOutput = res.state["scout"].payload_as(ScoutOutput)
    except Exception as e:
        logger.exception("scout role failed for %s", row.scout_run_id)
        return {
            "scout_run_id": row.scout_run_id,
            "generated_at": row.generated_at.isoformat(),
            "error": f"scout role failed: {e}",
            "original": {
                "hypothesis": row.hypothesis,
                "code_hash": row.code_hash,
                "candidates_count": row.candidates_count,
                "candidate_tickers": row.original_candidate_tickers,
            },
        }

    # market_data point-in-time 로드
    market_data = await load_market_data_records(
        engine,
        universe=scout_ctx.universe,
        as_of=as_of,
        lookback_days=market_data_lookback,
    )

    screening_context = {
        "as_of": as_of.isoformat(),
        "universe": scout_ctx.universe,
        "news_events": {t: e.model_dump(mode="json") for t, e in scout_ctx.news_events.items()},
        "sector_momentum": scout_ctx.sector_momentum,
        "macro_size_multiplier": macro_state.size_multiplier,
        "market_data_records": market_data,
    }

    # 새 code 실행
    try:
        new_candidates = await screening.invoke(new_scout_out.screening_code, screening_context)
    except Exception as e:
        logger.exception("screening execution failed for %s", row.scout_run_id)
        new_candidates = []
        screen_error: str | None = f"screening execution failed: {e}"
    else:
        screen_error = None

    new_code_hash = hashlib.sha256(new_scout_out.screening_code.encode("utf-8")).hexdigest()

    return {
        "scout_run_id": row.scout_run_id,
        "generated_at": row.generated_at.isoformat(),
        "as_of": as_of.isoformat(),
        "original": {
            "hypothesis": row.hypothesis,
            "code_hash": row.code_hash,
            "candidates_count": row.candidates_count,
            "candidate_tickers": row.original_candidate_tickers,
        },
        "replay": {
            "hypothesis": new_scout_out.hypothesis,
            "code_hash": new_code_hash,
            "code_text": new_scout_out.screening_code,
            "expected_candidates": new_scout_out.expected_candidates,
            "fallback_strategy": new_scout_out.fallback_strategy,
            "candidate_count": len(new_candidates),
            "candidates": [c.model_dump(mode="json") for c in new_candidates],
            "screening_error": screen_error,
        },
    }


# ---------------------------------------------------------------- main


async def _amain(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    engine = create_engine(PostgresConfig())
    try:
        rows = await _load_scout_runs(
            engine,
            scout_run_id=args.scout_run_id,
            since=_parse_date(args.since),
            per_day_first=args.per_day_first,
        )
        if not rows:
            logger.warning("매칭되는 scout_run 이 없음")
            return
        logger.info("target scout_runs: %d", len(rows))

        # Orchestrator 구성 — replay 전용 DeepSeek-only tiers (shadow 없음)
        tiers = _build_deepseek_only_tiers()
        orch = Orchestrator(
            role_registry=RoleRegistry.of(ScoutRole()),
            tool_registry=ToolRegistry(),
            model_router=TieredModelRouter(tiers),
            memory=NullMemoryStore(),
            hitl=NullHITLChannel(),
            observer=NullObserver(),
            resilience=default_resilience(),
        )
        # screening — subprocess backend (ScreeningToolAdapter). docker sock 없는 상황에선
        # subprocess 로도 성공.
        screening = ScreeningToolAdapter(backend="subprocess", timeout_s=30.0)

        results: list[dict[str, Any]] = []
        for i, row in enumerate(rows, 1):
            logger.info("[%d/%d] %s", i, len(rows), row.scout_run_id)
            try:
                out = await _replay_one(
                    engine,
                    orch,
                    screening,
                    row,
                    market_data_lookback=args.market_data_lookback,
                )
            except Exception as e:
                logger.exception("replay failed: %s", row.scout_run_id)
                out = {
                    "scout_run_id": row.scout_run_id,
                    "generated_at": row.generated_at.isoformat(),
                    "error": f"replay failed: {e}",
                }
            results.append(out)

        # 요약
        _print_summary(results)

        # 파일 출력
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
            logger.info("wrote %s", args.output)
    finally:
        await engine.dispose()


def _print_summary(results: list[dict[str, Any]]) -> None:
    print("\n=== Scout Replay Summary ===")
    print(f"runs: {len(results)}")
    ok = [
        r
        for r in results
        if "error" not in r and r.get("replay", {}).get("screening_error") is None
    ]
    errors = [r for r in results if "error" in r]
    screen_errors = [
        r for r in results if "error" not in r and r.get("replay", {}).get("screening_error")
    ]
    print(f"  success: {len(ok)}")
    print(f"  replay errors: {len(errors)}")
    print(f"  screening errors: {len(screen_errors)}")

    if ok:
        print("\n-- candidate_count 변화 (original → replay) --")
        for r in ok:
            orig_n = len(r["original"]["candidate_tickers"])
            new_n = r["replay"]["candidate_count"]
            arrow = "→"
            diff = new_n - orig_n
            sign = "+" if diff > 0 else ""
            print(f"  {r['scout_run_id']:<30} {orig_n:3d} {arrow} {new_n:3d}  ({sign}{diff})")

    if errors or screen_errors:
        print("\n-- errors --")
        for r in errors:
            print(f"  {r.get('scout_run_id', '?')}: {r.get('error')}")
        for r in screen_errors:
            print(f"  {r['scout_run_id']}: screen={r['replay']['screening_error']}")


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n", maxsplit=1)[0] if __doc__ else None,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--scout-run-id", help="단일 scout_run_id")
    src.add_argument("--since", help="이 날짜 이후 (YYYY-MM-DD)")
    p.add_argument(
        "--per-day-first",
        action="store_true",
        help="--since 와 함께. KST 날짜별 첫 run 만 재실행",
    )
    p.add_argument(
        "--market-data-lookback",
        type=int,
        default=int(os.environ.get("SCOUT_MARKET_DATA_LOOKBACK_DAYS", "60")),
    )
    p.add_argument("--output", help="결과 JSON 경로 (없으면 stdout 요약만)")
    p.add_argument("--log-level", default="INFO")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
