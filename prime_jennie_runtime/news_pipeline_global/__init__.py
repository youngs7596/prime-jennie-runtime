"""전역 매크로 뉴스 파이프라인 (Phase 2.13-1).

WSJ / Bloomberg / Reuters 등 해외 매크로 뉴스 RSS 를 수집해서
`global_macro_news_articles` 에 저장하고, 일일 LLM 요약을 만들어
`global_macro_news_digests` 에 저장한다. `RealWsjDigestFeeder` 가 이 digest 를 읽는다.

종목별 국내 뉴스는 별도 (news_pipeline_kor) — 파이프라인 목적과 스키마가 다르다.
"""

from __future__ import annotations
