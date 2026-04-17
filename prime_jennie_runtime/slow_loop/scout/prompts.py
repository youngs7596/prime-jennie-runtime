"""Scout 프롬프트 — system + user template.

SCOUT_CODE_GENERATION §5. 허용 import 리스트, 안티패턴, structured output 포맷 명시.
Few-shot 예제는 아직 별도 파일로 분리하지 않음 (Phase 2에서 few_shots/v{N}/로).
"""

from __future__ import annotations

from .schemas import ScoutContext

ALLOWED_IMPORTS = (
    "pandas",
    "numpy",
    "scipy.stats",
    "talib",
    "sklearn.cluster",
    "sklearn.linear_model",
    "sklearn.preprocessing",
    "sklearn.metrics",
    "math",
    "statistics",
    "datetime",
)

FORBIDDEN_PATTERNS = (
    "하드코딩된 ticker (예: if ticker == '005930')",
    "미래 데이터 참조 (예: df.shift(-1))",
    "전역 상태 변조",
    "재귀 호출",
    "외부 네트워크 호출 시도",
    "eval, exec, compile 사용",
    "__import__ 동적 import",
    "try/except로 에러 삼키기",
)

SCOUT_SYSTEM_PROMPT = f"""당신은 Prime Jennie의 Scout입니다. KOSPI/KOSDAQ 종목 스크리닝을 위한 Python 코드를 생성합니다.

역할:
- 시장 데이터를 분석하여 매수 후보를 필터링하는 `screen` 함수를 작성한다
- 자연어로 종목을 추천하지 않는다. 오직 코드로 표현한다
- 가설(hypothesis)은 한 문단으로 명확히 기술한다 (200자 이내)

제약:
1. 허용 import만 사용: {", ".join(ALLOWED_IMPORTS)}
2. 네트워크, 파일 I/O, 프로세스, 스레드 관련 모듈 금지
3. eval, exec, compile, __import__ 금지
4. 하드코딩된 ticker 금지. 팩터 기반 필터링만.
5. 미래 데이터 참조 금지 (shift(-n), lookahead)
6. try/except로 에러 숨기기 금지. 의도된 실패는 명시적으로 처리.

생성 코드 요구사항:
- `def screen(market_data: pd.DataFrame, context: dict) -> list[dict]` 시그니처
- 반환 0 ~ 20개
- ticker 중복 금지, universe 밖 ticker 금지
- conviction은 0.0 ~ 1.0

context dict 구조:
- as_of: date
- universe: list[str]
- news_scores: dict[str, dict]       (ticker → NewsScoreEntry dict)
- sector_momentum: dict[str, float]
- consensus_data: pd.DataFrame        (인덱스 ticker, 컨센서스 스냅샷)
- macro_size_multiplier: float        (참고용)

품질 기준:
- 최소 2개 이상의 독립 팩터 결합
- 뉴스 감성은 stale(48시간 초과) 데이터 의존 금지
- 컨센서스는 애널리스트 수 확보된 ticker만 사용 권장
- 한 종목에 다 걸지 말고 섹터 분산 고려
- fallback_strategy 명시 (통과 종목 0개 대응)

안티패턴 (절대 금지):
{chr(10).join(f"  - {p}" for p in FORBIDDEN_PATTERNS)}

당신의 최근 성과는 provided previous_runs에 있다. 과거 실패 패턴을 피한다.
"""


def build_user_prompt(ctx: ScoutContext) -> str:
    """User prompt 조립."""
    top_sectors = sorted(ctx.sector_momentum.items(), key=lambda x: x[1], reverse=True)[:5]
    bottom_sectors = sorted(ctx.sector_momentum.items(), key=lambda x: x[1])[:5]

    sector_top_str = "\n".join(f"  - {s}: {m:+.2%}" for s, m in top_sectors) or "  (없음)"
    sector_bot_str = "\n".join(f"  - {s}: {m:+.2%}" for s, m in bottom_sectors) or "  (없음)"

    # 뉴스 상위 20 (score 내림차순)
    news_items = sorted(ctx.news_scores.items(), key=lambda x: x[1].score, reverse=True)[:20]
    news_str = (
        "\n".join(
            f"  - {t}: score={e.score:+.2f} staleness={e.staleness_hours:.1f}h"
            for t, e in news_items
        )
        or "  (뉴스 없음)"
    )

    prev_str = (
        "\n".join(
            f"  - {r.scout_run_id} | {r.hypothesis} | cands={r.candidate_count}"
            for r in ctx.previous_scout_runs[:5]
        )
        or "  (이전 run 없음)"
    )

    return f"""다음 시장 상황에서 스크리닝 코드를 생성하십시오.

## 분석 기준일
{ctx.as_of.isoformat()}
Trigger: {ctx.trigger_reason}

## 시장 요약
- KOSPI: {ctx.market_summary.kospi_close:.2f} ({ctx.market_summary.kospi_change_pct:+.2%})
- KOSDAQ: {ctx.market_summary.kosdaq_close:.2f} ({ctx.market_summary.kosdaq_change_pct:+.2%})
- 상승종목 / 하락종목: {ctx.market_summary.up_count} / {ctx.market_summary.down_count}

## Macro Gate 상태
- Gate: {ctx.macro_state.gate}
- Size multiplier: {ctx.macro_state.size_multiplier}
- Top risks: {ctx.macro_state.top_risks_summary or "(없음)"}

## 섹터 모멘텀 (상위 5 / 하위 5)
{sector_top_str}
---
{sector_bot_str}

## 뉴스 감성 상위 20종목
{news_str}

## 최근 Scout run 요약 (최신 5개)
{prev_str}

## 사용 가능한 strategy_tag
{", ".join(ctx.strategy_tags_available)}

## Universe 크기
{len(ctx.universe)} 종목

---

다음 형식의 JSON으로만 응답하십시오.

{{
  "screening_code": "<Python 코드>",
  "hypothesis": "<200자 이내 자연어 가설>",
  "expected_candidates": <정수 0~20>,
  "factor_weights": {{"팩터명": 가중치, ...}},
  "strategy_tags_used": ["<tag>"],
  "fallback_strategy": "skip_today" | "use_previous_run" | "relax_filters",
  "estimated_runtime_seconds": <숫자>
}}
"""
