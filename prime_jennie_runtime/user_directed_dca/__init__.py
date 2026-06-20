"""user_directed_dca — 사용자 지정 종목 buy-only 시차 분할매수.

운영자가 텔레그램으로 무장(echo + "확인")한 종목을 매 영업일 14:30 시장가로 분할
매수한다. slow/fast loop·tracker·시트와 무관한 독립 경로이며, 게이트웨이 주문 API 를
직접 호출한다. 자동 매도는 없다(매도는 앱/텔레그램 수동). paper 알파 측정에서 구조적으로
제외된다 — 이 패키지의 dca_* 테이블만 쓰고 position_sheets/outcomes 를 건드리지 않는다.

설계: 명세 + 민지 rationale (2026-06-20), migration 026.
"""
