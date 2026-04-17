"""네이버 업종 세분류 → `SectorGroup` 대분류 매핑.

v2 원본: `prime_jennie/domain/sector_taxonomy.py`. 포트폴리오 분산/섹터 집중도
계산에 사용. Track B 는 `update_naver_sectors` 에서만 참조하지만, 추후 screening
/ portfolio 등에서 공용이 되면 `prime_jennie_runtime/infra/` 또는 전용 domain
모듈로 승격. 지금은 단일 consumer 이므로 jobs 내부에 둔다.
"""

from __future__ import annotations

from prime_jennie_runtime.backtest.domain import SectorGroup

# 네이버 세분류 → SectorGroup. v2 mapping 그대로 복사 (재튜닝 금지).
NAVER_TO_GROUP: dict[str, SectorGroup] = {
    # 반도체/IT
    "반도체와반도체장비": SectorGroup.SEMICONDUCTOR_IT,
    "디스플레이장비및부품": SectorGroup.SEMICONDUCTOR_IT,
    "디스플레이패널": SectorGroup.SEMICONDUCTOR_IT,
    "전자장비와기기": SectorGroup.SEMICONDUCTOR_IT,
    "전자제품": SectorGroup.SEMICONDUCTOR_IT,
    "컴퓨터와주변기기": SectorGroup.SEMICONDUCTOR_IT,
    "핸드셋": SectorGroup.SEMICONDUCTOR_IT,
    "사무용전자제품": SectorGroup.SEMICONDUCTOR_IT,
    "전기제품": SectorGroup.SEMICONDUCTOR_IT,
    "소프트웨어": SectorGroup.SEMICONDUCTOR_IT,
    "IT서비스": SectorGroup.SEMICONDUCTOR_IT,
    "양방향미디어와서비스": SectorGroup.SEMICONDUCTOR_IT,
    "인터넷과카탈로그소매": SectorGroup.SEMICONDUCTOR_IT,
    # 바이오/헬스케어
    "제약": SectorGroup.BIO_HEALTH,
    "생물공학": SectorGroup.BIO_HEALTH,
    "생명과학도구및서비스": SectorGroup.BIO_HEALTH,
    "건강관리장비와용품": SectorGroup.BIO_HEALTH,
    "건강관리업체및서비스": SectorGroup.BIO_HEALTH,
    "건강관리기술": SectorGroup.BIO_HEALTH,
    # 2차전지/소재
    "전기장비": SectorGroup.SECONDARY_BATTERY,
    # 금융
    "은행": SectorGroup.FINANCE,
    "증권": SectorGroup.FINANCE,
    "손해보험": SectorGroup.FINANCE,
    "생명보험": SectorGroup.FINANCE,
    "카드": SectorGroup.FINANCE,
    "기타금융": SectorGroup.FINANCE,
    "창업투자": SectorGroup.FINANCE,
    # 자동차
    "자동차": SectorGroup.AUTOMOBILE,
    "자동차부품": SectorGroup.AUTOMOBILE,
    # 건설/부동산
    "건설": SectorGroup.CONSTRUCTION,
    "건축자재": SectorGroup.CONSTRUCTION,
    "건축제품": SectorGroup.CONSTRUCTION,
    "부동산": SectorGroup.CONSTRUCTION,
    # 화학/에너지
    "화학": SectorGroup.CHEMICAL,
    "석유와가스": SectorGroup.CHEMICAL,
    "에너지장비및서비스": SectorGroup.CHEMICAL,
    "가스유틸리티": SectorGroup.CHEMICAL,
    # 철강/소재
    "철강": SectorGroup.STEEL_MATERIAL,
    "비철금속": SectorGroup.STEEL_MATERIAL,
    "포장재": SectorGroup.STEEL_MATERIAL,
    "종이와목재": SectorGroup.STEEL_MATERIAL,
    "기계": SectorGroup.STEEL_MATERIAL,
    # 음식료/생활
    "식품": SectorGroup.FOOD_CONSUMER,
    "식품과기본식료품소매": SectorGroup.FOOD_CONSUMER,
    "음료": SectorGroup.FOOD_CONSUMER,
    "담배": SectorGroup.FOOD_CONSUMER,
    "가정용품": SectorGroup.FOOD_CONSUMER,
    "가정용기기와용품": SectorGroup.FOOD_CONSUMER,
    "화장품": SectorGroup.FOOD_CONSUMER,
    "섬유,의류,신발,호화품": SectorGroup.FOOD_CONSUMER,
    "호텔,레스토랑,레저": SectorGroup.FOOD_CONSUMER,
    "다각화된소비자서비스": SectorGroup.FOOD_CONSUMER,
    "교육서비스": SectorGroup.FOOD_CONSUMER,
    "백화점과일반상점": SectorGroup.FOOD_CONSUMER,
    "전문소매": SectorGroup.FOOD_CONSUMER,
    "판매업체": SectorGroup.FOOD_CONSUMER,
    "무역회사와판매업체": SectorGroup.FOOD_CONSUMER,
    # 미디어/엔터
    "게임엔터테인먼트": SectorGroup.MEDIA_ENTERTAINMENT,
    "방송과엔터테인먼트": SectorGroup.MEDIA_ENTERTAINMENT,
    "레저용장비와제품": SectorGroup.MEDIA_ENTERTAINMENT,
    "광고": SectorGroup.MEDIA_ENTERTAINMENT,
    "출판": SectorGroup.MEDIA_ENTERTAINMENT,
    # 운송/물류
    "해운사": SectorGroup.LOGISTICS_TRANSPORT,
    "항공사": SectorGroup.LOGISTICS_TRANSPORT,
    "항공화물운송과물류": SectorGroup.LOGISTICS_TRANSPORT,
    "도로와철도운송": SectorGroup.LOGISTICS_TRANSPORT,
    "운송인프라": SectorGroup.LOGISTICS_TRANSPORT,
    # 통신
    "통신장비": SectorGroup.TELECOM,
    "다각화된통신서비스": SectorGroup.TELECOM,
    "무선통신서비스": SectorGroup.TELECOM,
    # 유틸리티
    "전기유틸리티": SectorGroup.UTILITY,
    "복합유틸리티": SectorGroup.UTILITY,
    # 조선/방산
    "조선": SectorGroup.DEFENSE_SHIPBUILDING,
    "우주항공과국방": SectorGroup.DEFENSE_SHIPBUILDING,
    # 기타
    "상업서비스와공급품": SectorGroup.ETC,
    "복합기업": SectorGroup.ETC,
    "가구": SectorGroup.ETC,
    "문구류": SectorGroup.ETC,
    "기타": SectorGroup.ETC,
}


STOCK_SECTOR_OVERRIDE: dict[str, SectorGroup] = {
    "000880": SectorGroup.DEFENSE_SHIPBUILDING,  # 한화 — 방산/조선 핵심 지주
}


def get_sector_group(
    naver_sector: str, stock_code: str | None = None
) -> SectorGroup:
    """네이버 세분류 → SectorGroup. stock_code override 우선."""
    if stock_code and stock_code in STOCK_SECTOR_OVERRIDE:
        return STOCK_SECTOR_OVERRIDE[stock_code]
    return NAVER_TO_GROUP.get(naver_sector, SectorGroup.ETC)


__all__ = [
    "NAVER_TO_GROUP",
    "STOCK_SECTOR_OVERRIDE",
    "get_sector_group",
]
