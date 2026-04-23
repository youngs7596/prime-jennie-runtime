"""pure formatter 테스트 — LLM/DB 없음."""

from __future__ import annotations

from prime_jennie_runtime.briefing.formatters import (
    build_llm_context,
    compute_trade_summary,
    format_fallback_html,
)


def _sample_data(**overrides) -> dict:
    data = {
        "date": "2026-04-17",
        "positions": [
            {
                "stock_code": "005930",
                "stock_name": "삼성전자",
                "quantity": 10,
                "avg_price": 71000,
                "total_buy": 710000,
            },
        ],
        "trades": [
            {
                "stock_code": "000660",
                "stock_name": "SK하이닉스",
                "trade_type": "BUY",
                "quantity": 5,
                "price": 150000,
                "total_amount": 750000,
                "reason": "momentum",
                "profit_pct": None,
                "profit_amount": None,
            },
            {
                "stock_code": "005930",
                "stock_name": "삼성전자",
                "trade_type": "SELL",
                "quantity": 3,
                "price": 72000,
                "total_amount": 216000,
                "reason": "tp",
                "profit_pct": 2.5,
                "profit_amount": 3000,
            },
            {
                "stock_code": "035720",
                "stock_name": "카카오",
                "trade_type": "SELL",
                "quantity": 2,
                "price": 40000,
                "total_amount": 80000,
                "reason": "sl",
                "profit_pct": -3.0,
                "profit_amount": -2400,
            },
        ],
        "macro": {
            "sentiment": "open",
            "sentiment_score": 1.0,
            "regime_hint": "high",
            "kospi_index": 2650.12,
            "kospi_change_pct": 0.5,
            "kosdaq_index": 855.0,
            "kosdaq_change_pct": -0.3,
            "risk_factors": ["미국 금리 인상", "반도체 수요 둔화"],
            "trading_reasoning": "선별적 매수",
        },
        "watchlist": [
            {
                "stock_code": "005930",
                "stock_name": "삼성전자",
                "hybrid_score": 78.5,
                "trade_tier": "A",
                "rank": 1,
            },
        ],
        "assets": {
            "total_asset": 10_000_000,
            "cash_balance": 3_000_000,
            "stock_eval": 7_000_000,
            "position_count": 1,
        },
        "news": [
            {"stock_code": "005930", "headline": "삼성전자 신제품 발표", "score": 0.8},
        ],
    }
    data["trade_summary"] = compute_trade_summary(data["trades"])
    data.update(overrides)
    return data


def test_compute_trade_summary_counts_and_win_rate():
    trades = [
        {"trade_type": "BUY", "profit_pct": None, "profit_amount": None, "stock_name": "A"},
        {"trade_type": "SELL", "profit_pct": 2.0, "profit_amount": 100, "stock_name": "B"},
        {"trade_type": "SELL", "profit_pct": -1.0, "profit_amount": -50, "stock_name": "C"},
        {"trade_type": "SELL", "profit_pct": 5.0, "profit_amount": 500, "stock_name": "D"},
    ]
    summary = compute_trade_summary(trades)
    assert summary["buy_count"] == 1
    assert summary["sell_count"] == 3
    assert summary["win_count"] == 2
    assert summary["loss_count"] == 1
    assert summary["total_realized_pnl"] == 550
    assert abs(summary["win_rate"] - (2 / 3 * 100)) < 1e-6
    assert summary["best_trade"]["stock_name"] == "D"
    assert summary["worst_trade"]["stock_name"] == "C"


def test_compute_trade_summary_no_sells():
    summary = compute_trade_summary(
        [{"trade_type": "BUY", "profit_pct": None, "profit_amount": None}]
    )
    assert summary["sell_count"] == 0
    assert summary["win_rate"] == 0.0
    assert summary["best_trade"] is None
    assert summary["worst_trade"] is None


def test_build_llm_context_includes_all_sections():
    data = _sample_data()
    ctx = build_llm_context(data)
    # 필수 섹션 헤더
    assert "[일일 브리핑 데이터] 2026-04-17" in ctx
    assert "== 자산 현황 ==" in ctx
    assert "== 오늘 매매 요약 ==" in ctx
    assert "== 매매 상세 ==" in ctx
    assert "== 보유 종목 (1개) ==" in ctx
    assert "== 시장 현황 ==" in ctx
    assert "== 워치리스트 Top 1 ==" in ctx
    assert "== 주요 뉴스 ==" in ctx
    # 값 렌더링 스팟체크
    assert "총자산: 10,000,000원" in ctx
    assert "매수: 1건 / 매도: 2건" in ctx
    assert "코스피: 2,650.12 (+0.50%)" in ctx
    assert "위험 요인: 미국 금리 인상, 반도체 수요 둔화" in ctx


def test_build_llm_context_handles_empty_data():
    """macro=None, positions=[], watchlist=[], assets=None 인 최소 dict 도 깨지지 않는다."""
    empty = {
        "date": "2026-04-17",
        "positions": [],
        "trades": [],
        "trade_summary": compute_trade_summary([]),
        "macro": None,
        "watchlist": [],
        "assets": None,
        "news": [],
    }
    ctx = build_llm_context(empty)
    assert "자산 데이터 없음" in ctx
    assert "오늘 매매 없음" in ctx
    assert "매크로 데이터 없음" in ctx


def test_format_fallback_html_uses_html_tags_only():
    data = _sample_data()
    html_out = format_fallback_html(data)
    assert "<b>[2026-04-17] 일일 브리핑</b>" in html_out
    assert "<b>자산 현황</b>" in html_out
    assert "<b>오늘 매매</b> (매수 1건 / 매도 2건)" in html_out
    assert "<b>보유 종목</b>" in html_out
    assert "<b>시장 현황</b>" in html_out
    # Markdown 헤딩/코드블럭 금지 — watchlist 의 "#1" 랭크는 허용
    for line in html_out.splitlines():
        assert not line.lstrip().startswith("# ")
    assert "```" not in html_out
    assert "**" not in html_out


def test_format_fallback_html_escapes_special_chars():
    data = _sample_data()
    data["positions"] = [
        {
            "stock_code": "A",
            "stock_name": "<script>alert('x')</script>",
            "quantity": 1,
            "avg_price": 100,
            "total_buy": 100,
        },
    ]
    html_out = format_fallback_html(data)
    assert "&lt;script&gt;" in html_out
    assert "<script>" not in html_out


def test_format_fallback_html_negative_pnl_shows_minus_sign():
    data = _sample_data()
    # 전부 손실인 매도 하나만
    data["trades"] = [
        {
            "stock_code": "A",
            "stock_name": "A",
            "trade_type": "SELL",
            "quantity": 1,
            "price": 1000,
            "total_amount": 1000,
            "reason": "sl",
            "profit_pct": -5.0,
            "profit_amount": -100,
        },
    ]
    data["trade_summary"] = compute_trade_summary(data["trades"])
    html_out = format_fallback_html(data)
    assert "실현손익: <b>-100원</b>" in html_out
