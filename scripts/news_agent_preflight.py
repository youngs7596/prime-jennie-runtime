"""News Agent Pre-flight — 사람 라벨 없이 자동 측정 protocol.

design `.ai/designs/2026-05-23-news-agent.md` §5.

샘플 60건 (random 50 + forced FP cluster 10) 을 Pre-flight Agent (Qwen3) 에
태우고 4 metric 산출:

- Metric 1: Title-coverage rate (Agent 의 ticker 가 title+body 에 substring
  등장하는 비율). precision proxy. gate ≥ 0.95
- Metric 2: Cluster reduction (forced FP cluster 10건의 Agent 출력 ticker 수
  평균). gate ≤ 1.5
- Metric 3: Cross-LLM sanity (10건 한정) — Qwen3 ↔ DeepSeek tickers 일치율
- Metric 4: Crawler overlap — Agent tickers ↔ crawler ticker 비율 (정보용)

산출: stdout summary + `--out` JSON + `--analyses-doc` markdown.

MS-01 에서 실행 (vLLM/postgres 동일 network):
    uv run python scripts/news_agent_preflight.py \\
        --random 50 --forced 10 \\
        --out reports/news-preflight-$(date +%Y%m%d-%H%M).json \\
        --analyses-doc .ai/analyses/2026-05-23-news-agent-preflight.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import litellm  # type: ignore[import-untyped]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("preflight")

KST = UTC  # published_at 은 KST 또는 UTC 혼합 — 분석은 UTC 통일

# ---------------------------------------------------------------------
# Prompt — Pre-flight 전용 (정식 Agent 디자인의 minimal subset)
# ---------------------------------------------------------------------

PROMPT_TEMPLATE = """다음 한국 주식 뉴스의 진짜 주제가 어떤 한국 상장사인지 식별하세요.

헤드라인: {title}
본문: {body}

## 작업
이 기사가 **직접 다루는** 한국 상장사 0~5개. 단순 언급이 아니라 "기사 주제" 인 회사만.

## 절대 ticker 부여 금지 케이스 (tickers=[] + is_market_general=true)
- 코스피/코스닥/지수 일반 동향 (예: "코스피 600p 반등", "외국인 매도", "8000선 도전")
- 환율/금리/유가 시황 (예: "달러 1350원", "WTI 80달러")
- 시장 분류 일반 뉴스 (예: "대구 상장법인 1분기 영업이익", "은행권 전체 동향")
- 해외 회사 단독 주제 (엔비디아·테슬라·애플 등 — 한국 상장사 영향 명시 안 됐으면)
- 정책·규제 일반 (예: "공정위 과징금", "정부 ETF 규제")

## ticker 부여 조건
- 기사 제목 또는 본문에 회사의 정식명/약어/별칭이 명시적 등장
- 그 회사의 사건이 기사 핵심 주제 (CEO 인사, 실적, M&A, 신제품, 수주, 소송 등)

## hallucination 금지 — 가공의 회사명 금지
- 존재하지 않는 종목명 만들지 마세요 (예: "삼성제삼", "대구상장법인")
- 약어가 애매하면 부여 안 함이 안전 (예: "삼성"만으론 삼성전자/삼성물산/삼성SDI 등 불명)
- 본문에 풀명이 나오면 disambiguate. 풀명 없으면 tickers=[]

## 출력 (JSON object only)
{{
  "tickers": ["삼성물산", "..."],
  "ticker_rationales": {{
    "삼성물산": "삼성물산의 압구정4구역 시공권 획득이 주제"
  }},
  "is_market_general": false,
  "confidence": 0.9
}}

- tickers: 한국 상장사 정식 이름 (한글). KOSPI/KOSDAQ 실존 종목만
- is_market_general: tickers 가 비어있고 일반 시장/지수/환율/정책 뉴스이면 true
- confidence: 0.0~1.0

## 예시
입력: "코스피 7200선 마감, 외국인 6조 순매도"
출력: {{"tickers": [], "ticker_rationales": {{}},
       "is_market_general": true, "confidence": 0.95}}

입력: "삼성 노조 부문 70·사업부 30 고집" (본문: 삼성전자 노사 협의)
출력: {{"tickers": ["삼성전자"],
       "ticker_rationales": {{"삼성전자": "노사 협상 주제"}},
       "is_market_general": false, "confidence": 0.85}}

입력: "삼전·하닉 프리마켓 5%대 급등"
출력: {{"tickers": ["삼성전자", "SK하이닉스"],
       "ticker_rationales": {{"삼성전자": "삼전 약어",
                              "SK하이닉스": "하닉 약어"}},
       "is_market_general": false, "confidence": 0.9}}
"""


# ---------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------


def _pg_dsn() -> str:
    return (
        f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}"
        f"/{os.environ['POSTGRES_DB']}"
    )


async def fetch_forced_fp(pool: asyncpg.Pool, n: int) -> list[dict]:
    """algorithm: 같은 title 의 article_id 가 ≥3 개 ticker 와 매핑된 cluster 상위 N.

    naver 의 종목별 referrer URL (`code=`) 때문에 같은 기사가 ticker 마다 다른
    article_id (md5(source_url) 기반) 를 가짐. 따라서 article_id 그룹핑은 의미
    없고, title 동일 기준으로 cluster 식별.
    """
    rows = await pool.fetch(
        """
        WITH title_clusters AS (
            SELECT
                title,
                COUNT(DISTINCT ticker) AS ticker_count,
                ARRAY_AGG(DISTINCT ticker ORDER BY ticker) AS cluster_tickers,
                MIN(article_id) AS sample_article_id
            FROM news_articles
            WHERE published_at > NOW() - INTERVAL '7 days'
            GROUP BY title
            HAVING COUNT(DISTINCT ticker) >= 3
        )
        SELECT
            na.article_id,
            na.title,
            na.body,
            na.ticker AS crawler_ticker,
            na.source_url::text AS source_url,
            na.published_at,
            c.ticker_count,
            c.cluster_tickers
        FROM title_clusters c
        JOIN news_articles na ON na.article_id = c.sample_article_id
        ORDER BY c.ticker_count DESC, na.published_at DESC
        LIMIT $1
        """,
        n,
    )
    return [dict(r) for r in rows]


async def fetch_random_sample(pool: asyncpg.Pool, n: int, exclude_ids: set[str]) -> list[dict]:
    """recent 5 days, stratified by event_type."""
    rows = await pool.fetch(
        """
        SELECT
            na.article_id,
            na.title,
            na.body,
            na.ticker AS crawler_ticker,
            na.source_url::text AS source_url,
            na.published_at,
            ne.event_type
        FROM news_articles na
        JOIN news_events ne
            ON ne.article_id = na.article_id AND ne.ticker = na.ticker
        WHERE na.published_at > NOW() - INTERVAL '5 days'
          AND NOT (na.article_id = ANY($1::text[]))
        ORDER BY RANDOM()
        LIMIT $2
        """,
        list(exclude_ids),
        n,
    )
    return [dict(r) for r in rows]


async def load_stock_masters(pool: asyncpg.Pool) -> dict[str, str]:
    """{stock_name → stock_code}. KOSPI/KOSDAQ universe 의 모든 종목."""
    rows = await pool.fetch(
        "SELECT stock_code, stock_name FROM stock_masters WHERE stock_name IS NOT NULL"
    )
    return {r["stock_name"]: r["stock_code"] for r in rows}


# ---------------------------------------------------------------------
# LLM 호출
# ---------------------------------------------------------------------


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_agent_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    candidates = [cleaned]
    m = _JSON_OBJ_RE.search(cleaned)
    if m:
        candidates.append(m.group(0))
    for s in candidates:
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


async def agent_call(
    *,
    title: str,
    body: str,
    model: str,
    api_base: str | None = None,
    api_key: str | None = None,
    timeout: float = 60.0,
) -> dict | None:
    prompt = PROMPT_TEMPLATE.format(title=title, body=body or "(본문 없음)")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "timeout": timeout,
    }
    if api_base:
        kwargs["api_base"] = api_base
    if api_key:
        kwargs["api_key"] = api_key
    try:
        resp = await litellm.acompletion(**kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent_call %s failed: %s", model, e)
        return None
    content = resp["choices"][0]["message"]["content"]
    parsed = _parse_agent_json(content)
    if parsed is None:
        logger.warning("agent_call parse failed model=%s raw=%s", model, content[:200])
    return parsed


# ---------------------------------------------------------------------
# Metric
# ---------------------------------------------------------------------


_COMMON_SUFFIXES = (
    "에너지솔루션",
    "전자",
    "전기",
    "물산",
    "중공업",
    "건설",
    "건설기계",
    "케미칼",
    "이노베이션",
    "하이닉스",
    "디스플레이",
    "바이오로직스",
    "바이오에피스",
    "에너지",
    "텔레콤",
    "증권",
    "은행",
    "지주",
    "금융",
    "ELECTRIC",
    "ENM",
    "쇼핑",
    "스토어",
    "타이어",
    "화학",
    "제약",
    "엔터테인먼트",
    "유플러스",
)


def _aliases_for(stock_name: str) -> list[str]:
    """stock_name 에서 약어 후보 생성.

    예: '삼성전자' → ['삼성전자', '삼전', '삼성']
        'SK하이닉스' → ['SK하이닉스', '하이닉스', 'SK하닉', '하닉']
        'LG에너지솔루션' → ['LG에너지솔루션', '엔솔', 'LG엔솔']
    """
    out = [stock_name]
    # 공통 접미사 stripped 형태 (그룹+업종 접미사 → 그룹 단독)
    for sfx in _COMMON_SUFFIXES:
        if stock_name.endswith(sfx) and len(stock_name) > len(sfx):
            stem = stock_name[: -len(sfx)]
            if stem and stem not in out:
                out.append(stem)
    # 자주 쓰이는 줄임말 사전 (시장 실측 기준 — 확장 여지)
    known_short = {
        "삼성전자": ["삼전"],
        "SK하이닉스": ["하이닉스", "하닉", "SK하닉"],
        "LG에너지솔루션": ["엔솔", "LG엔솔"],
        "삼성SDI": ["SDI"],
        "삼성바이오로직스": ["삼바", "삼성바이오"],
        "현대차": ["현대"],
        "기아": ["기아차"],
        "현대모비스": ["모비스"],
        "KB금융": ["KB"],
        "신한지주": ["신한", "신한금융"],
        "하나금융지주": ["하나", "하나금융"],
        "우리금융지주": ["우리", "우리금융"],
        "NH투자증권": ["NH투자", "농협증권"],
        "엔씨소프트": ["엔씨", "NC"],
        "POSCO홀딩스": ["POSCO", "포스코"],
        "POSCO퓨처엠": ["퓨처엠"],
        "셀트리온": [],
        "두산밥캣": ["밥캣"],
        "한화에어로스페이스": ["한화에어로", "한화우주"],
        "한국전력": ["한전"],
        "KT&G": ["KT앤지", "케이티앤지"],
        "S-Oil": ["에쓰오일", "S오일"],
    }
    if stock_name in known_short:
        out.extend(known_short[stock_name])
    # 중복 제거
    seen: set[str] = set()
    deduped = []
    for a in out:
        if a not in seen:
            seen.add(a)
            deduped.append(a)
    return deduped


def _matches_text(agent_name: str, text: str, stock_name: str | None) -> bool:
    """agent 가 emit 한 ticker 이름이 본문 어딘가에 등장하는지.

    1) agent_name 그대로 substring
    2) name_to_code 매칭되는 stock_name 의 모든 alias 중 하나라도 substring
    """
    if agent_name in text:
        return True
    candidates: list[str] = []
    # agent 가 약어 emit 시 (예: "삼전") 호출부가 stock_name 으로 정식명 전달
    if stock_name:
        candidates.extend(_aliases_for(stock_name))
    else:
        candidates.extend(_aliases_for(agent_name))
    return any(a and a in text for a in candidates)


def title_coverage_rate(
    items: list[dict], name_to_code: dict[str, str]
) -> tuple[float, list[dict]]:
    """Agent ticker(name) 가 article.title+body 에 substring/alias 매칭 비율."""
    total = 0
    covered = 0
    misses: list[dict] = []
    # name → code 외에 약어 → 정식명 dictionary 도 구축
    alias_to_name: dict[str, str] = {}
    for nm in name_to_code:
        for a in _aliases_for(nm):
            alias_to_name.setdefault(a, nm)
    for item in items:
        agent_tickers = (item.get("agent_result") or {}).get("tickers") or []
        if not agent_tickers:
            continue
        text = (item.get("title") or "") + " " + (item.get("body") or "")
        for tn in agent_tickers:
            total += 1
            # agent 가 정식명 줬을 때 그 종목의 alias 확인. 약어 줬으면 alias 사전으로 역추적
            stock_name = tn if tn in name_to_code else alias_to_name.get(tn)
            if _matches_text(tn, text, stock_name):
                covered += 1
            else:
                misses.append(
                    {
                        "article_id": item["article_id"],
                        "title": item["title"],
                        "agent_ticker": tn,
                    }
                )
    rate = covered / total if total else 0.0
    return rate, misses


def cluster_reduction_stat(forced_items: list[dict]) -> dict:
    """forced FP cluster 의 Agent 출력 ticker 수 평균 + 원 cluster 크기 평균."""
    agent_counts: list[int] = []
    crawler_counts: list[int] = []
    for it in forced_items:
        agent = (it.get("agent_result") or {}).get("tickers") or []
        agent_counts.append(len(agent))
        crawler_counts.append(int(it.get("ticker_count") or 0))
    return {
        "agent_avg_tickers": sum(agent_counts) / len(agent_counts) if agent_counts else 0.0,
        "crawler_avg_tickers": sum(crawler_counts) / len(crawler_counts) if crawler_counts else 0.0,
        "per_item": [
            {
                "article_id": it["article_id"],
                "title": it["title"][:80],
                "crawler_count": int(it.get("ticker_count") or 0),
                "agent_count": len((it.get("agent_result") or {}).get("tickers") or []),
                "agent_tickers": (it.get("agent_result") or {}).get("tickers") or [],
            }
            for it in forced_items
        ],
    }


def cross_llm_overlap(items: list[dict]) -> dict:
    """Qwen3 ↔ DeepSeek tickers Jaccard. 둘 다 빈 배열이면 일치 (1.0)."""
    scores: list[float] = []
    per_item: list[dict] = []
    for it in items:
        if "deepseek_result" not in it:
            continue
        a = set((it.get("agent_result") or {}).get("tickers") or [])
        b = set((it.get("deepseek_result") or {}).get("tickers") or [])
        if not a and not b:
            score = 1.0
        elif not a or not b:
            score = 0.0
        else:
            score = len(a & b) / len(a | b)
        scores.append(score)
        per_item.append(
            {
                "article_id": it["article_id"],
                "title": it["title"][:80],
                "agent_tickers": sorted(a),
                "deepseek_tickers": sorted(b),
                "jaccard": round(score, 3),
            }
        )
    return {
        "avg_jaccard": sum(scores) / len(scores) if scores else 0.0,
        "per_item": per_item,
    }


def crawler_overlap_rate(items: list[dict], name_to_code: dict[str, str]) -> dict:
    """Agent tickers (name) → code 변환 → crawler ticker 포함 비율."""
    contains_crawler = 0
    has_agent = 0
    code_to_name = {v: k for k, v in name_to_code.items()}
    detail: list[dict] = []
    for it in items:
        agent_names = (it.get("agent_result") or {}).get("tickers") or []
        agent_codes = {name_to_code.get(n) for n in agent_names if name_to_code.get(n)}
        crawler_code = it.get("crawler_ticker")
        if agent_names:
            has_agent += 1
            if crawler_code in agent_codes:
                contains_crawler += 1
        detail.append(
            {
                "article_id": it["article_id"],
                "title": it["title"][:80],
                "crawler_ticker": crawler_code,
                "crawler_name": code_to_name.get(crawler_code or ""),
                "agent_tickers": agent_names,
                "agent_codes": sorted(c for c in agent_codes if c),
            }
        )
    rate = contains_crawler / has_agent if has_agent else 0.0
    return {"rate": rate, "with_agent": has_agent, "contains_crawler": contains_crawler}


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------


async def run_agent_calls(
    items: list[dict],
    *,
    model: str,
    api_base: str | None,
    api_key: str | None = None,
    concurrency: int = 4,
    key_label: str = "agent_result",
) -> None:
    """병렬도 제한된 LLM 호출. 결과는 item[key_label] 에 in-place 저장."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(it: dict) -> None:
        async with sem:
            res = await agent_call(
                title=it["title"],
                body=it["body"] or "",
                model=model,
                api_base=api_base,
                api_key=api_key,
            )
            it[key_label] = res

    t0 = time.time()
    await asyncio.gather(*(_one(it) for it in items))
    logger.info(
        "%s done: %d items in %.1fs",
        key_label,
        sum(1 for it in items if it.get(key_label) is not None),
        time.time() - t0,
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--random", type=int, default=50)
    parser.add_argument("--forced", type=int, default=10)
    parser.add_argument(
        "--cross-llm-count",
        type=int,
        default=10,
        help="Metric 3 (DeepSeek 호출) 샘플 수. 0 이면 skip",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get(
            "VLLM_LLM_MODEL", "jart25/Qwen3-30B-A3B-Instruct-2507-Autoround-Int-4bit-gptq"
        ),
        help="Qwen3 모델 (litellm prefix 자동 추가)",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("VLLM_LLM_URL", "http://vllm-llm:8001/v1"),
    )
    parser.add_argument(
        "--cross-model",
        default="deepseek/deepseek-chat",
        help="Cross-LLM model (litellm format)",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path, default=None, help="JSON detail output")
    parser.add_argument(
        "--analyses-doc",
        type=Path,
        default=Path(".ai/analyses/2026-05-23-news-agent-preflight.md"),
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    random.seed(args.seed)

    # Qwen3 model 에 hosted_vllm/ prefix 자동 부착 (litellm convention)
    qwen_model = args.model
    if not qwen_model.startswith("hosted_vllm/"):
        qwen_model = f"hosted_vllm/{qwen_model}"

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if args.cross_llm_count > 0 and not deepseek_key:
        logger.warning("DEEPSEEK_API_KEY 없음 → Metric 3 skip")
        args.cross_llm_count = 0

    pool = await asyncpg.create_pool(dsn=_pg_dsn(), min_size=1, max_size=4)
    try:
        assert pool is not None
        logger.info("loading stock_masters...")
        name_to_code = await load_stock_masters(pool)
        logger.info("stock_masters loaded: %d names", len(name_to_code))

        logger.info("fetching forced FP cluster: n=%d", args.forced)
        forced_items = await fetch_forced_fp(pool, args.forced)
        logger.info("forced FP fetched: %d", len(forced_items))

        exclude_ids = {it["article_id"] for it in forced_items}
        logger.info("fetching random sample: n=%d", args.random)
        random_items = await fetch_random_sample(pool, args.random, exclude_ids)
        logger.info("random fetched: %d", len(random_items))
    finally:
        await pool.close()

    all_items = list(forced_items) + list(random_items)
    logger.info("Agent (Qwen3) calls: %d items, concurrency=%d", len(all_items), args.concurrency)
    await run_agent_calls(
        all_items,
        model=qwen_model,
        api_base=args.api_base,
        concurrency=args.concurrency,
        key_label="agent_result",
    )

    cross_items: list[dict] = []
    if args.cross_llm_count > 0:
        # 우선 forced FP cluster 에서 cross-LLM 비교 (가장 신호 강함)
        cross_items = list(forced_items[: args.cross_llm_count])
        logger.info("Cross-LLM (DeepSeek) calls: %d items", len(cross_items))
        await run_agent_calls(
            cross_items,
            model=args.cross_model,
            api_base=None,
            api_key=deepseek_key,
            concurrency=4,
            key_label="deepseek_result",
        )

    # Metric 계산
    tc_rate, tc_misses = title_coverage_rate(all_items, name_to_code)
    cluster_stat = cluster_reduction_stat(forced_items)
    cross_stat = cross_llm_overlap(cross_items) if cross_items else None
    crawler_stat = crawler_overlap_rate(all_items, name_to_code)

    # Gate 판정
    gate_passed = tc_rate >= 0.95 and cluster_stat["agent_avg_tickers"] <= 1.5

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sample": {
            "forced_fp": len(forced_items),
            "random": len(random_items),
            "total": len(all_items),
            "cross_llm": len(cross_items),
        },
        "metric_1_title_coverage": {
            "rate": round(tc_rate, 3),
            "gate": 0.95,
            "passed": tc_rate >= 0.95,
            "misses": tc_misses[:20],
        },
        "metric_2_cluster_reduction": {
            **cluster_stat,
            "gate_avg_tickers_max": 1.5,
            "passed": cluster_stat["agent_avg_tickers"] <= 1.5,
        },
        "metric_3_cross_llm": cross_stat,
        "metric_4_crawler_overlap": crawler_stat,
        "gate_passed": gate_passed,
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as f:
            json.dump(
                {"summary": summary, "items": all_items},
                f,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        logger.info("detail JSON written: %s", args.out)

    if args.analyses_doc:
        args.analyses_doc.parent.mkdir(parents=True, exist_ok=True)
        _write_analyses_doc(args.analyses_doc, summary, args)
        logger.info("analyses doc written: %s", args.analyses_doc)

    return 0 if gate_passed else 1


def _write_analyses_doc(path: Path, summary: dict, args: argparse.Namespace) -> None:
    """Markdown analyses doc 출력."""
    s = summary
    lines = []
    lines.append(f"# News Agent Pre-flight — 측정 결과 ({s['generated_at']})\n")
    lines.append(
        "> design `.ai/designs/2026-05-23-news-agent.md` §5 의 자동 측정 protocol.\n"
        "> 사람 ground truth 없음 — proxy metric. ±5%p noise.\n"
    )
    lines.append("## 0. 샘플\n")
    lines.append(f"- forced FP cluster: {s['sample']['forced_fp']}건")
    lines.append(f"- random (5거래일): {s['sample']['random']}건")
    lines.append(f"- total: {s['sample']['total']}건")
    lines.append(f"- cross-LLM (DeepSeek): {s['sample']['cross_llm']}건\n")
    lines.append(f"- Agent model: `{args.model}`")
    lines.append(f"- vLLM endpoint: `{args.api_base}`\n")

    m1 = s["metric_1_title_coverage"]
    lines.append("## Metric 1 — Title-coverage rate\n")
    lines.append(
        f"- **{m1['rate']:.3f}** (gate ≥ {m1['gate']}) → {'PASS' if m1['passed'] else 'FAIL'}\n"
    )
    if m1["misses"]:
        lines.append("### Miss 사례 (Agent 가 emit 한 ticker 가 본문에 없음)\n")
        for mi in m1["misses"][:10]:
            lines.append(
                f"- `{mi['article_id']}` ticker=**{mi['agent_ticker']}** title={mi['title'][:60]}"
            )
        lines.append("")

    m2 = s["metric_2_cluster_reduction"]
    lines.append("## Metric 2 — Cluster reduction\n")
    lines.append(
        f"- Agent 출력 ticker 수 평균 **{m2['agent_avg_tickers']:.2f}** "
        f"(원 cluster 평균 {m2['crawler_avg_tickers']:.2f}, gate ≤ {m2['gate_avg_tickers_max']}) "
        f"→ {'PASS' if m2['passed'] else 'FAIL'}\n"
    )
    lines.append("| article_id | title | crawler→agent | agent_tickers |")
    lines.append("|---|---|---|---|")
    for pi in m2["per_item"]:
        agent_tickers = ", ".join(pi["agent_tickers"])
        delta = f"{pi['crawler_count']}→{pi['agent_count']}"
        lines.append(f"| `{pi['article_id']}` | {pi['title'][:50]} | {delta} | {agent_tickers} |")
    lines.append("")

    if s["metric_3_cross_llm"]:
        m3 = s["metric_3_cross_llm"]
        lines.append("## Metric 3 — Cross-LLM sanity (Qwen3 ↔ DeepSeek)\n")
        lines.append(f"- avg Jaccard **{m3['avg_jaccard']:.3f}** (목표 ≥ 0.80)\n")
        lines.append("| article_id | title | Agent (Qwen3) | DeepSeek | jaccard |")
        lines.append("|---|---|---|---|---|")
        for pi in m3["per_item"]:
            qwen = ", ".join(pi["agent_tickers"])
            ds = ", ".join(pi["deepseek_tickers"])
            lines.append(
                f"| `{pi['article_id']}` | {pi['title'][:40]} | {qwen} | {ds} | {pi['jaccard']} |"
            )
        lines.append("")

    m4 = s["metric_4_crawler_overlap"]
    lines.append("## Metric 4 — Crawler overlap (정보용)\n")
    lines.append(
        f"- Agent emit 한 sample {m4['with_agent']}건 중 crawler ticker 포함 비율: "
        f"**{m4['rate']:.3f}** ({m4['contains_crawler']}/{m4['with_agent']})\n"
    )

    lines.append("## Gate 판정\n")
    if s["gate_passed"]:
        lines.append(
            "**PASS** → Phase 1 (shadow) 진입 허용. 본 protocol 의 Agent 디자인을 정식으로 승격.\n"
        )
    else:
        lines.append("**FAIL** → prompt/parsing/샘플 재조정 후 재실행.\n")

    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
