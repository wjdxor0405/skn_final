"""리뷰 서비스 — A7 부품 리뷰 / 전체 PC 리뷰 작성 · 게시 · 개봉/구매 인증.

부품 리뷰 = product/variant 대상, 자기신고. 전체 PC 리뷰 = pc_build_version/component 고정 +
assembled_self_reported 확인 후 게시(C11). 외부 리뷰 원문 미저장.
"""
from __future__ import annotations

from uuid import UUID

from src.config import REVIEW_SUMMARIES_DEMO
from src.errors import NotFound
from src.repo.review_repo import ReviewSummaryDemoFile, default_risk_store
from src.schemas import ProductRiskOut, ReviewSummaryOut, ReviewTelemetry, SyntheticDemoOut

TELEMETRY_KEY = "telemetry"

_demo_file: ReviewSummaryDemoFile | None = None


def _stores():
    """파일 기반 산출물 — DB 연결 전까지의 자리. 산출 JSON 이 없으면 관측 없이 데모 블록만."""
    global _demo_file
    if _demo_file is None:
        _demo_file = ReviewSummaryDemoFile(REVIEW_SUMMARIES_DEMO)
    return default_risk_store(), _demo_file


def get_summary(product_key: str) -> ReviewSummaryOut:
    """S5 리뷰 상세. 실측(관계·행동 축 관측)과 합성 데모 블록을 분리해 낸다.

    - 관측은 상품 단위이고 점수가 아니다. 개별 리뷰의 진위가 아니다
    - cleaned_rating · cleanse_ratio 는 항상 null — 판정기가 없다(docs/decisions/0001)
    - 항목별 평가·요약 3건은 지금 합성 데모뿐이라 `synthetic_demo` 에 표지와 함께 둔다
    """
    store, demo = _stores()
    facts = store.get(product_key) if store else None
    d = demo.get(product_key) if demo else None
    if facts is None and d is None:
        raise NotFound(f"리뷰 요약 없음: {product_key}", field="product_key")

    if facts is not None:
        auth = store.get_review_authenticity(product_key)
        risk = auth["product_manipulation_risk"]
        ref = risk.get("product_ref")
        risk_out = ProductRiskOut(
            evidence=risk["evidence"], reliable_range=risk["reliable_range"],
            controls=risk.get("controls", {}), control_scope=store.meta.get("control_scope"),
            product_ref=ref, verify_url=f"https://www.amazon.com/dp/{ref}" if ref else None)
        orig, total, note = auth["orig_rating"], auth["total_reviews"], auth["confidence_note"]
    else:
        risk_out = ProductRiskOut(evidence=[], reliable_range=None)
        orig, total = None, 0
        note = ("관측 없음 — 이 상품은 관계·행동 축 산출물에 없다(리뷰 수 문턱 미만이거나 데이터 기간 밖). "
                "정제 평점·제외 비율은 산출하지 않는다.")

    synthetic = None
    if d is not None:
        synthetic = SyntheticDemoOut(
            note=d.get("cleaned_rating_note", "합성 데모값"),
            cleaned_rating=d.get("cleaned_rating"), cleanse_ratio=d.get("cleanse_ratio"),
            removed_count=d.get("removed_count"), rating_dist=d.get("rating_dist", {}),
            axis_scores=d.get("axis_scores", {}), top_summaries=d.get("top_summaries", []),
            sources=d.get("sources", []), collected_at=d.get("collected_at"))

    return ReviewSummaryOut(
        product_key=product_key, product_name=d.get("product_name") if d else None,
        orig_rating=orig, total_reviews=total, confidence_note=note,
        product_manipulation_risk=risk_out, synthetic_demo=synthetic)


def usage_context_with_telemetry(usage_context: dict | None, telemetry: ReviewTelemetry | None) -> dict:
    """`review_revision.usage_context` 에 폼 계측값을 `telemetry` 키로 넣는다.

    없으면 키를 만들지 않는다 — 빈 dict 나 0 으로 채우면 "붙여넣기 0회 = 직접 씀" 으로 읽히는데
    그건 이 신호가 말하지 않는 것이다. 있을 때만, 정수만 들어간다(ReviewTelemetry 가 거른다).
    """
    ctx = dict(usage_context or {})
    if telemetry is not None:
        ctx[TELEMETRY_KEY] = telemetry.model_dump()
    return ctx


def write_part_review(user_id: UUID, variant_id: UUID, *, rating: int, title: str,
                      body: str, axis_scores: dict, telemetry: ReviewTelemetry | None = None) -> dict:
    """usage_context = usage_context_with_telemetry(…, telemetry) 로 add_revision 에 넘긴다."""
    raise NotImplementedError


def write_build_review(user_id: UUID, build_version_id: UUID, *, rating: int, title: str,
                       body: str, axis_scores: dict, telemetry: ReviewTelemetry | None = None) -> dict:
    """build.owner == author, build_version published, usage_status assembled_self_reported 확인.
    usage_context 는 write_part_review 와 같은 규칙."""
    raise NotImplementedError


def publish(review_id: UUID, user_id: UUID) -> None:
    raise NotImplementedError


def list_pending_for_user(user_id: UUID) -> dict:
    """A7: 작성해야 할 리뷰 / 개봉 확인 / 내가 쓴 리뷰."""
    raise NotImplementedError
