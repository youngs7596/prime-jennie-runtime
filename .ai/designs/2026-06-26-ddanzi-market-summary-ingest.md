# 딴지 증시 요약 수집 — 외부 큐레이션 매크로 맥락 (2026-06-26)

## 왜

딴지일보 자유게시판에 매일 두 번 올라오는 김경록 PB(딴지 아이디 `hisasimitsui`, 현재 닉 `정대만mitsui`)의
증시 요약을 prime-jennie 의 매크로 맥락 입력으로 받는다. 글은 미국장 마감 뒤 한국 개장 전(오전 6~7시)에
"미국 증시 요약", 한국장 마감 뒤(오후 5~6시)에 "한국 증시 요약"으로 올라온다. 지수 등락·핵심 이슈·종목
움직임·한국 증시 전망이 한 글에 정리돼 있어, 우리가 이미 받고 있는 WSJ 뉴스레터와 정확히 같은 부류의
큐레이션 매크로 요약이다. 한국어·한국 관점이라 WSJ 를 보완한다.

영석님이 6-25 글(`https://www.ddanzi.com/free/887282383`)로 추출 PoC 를 돌려 본문 전문이 로그인 없이
열리고 지수·이슈·종목·한국전망이 구조화 추출됨을 확인했다(2026-06-26).

## 결정 — WSJ 수집의 형제 잡

새 수집 잡이 딴지 글을 크롤·추출해 **기존 `global_macro_news_articles` 테이블에 `source='ddanzi_kimkr'`
로 upsert** 한다. 그러면 기존 07:30 `global_news_digest`(24시간 lookback)가 자동으로 흡수해
`global_macro_news_digests` 로 요약하고, `RealMacroNewsDigestFeeder`(36시간 윈도우)가 그 요약을 읽어
08:30 첫 매크로 run 의 카운슬 맥락에 반영한다. **새 테이블·새 피더·마이그레이션이 없다.** WSJ 와 같은
길에 소스 하나를 더 얹는 것뿐이다. 텔레그램 한국어 요약 발송도 WSJ 의 dedup 패턴을 그대로 재사용한다.

## 정체성 — 하드 입력이 아니라 맥락·검증 거울

이건 한 개인이 손으로 쓰는 글이다. 트레이딩 결정에 직접 들어가는 하드 입력으로 쓰지 않는다. 이유는
세 가지다. 안 올리거나 늦거나 포맷이 바뀔 수 있고(사람), 수치를 틀리거나 편향될 수 있으며(주관),
WSJ 도 게이트의 결정론 입력이 아니라 카운슬 맥락으로만 들어간다(전례). 그래서 매크로 피더의 "36시간
내 digest 없으면 사실 기반 fallback" 강등 구조에 그대로 태운다 — 결측을 허용한다.

오히려 이 글의 값어치는 **검증 거울**에 있다. 글에 적힌 지수·환율·한국 신호(필반 +3.59%, MSCI 한국
+3.92%, 달러원 1,544)를 우리가 자체 수집하는 `us_market_daily`·FX·수급과 교차 대조하면, 사람 요약의
오차와 우리 수집의 오차를 서로 비춰볼 수 있다. 2026-06-26 시총·수급 단위 100배 버그에서 얻은 결론과
같은 결이다 — 외부 큐레이션은 맥락으로 받고, 결정은 우리 정량 위에서 내린다.

## 수집 메커니즘 (확인된 사실)

- 딴지 자유게시판은 로그인 없이 글 목록·본문이 열린다(2026-06-26 실측).
- **글 발견은 옛 아이디 `hisasimitsui` 가 아니라 현재 닉 `정대만mitsui` 로 잡아야 한다.** 그 사람이 닉을
  바꾼 뒤 일별 요약을 새 닉으로 올려서, 옛 닉 검색은 3-25 이전 글까지만 나온다. 더 안정적인 길은
  회원 글목록(member_srl) 고정이다 — 닉이 또 바뀌어도 견딘다.
- 작성자 검색 URL: `?mid=free&search_target=nick_name&search_keyword=정대만mitsui` (또는 member_srl 글목록).
- 개별 글 URL: `https://www.ddanzi.com/free/{document_srl}` (예: 887282383).
- 제목 패턴: `📊 YYYY년 M월 D일 미국 증시 요약` / 한국 증시 요약. 게시 시각 미국=06:10 무렵, 한국=오후.

## 데이터 흐름

```
딴지 글목록(정대만mitsui, 제목 패턴 매칭) → 최신 미국/한국 요약 1건의 document_srl
  → 본문 HTML fetch → LLM 추출(아래 스키마 + 한국어 요약 headline)
  → global_macro_news_articles upsert (source='ddanzi_kimkr', published_at=게시시각, article_id=해시)
  → [기존] 07:30 global_news_digest 가 24h 윈도우로 흡수 → global_macro_news_digests
  → [기존] RealMacroNewsDigestFeeder(36h) → 08:30 매크로 카운슬
  → [선택] LLM 한국어 요약 → 텔레그램 (Redis dedup key: ddanzi:{market}:{date})
```

멱등은 두 겹이다. Redis dedup key(`ddanzi:sent:{market}:{date}`)로 하루 1회 발송을 보장하고,
`article_id = hash(source|document_srl)` 로 같은 글 재적재를 막는다(WSJ 와 동일).

## 두 슬롯·cron (평일)

| 글 | 게시 | 수집 cron(KST) | 닿는 곳 |
|---|---|---|---|
| 미국 증시 요약 | ~06:10 | `0 7 * * 1-5` (07:00) | WSJ(07:20)와 나란히 07:30 digest → 08:30 매크로 |
| 한국 증시 요약 | 17~18시 | `30 18 * * 1-5` (18:30) | 다음날 07:30 digest 가 24h 안이라 자연 포함 |

미국 요약은 개장 전 매크로 입력으로 WSJ 와 같은 슬롯에 들어간다. 한국 요약은 그날 한국장 복기라
다음날 아침 digest 에 함께 섞여 들어간다(별도 저녁 브리핑 UI 는 비스코프).

## 추출 스키마 (PoC 검증됨)

```json
{
  "source": "ddanzi_kimkr", "document_srl": "887282383",
  "title": "📊 2026년 6월 25일 미국 증시 요약",
  "summary_date": "2026-06-25", "market": "US", "posted_at_kst": "06:10",
  "headline": "마이크론 +15.7% 급등 vs 애플 -6.1% 급락 — 반도체→하드웨어 이익 재분배 충돌",
  "indices": {"DOW": 0.14, "NASDAQ": -0.46, "SP500": -0.01, "RUSSELL2000": 0.71, "SOX": 3.59},
  "macro": {"PCE_YoY": 4.07, "core_PCE_YoY": 3.41, "USDKRW_NDF": 1544, "MSCI_Korea": 3.92},
  "key_issues": [{"topic": "...", "tickers": ["MU"], "detail": "..."}],
  "korea_outlook": {"bias": "...", "signals": ["..."]}
}
```

종목명이 한글·구어체라 미국 티커 매핑은 맥락 용도엔 불필요해 보류한다(비스코프). "소폭"·"−1%대" 같은
정성 표현은 그대로 보존한다 — 맥락엔 무방하고 정밀 수치가 필요한 자리엔 안 쓴다.

## 실패 모드·가드

- **닉 재변경**: member_srl 글목록을 1차로 쓰고 닉 검색은 폴백. 둘 다 실패하면 그날 결측(허용).
- **미게시·지연**: 수집 시각에 당일 글이 없으면 조용히 skip(결측 허용). 하드 의존 금지라 게이트는 fallback.
- **본문 구조 변화**: HTML 셀렉터 실패 시 원문 텍스트를 통째로 저장하고 LLM 추출만 best-effort.
- **추출 LLM 실패**: 구조화 실패해도 원문·headline 은 보존해 digest 가 최소한 텍스트로 흡수.
- **단일 소스 편향**: 검증 거울로만. 글 수치와 우리 수집값이 크게 어긋나면 경보(향후).

## ToS·저작권

한 개인의 글을 영석님 본인 판단용으로 사적 이용한다(텔레그램도 영석님만 수신). 정당하다. 단 원문을
외부에 재배포하지 않는다. 폴링은 하루 1~2회(슬롯당 1회)로 점잖게, robots/ToS 존중.

## Pre-flight (배포 전 측정)

1. 살아있는 미국·한국 요약 각 5건 이상을 수기로 추출해, 지수·핵심 수치가 본문과 일치하는지 점검.
2. 같은 날 글의 미국 지수·환율을 우리 `us_market_daily`·FX 와 대조해 사람 요약의 오차 분포를 본다
   (검증 거울로서 신뢰 가능한지).
3. 최근 2~3주 글에서 닉(`정대만mitsui`)·제목 패턴(`... 미국/한국 증시 요약`)이 안정적인지, member_srl
   글목록이 더 견고한지 확인.
4. 07:00 수집 → 07:30 digest 흡수가 실제로 되는지 스테이징 1회 검증(WSJ 처럼 digest 가 source 무관
   24h 흡수임을 재확인).

## 구현 조각 (마이그레이션 불요)

- `jobs/crawlers/ddanzi.py` — 글목록(닉/member_srl + 제목 패턴) → 최신 srl → 본문 fetch·파싱.
- `jobs/ddanzi_ingest.py` — WSJ 형제 잡. fetch → LLM 추출 → `news_pipeline_global.upsert_articles`
  로 `global_macro_news_articles` upsert(source 태그) → 텔레그램 요약(dedup).
- `jobs/app.py` 핸들러 2개(`ddanzi_ingest_us`, `ddanzi_ingest_kr`) + `scheduled_jobs` cron 2개.
- 추출·요약 LLM 은 기존 `briefing.reporter.LLMCaller` 재사용(WSJ 와 동일 티어 정책).

## 비스코프

종목명→티커 매핑과 per-stock 신호화, 한국 요약 전용 저녁 브리핑 UI, 게이트 결정론 하드 입력, 글
수치 자동 경보(검증 거울 1차는 수기 점검으로 충분). 필요해지면 후속 설계.
