"""브리핑 데이터 dict → LLM 입력 텍스트 / fallback HTML.

포팅 원본: prime-jennie/prime_jennie/services/briefing/reporter.py
- `_build_data_context` → `build_llm_context`
- `_format_fallback_html` → `format_fallback_html`
- `_compute_trade_summary` → `compute_trade_summary`

데이터 포맷. collect_briefing_data 가 채워주는 dict 구조:
  {
    "date": "YYYY-MM-DD",
    "positions": [{"stock_code", "stock_name", "quantity", "avg_price", "total_buy"}, ...],
    "trades": [{"stock_code", "stock_name", "trade_type", "quantity", "price",
                "total_amount", "reason", "profit_pct", "profit_amount"}, ...],
    "trade_summary": {...},
    "macro": {
        # 항상 존재
        "sentiment", "sentiment_score", "regime_hint",
        "kospi_index", "kospi_change_pct", "kosdaq_index", "kosdaq_change_pct",
        "risk_factors", "trading_reasoning", "next_review_hint",
        # Redis snapshot 에서 병합 (없으면 key 자체 생략):
        "vix", "vix_regime",
        "sox_close", "sox_change_pct", "nvda_close", "nvda_change_pct",
        "nikkei_close", "nikkei_change_pct", "hsi_close", "hsi_change_pct",
        "usd_jpy", "usd_jpy_change_pct", "usd_krw",  # usd_krw 는 BOK 키 있을 때만
        "crude_oil", "crude_oil_change_pct", "gold", "gold_change_pct",
        "kospi_foreign_net", "kospi_institutional_net", "kospi_retail_net",
    } | None,
    "watchlist": [{"stock_code", "stock_name", "hybrid_score", "trade_tier", "rank"}, ...],
    "assets": {"total_asset", "cash_balance", "stock_eval", "position_count"} | None,
    "news": [{"stock_code", "headline", "score"}, ...],
  }

sectors/council_consensus/key_themes 는 v3 에 수집 경로가 없어 여전히 제외.
"""

from __future__ import annotations

import html


def _safe(value: object) -> str:
    """HTML 특수문자 이스케이프."""
    return html.escape(str(value))


def _indexish(name: str, close: float | None, change_pct: float | None) -> str:
    """'이름: 1,234.56 (+0.12%)' 형태. change 없으면 변동률 생략."""
    change = f" ({change_pct:+.2f}%)" if change_pct is not None else ""
    return f"{name}: {close:,.2f}{change}"


def _format_kospi_flows(m: dict) -> str:
    """KOSPI 외국인/기관/개인 수급 한 줄 요약. 단위: 억원 (Naver 원본 동일)."""
    parts: list[str] = []
    foreign = m.get("kospi_foreign_net")
    inst = m.get("kospi_institutional_net")
    retail = m.get("kospi_retail_net")
    if foreign is not None:
        parts.append(f"외국인 {foreign:+,.0f}")
    if inst is not None:
        parts.append(f"기관 {inst:+,.0f}")
    if retail is not None:
        parts.append(f"개인 {retail:+,.0f}")
    return " | ".join(parts)


def compute_trade_summary(trade_data: list[dict]) -> dict:
    """매매 요약 통계 계산."""
    buys = [t for t in trade_data if t["trade_type"] == "BUY"]
    sells = [t for t in trade_data if t["trade_type"] == "SELL"]

    wins = [s for s in sells if (s.get("profit_pct") or 0) > 0]
    losses = [s for s in sells if (s.get("profit_pct") or 0) < 0]
    total_realized_pnl = sum(s.get("profit_amount") or 0 for s in sells)

    best_trade = None
    worst_trade = None
    if sells:
        sells_with_pct = [s for s in sells if s.get("profit_pct") is not None]
        if sells_with_pct:
            best_trade = max(sells_with_pct, key=lambda s: s["profit_pct"])
            worst_trade = min(sells_with_pct, key=lambda s: s["profit_pct"])

    sell_count = len(sells)
    win_rate = (len(wins) / sell_count * 100) if sell_count > 0 else 0.0

    return {
        "buy_count": len(buys),
        "sell_count": sell_count,
        "total_realized_pnl": total_realized_pnl,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": win_rate,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
    }


def build_llm_context(data: dict) -> str:
    """수집 데이터를 LLM 입력용 구조화 텍스트로 변환."""
    lines: list[str] = []
    lines.append(f"[일일 브리핑 데이터] {data['date']}")
    lines.append("")

    # 자산 현황
    lines.append("== 자산 현황 ==")
    if data.get("assets"):
        a = data["assets"]
        lines.append(f"총자산: {a['total_asset']:,}원")
        lines.append(f"현금: {a['cash_balance']:,}원")
        lines.append(f"주식평가: {a['stock_eval']:,}원")
        lines.append(f"보유종목수: {a['position_count']}개")
    else:
        lines.append("자산 데이터 없음")
    lines.append("")

    # 매매 요약
    lines.append("== 오늘 매매 요약 ==")
    ts = data.get("trade_summary", {})
    buy_count = ts.get("buy_count", 0)
    sell_count = ts.get("sell_count", 0)
    if buy_count + sell_count > 0:
        lines.append(f"매수: {buy_count}건 / 매도: {sell_count}건")
        if sell_count > 0:
            lines.append(f"실현손익: {ts['total_realized_pnl']:,}원")
            lines.append(
                f"승률: {ts['win_rate']:.0f}% "
                f"(익절 {ts['win_count']}건 / 손절 {ts['loss_count']}건)"
            )
            if ts.get("best_trade"):
                bt = ts["best_trade"]
                lines.append(f"최고수익: {bt['stock_name']} {bt['profit_pct']:+.1f}%")
            if ts.get("worst_trade"):
                wt = ts["worst_trade"]
                lines.append(f"최저수익: {wt['stock_name']} {wt['profit_pct']:+.1f}%")
    else:
        lines.append("오늘 매매 없음")
    lines.append("")

    # 매매 상세
    if data.get("trades"):
        lines.append("== 매매 상세 ==")
        for t in data["trades"]:
            pnl = f" ({t['profit_pct']:+.1f}%)" if t.get("profit_pct") else ""
            lines.append(
                f"{t['trade_type']} {t['stock_name']} {t['quantity']}주 "
                f"@{t['price']:,}원{pnl} [{t['reason']}]"
            )
        lines.append("")

    # 보유 종목
    if data.get("positions"):
        lines.append(f"== 보유 종목 ({len(data['positions'])}개) ==")
        for p in data["positions"]:
            lines.append(
                f"{p['stock_name']}({p['stock_code']}) {p['quantity']}주 @{p['avg_price']:,}원"
            )
        lines.append("")

    # 매크로/시장 지표
    lines.append("== 시장 현황 ==")
    if data.get("macro"):
        m = data["macro"]
        if m.get("kospi_index"):
            change = (
                f" ({m['kospi_change_pct']:+.2f}%)" if m.get("kospi_change_pct") is not None else ""
            )
            lines.append(f"코스피: {m['kospi_index']:,.2f}{change}")
        if m.get("kosdaq_index"):
            change = (
                f" ({m['kosdaq_change_pct']:+.2f}%)"
                if m.get("kosdaq_change_pct") is not None
                else ""
            )
            lines.append(f"코스닥: {m['kosdaq_index']:,.2f}{change}")
        if m.get("vix") is not None:
            regime = f" [{m['vix_regime']}]" if m.get("vix_regime") else ""
            lines.append(f"VIX: {m['vix']:.2f}{regime}")
        us_parts: list[str] = []
        if m.get("sox_close"):
            us_parts.append(_indexish("SOX", m.get("sox_close"), m.get("sox_change_pct")))
        if m.get("nvda_close"):
            us_parts.append(_indexish("NVDA", m.get("nvda_close"), m.get("nvda_change_pct")))
        if us_parts:
            lines.append(" | ".join(us_parts))
        asia_parts: list[str] = []
        if m.get("nikkei_close"):
            asia_parts.append(
                _indexish("닛케이", m.get("nikkei_close"), m.get("nikkei_change_pct"))
            )
        if m.get("hsi_close"):
            asia_parts.append(_indexish("항셍", m.get("hsi_close"), m.get("hsi_change_pct")))
        if asia_parts:
            lines.append(" | ".join(asia_parts))
        fx_parts: list[str] = []
        if m.get("usd_krw"):
            fx_parts.append(f"USD/KRW: {m['usd_krw']:,.1f}")
        if m.get("usd_jpy"):
            fx_parts.append(f"USD/JPY: {m['usd_jpy']:,.2f}")
        if fx_parts:
            lines.append(" | ".join(fx_parts))
        commodity_parts: list[str] = []
        if m.get("crude_oil"):
            commodity_parts.append(
                _indexish("원유", m.get("crude_oil"), m.get("crude_oil_change_pct"))
            )
        if m.get("gold"):
            commodity_parts.append(_indexish("금", m.get("gold"), m.get("gold_change_pct")))
        if commodity_parts:
            lines.append(" | ".join(commodity_parts))
        flows = _format_kospi_flows(m)
        if flows:
            lines.append(f"외국인 수급(KOSPI): {flows}")
        if m.get("sentiment") is not None:
            lines.append(f"심리: {m['sentiment']} (점수: {m.get('sentiment_score', '-')})")
        if m.get("regime_hint"):
            lines.append(f"국면: {m['regime_hint']}")
        if m.get("trading_reasoning"):
            lines.append(f"매매 근거: {m['trading_reasoning']}")
        if m.get("risk_factors"):
            factors = m["risk_factors"]
            if isinstance(factors, list):
                lines.append(f"위험 요인: {', '.join(str(f) for f in factors)}")
    else:
        lines.append("매크로 데이터 없음")
    lines.append("")

    # 워치리스트
    if data.get("watchlist"):
        lines.append(f"== 워치리스트 Top {len(data['watchlist'])} ==")
        for w in data["watchlist"]:
            rank = w["rank"] if w["rank"] is not None else "-"
            score = f"{w['hybrid_score']:.0f}" if w["hybrid_score"] is not None else "-"
            tier = w["trade_tier"] or "-"
            lines.append(f"#{rank} {w['stock_name']} ({score}점, {tier})")
        lines.append("")

    # 뉴스
    if data.get("news"):
        lines.append("== 주요 뉴스 ==")
        for n in data["news"]:
            lines.append(f"[{n['stock_code']}] {n['headline']} (감성: {n['score']})")
        lines.append("")

    return "\n".join(lines)


def format_fallback_html(data: dict) -> str:
    """LLM 실패 시 사용할 결정론 HTML 포맷 리포트."""
    lines: list[str] = []
    lines.append(f"<b>[{_safe(data['date'])}] 일일 브리핑</b>")
    lines.append("")

    # 자산 현황
    if data.get("assets"):
        a = data["assets"]
        lines.append("<b>자산 현황</b>")
        lines.append(f"총자산: <b>{a['total_asset']:,}원</b>")
        lines.append(f"현금: {a['cash_balance']:,}원 | 주식: {a['stock_eval']:,}원")
        lines.append(f"보유 종목: {a['position_count']}개")
        lines.append("")

    # 매매 요약
    ts = data.get("trade_summary", {})
    buy_count = ts.get("buy_count", 0)
    sell_count = ts.get("sell_count", 0)
    if buy_count + sell_count > 0:
        lines.append(f"<b>오늘 매매</b> (매수 {buy_count}건 / 매도 {sell_count}건)")
        if sell_count > 0:
            pnl = ts["total_realized_pnl"]
            pnl_sign = "+" if pnl >= 0 else ""
            lines.append(f"실현손익: <b>{pnl_sign}{pnl:,}원</b> | 승률: {ts['win_rate']:.0f}%")
        for t in data.get("trades", [])[:8]:
            pnl_str = f" ({t['profit_pct']:+.1f}%)" if t.get("profit_pct") else ""
            lines.append(
                f"  {_safe(t['trade_type'])} {_safe(t['stock_name'])} "
                f"{t['quantity']}주 @{t['price']:,}{pnl_str}"
            )
        lines.append("")
    else:
        lines.append("<b>오늘 매매</b>")
        lines.append("매매 없음")
        lines.append("")

    # 보유 종목
    if data.get("positions"):
        lines.append(f"<b>보유 종목</b> ({len(data['positions'])}개)")
        for p in data["positions"][:8]:
            lines.append(f"  {_safe(p['stock_name'])} {p['quantity']}주 @{p['avg_price']:,}")
        lines.append("")

    # 시장 현황
    if data.get("macro"):
        m = data["macro"]
        lines.append("<b>시장 현황</b>")
        parts: list[str] = []
        if m.get("kospi_index"):
            change = (
                f" ({m['kospi_change_pct']:+.2f}%)" if m.get("kospi_change_pct") is not None else ""
            )
            parts.append(f"코스피: {m['kospi_index']:,.2f}{change}")
        if m.get("kosdaq_index"):
            change = (
                f" ({m['kosdaq_change_pct']:+.2f}%)"
                if m.get("kosdaq_change_pct") is not None
                else ""
            )
            parts.append(f"코스닥: {m['kosdaq_index']:,.2f}{change}")
        if m.get("vix") is not None:
            regime = f" [{_safe(m['vix_regime'])}]" if m.get("vix_regime") else ""
            parts.append(f"VIX: {m['vix']:.2f}{regime}")
        if parts:
            lines.append(" | ".join(parts))
        asia_us_parts: list[str] = []
        if m.get("nikkei_close"):
            asia_us_parts.append(
                _indexish("닛케이", m.get("nikkei_close"), m.get("nikkei_change_pct"))
            )
        if m.get("hsi_close"):
            asia_us_parts.append(_indexish("항셍", m.get("hsi_close"), m.get("hsi_change_pct")))
        if m.get("sox_close"):
            asia_us_parts.append(_indexish("SOX", m.get("sox_close"), m.get("sox_change_pct")))
        if m.get("nvda_close"):
            asia_us_parts.append(_indexish("NVDA", m.get("nvda_close"), m.get("nvda_change_pct")))
        if asia_us_parts:
            lines.append(" | ".join(asia_us_parts))
        fx_cmd_parts: list[str] = []
        if m.get("usd_krw"):
            fx_cmd_parts.append(f"USD/KRW: {m['usd_krw']:,.1f}")
        if m.get("usd_jpy"):
            fx_cmd_parts.append(f"USD/JPY: {m['usd_jpy']:,.2f}")
        if m.get("crude_oil"):
            fx_cmd_parts.append(_indexish("원유", m["crude_oil"], m.get("crude_oil_change_pct")))
        if m.get("gold"):
            fx_cmd_parts.append(_indexish("금", m["gold"], m.get("gold_change_pct")))
        if fx_cmd_parts:
            lines.append(" | ".join(fx_cmd_parts))
        flows = _format_kospi_flows(m)
        if flows:
            lines.append(f"수급(KOSPI, 억): {flows}")
        if m.get("sentiment") is not None:
            lines.append(
                f"심리: {_safe(m['sentiment'])} "
                f"(점수: {m.get('sentiment_score', '-')})"
                + (f" | 국면: {_safe(m['regime_hint'])}" if m.get("regime_hint") else "")
            )
        lines.append("")

    # 워치리스트
    if data.get("watchlist"):
        lines.append(f"<b>워치리스트</b> Top {len(data['watchlist'])}")
        for w in data["watchlist"][:5]:
            rank = w["rank"] if w["rank"] is not None else "-"
            score = f"{w['hybrid_score']:.0f}" if w["hybrid_score"] is not None else "-"
            tier = w["trade_tier"] or "-"
            lines.append(f"  #{rank} {_safe(w['stock_name'])} ({score}점, {tier})")
        lines.append("")

    # 뉴스
    if data.get("news"):
        lines.append("<b>뉴스</b>")
        for n in data["news"]:
            lines.append(f"  [{_safe(n['stock_code'])}] {_safe(n['headline'])}")
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "build_llm_context",
    "compute_trade_summary",
    "format_fallback_html",
]
