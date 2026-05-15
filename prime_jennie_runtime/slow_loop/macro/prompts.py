"""Macro Gate 프롬프트.

MACRO_GATE_SPEC §4. system prompt + user prompt template. few-shot은 Phase 2
별도 디렉토리로 분리 예정.
"""

from __future__ import annotations

from .schemas import MacroContext

MACRO_SYSTEM_PROMPT = """당신은 Prime Jennie의 Macro Gate입니다. KOSPI/KOSDAQ 트레이딩 시스템의 거시 환경 게이트 역할을 합니다.

책임:
- 거시 환경을 종합 판단하여 오늘 매매 허용 여부(gate)와 포지션 크기 배수(size_multiplier)를 결정한다
- 판단 근거(reasoning)는 명확히 기록하지만, 이는 로깅 용도이며 실행 로직은 두 숫자(gate, size_multiplier)만으로 결정된다

중요 원칙:
1. 당신은 어드바이저가 아닙니다. 두 숫자로 게이트 역할만 합니다.
2. 불확실성을 reasoning으로 풀어쓰려 하지 말고, size_multiplier의 수치로 표현하십시오.
3. "상황을 지켜봐야 한다" 같은 유보적 표현 대신, 현재 가용 정보로 결정하십시오. 6시간 후 재검토는 next_review_hint에 적으십시오.

gate = "closed" 조건 (하나라도 충족 시 반드시 closed):
1. 지정학적 critical 이벤트 (한반도/중동/대만 군사 충돌 임박 또는 발생)
2. 유동성 경색 (호가 스프레드 2배 확대, 거래대금 50% 감소)
3. 섹터 전염 (주요 섹터 3개 이상 동시 -5% 이상 하락)
4. 환율 충격 (KRW/USD 일일 ±3% 이상)
5. 시스템 이벤트 (시스템 장애, 서킷브레이커)

1개 미만 충족 시 open. 이 조건은 엄격히 지킵니다.

국내 매크로 뉴스 해석 (`## 국내 매크로 뉴스` 섹션은 `[event_type/impact/sentiment score]` 태그):
- `impact=high` + `event_type ∈ {geopolitical, bankruptcy, market_movement}` 이 2건 이상이면 closed 조건 #1/#3/#5 재검토
- `impact=high` + `event_type=regulation` 으로 **섹터 전반 규제** 은 size_multiplier 를 한 단계 내리는 신호 (단 단일 종목 규제는 과민반응 금지)
- `impact=high` + `event_type ∈ {earnings, contract, shareholder_return}` positive 우세는 낙관 편향 근거 (size 1.00 으로 상향 검토)
- `event_type=other` 비중이 높으면 노이즈로 취급 — size 판단에 가중치 낮게
- sentiment_score 절댓값보다 event_type 과 impact 를 먼저 본다. "-0.3 earnings high" 는 작은 숫자지만 중요 신호.

size_multiplier 가이드 (open 은 2단계 — 0.75 / 1.0):
- 1.00: 거시 양호, 뚜렷한 상승 환경 또는 명확한 긍정 신호 우세
- 0.75: 중립 ~ 혼재 신호. open 의 기본값. 보수적이지만 매수는 진행
- 0.75 미만으로 내려갈 정도로 부정적이면 gate=closed 가 옳음 (출력은 연속값이지만 (0, 0.75] 은 모두 0.75 로 이산화됨)

중요 제약 — 모순 방지:
- gate="open" 과 size_multiplier=0.0 은 동시에 내보낼 수 없습니다. 크기가 0 이면 정의상 closed 입니다.
- 보수 신호 강할 땐 size 를 더 낮추는 것이 아니라 gate=closed 로 가십시오. open 은 "매수 진행" 의 의미가 명확해야 합니다.

top_risks는 최대 5개. 각 리스크는 category/severity/description 구조.
severity "critical"이 1개 이상이면 gate 판단에 강하게 반영.

reasoning은 500자 이내. 결론→근거 순서. 감정어 배제, 사실과 판단만.

과거 run 연속성 고려:
- 제공되는 최근 7일 run 이력을 참고하십시오
- 급격한 게이트 전환(전일 1.00 → 오늘 0.25)은 명확한 사유와 함께
- 반대로 상황이 명확히 개선되었는데도 관성적으로 낮은 multiplier를 유지하지 마십시오
"""


MACRO_PROMPT_VERSION = "v0.3"
"""Macro Gate 프롬프트 버전.

prompt 텍스트 또는 위 SYSTEM_PROMPT 의 의미론적 변경 시 bump. macro_runs.prompt_version
로 저장되어 모델 회귀 분석 시 동일 prompt 버전 끼리 비교 가능.

- v0.1 (2026-04-16): 초안
- v0.2 (2026-04-16): open+0.0 모순 명시, 이산화 가이드 보강
- v0.3 (2026-05-10): open 2단계 (0.75/1.0) 로 축소
"""


def build_user_prompt(ctx: MacroContext) -> str:
    """User prompt 조립."""
    snap = ctx.market_snapshot

    recent_str = (
        "\n".join(
            f"  - {r.macro_run_id}: gate={r.gate} size={r.size_multiplier:.2f} risks={r.top_risks_summary or '(없음)'}"
            for r in ctx.recent_runs[:7]
        )
        or "  (이력 없음)"
    )

    sector_str = (
        "\n".join(f"    - {s.sector}: {s.change_pct:+.2%}" for s in snap.major_sector_drops)
        or "    (섹터 스냅샷 없음)"
    )

    # 같은 호출 내 재시도 컨텍스트 — Scout 와 동일 패턴.
    retry_section = ""
    if ctx.previous_attempts:
        attempts_str = "\n\n".join(
            (f"### 시도 #{a.attempt_no} — 실패 사유: {a.error}\n세부:\n```\n{a.details}\n```")
            for a in ctx.previous_attempts
        )
        retry_section = f"""
## ⚠️ 직전 시도 실패 — 같은 실수 반복 금지
이전 시도에서 출력 구조화 / 검증이 실패했습니다. 아래 정보를 반드시 확인하고
같은 패턴을 피하십시오 (예: top_risks 6개 → 5개로, reasoning > 500자 → 짧게,
필수 필드 누락).

{attempts_str}

체크리스트:
- top_risks 는 최대 5개. 6개 이상이면 가장 약한 것 제거.
- reasoning 은 500자 이내. 결론→근거 순서.
- gate 는 "open" 또는 "closed" 만. 다른 값 금지.
- size_multiplier 는 0.0 ~ 1.0 사이.
- gate="open" 과 size_multiplier=0.0 동시 불가 (모순).

"""

    return f"""## 분석 기준 시각
{ctx.as_of.isoformat()}
Trigger: {ctx.trigger_reason}

## 시장 스냅샷

국내:
- KOSPI: {snap.kospi.close:.2f} ({snap.kospi.change_pct:+.2%})
- KOSDAQ: {snap.kosdaq.close:.2f} ({snap.kosdaq.change_pct:+.2%})

해외 (전일 / 현재 선물):
- S&P500: {snap.sp500.close:.2f} ({snap.sp500.change_pct:+.2%})
- Nasdaq: {snap.nasdaq.close:.2f} ({snap.nasdaq.change_pct:+.2%})
- Nikkei: {snap.nikkei.close:.2f} ({snap.nikkei.change_pct:+.2%})
- HSI: {snap.hsi.close:.2f} ({snap.hsi.change_pct:+.2%})

환율/원자재:
- USD/KRW: {snap.usd_krw:.2f} ({snap.usd_krw_change_pct:+.2%})
- Crude WTI: {snap.crude_oil:.2f} ({snap.crude_oil_change_pct:+.2%})
- Gold: {snap.gold:.2f} ({snap.gold_change_pct:+.2%})

공포 지수:
- VIX: {snap.vix:.2f} (전일 {snap.vix_prev if snap.vix_prev is not None else "N/A"})
- VKOSPI: {snap.vkospi if snap.vkospi is not None else "N/A"}

변동성:
- KOSPI 20d vol: {snap.kospi_20d_vol:.1%}
- KOSPI 60d vol: {snap.kospi_60d_vol:.1%}

섹터 하락 스냅샷 (closed 조건 참고):
{sector_str}

## WSJ News Digest
Digest ID: {ctx.wsj_digest_id}
{ctx.wsj_macro_summary}

주요 헤드라인:
{ctx.wsj_headlines_formatted}

## 국내 매크로 뉴스
{ctx.kor_macro_news_formatted}

## 최근 Macro Gate 이력
{recent_str}
{retry_section}
---

다음 JSON 형식으로만 응답하십시오. news_digest_ref는 "{ctx.wsj_digest_id}" 사용:

{{
  "gate": "open" | "closed",
  "size_multiplier": 0.0 ~ 1.0,
  "reasoning": "<500자 이내>",
  "top_risks": [{{"category": "...", "description": "...", "severity": "..."}}],
  "confidence": "high" | "medium" | "low",
  "news_digest_ref": "{ctx.wsj_digest_id}",
  "next_review_hint": "<없으면 null>"
}}
"""
