"""PC 견적 점검 — 대안 적용 (CHK-08).

비교 분석(CHK-07)이나 부품 비교(CHK-10)에서 나온 대안을 받아들이면, 그 부품(들)을 업그레이드 대상으로 하고 나머지는
견적에 적힌 대로 유지하는 **새 계획**을 만든다. 판정·조합은 새로 짜지 않고 업그레이드 추천(mode=upgrade)을
그대로 재사용한다(session_service.choose_category + 조건 저장 + recommendation_service.start_recommendation) —
기획서가 명시한 방식이다. 엔진이 그 슬롯에 어떤 후보를 고를지는 추천 로직의 몫이라, 사용자가 비교에서 본 특정
후보가 그대로 담긴다는 보장은 아니다(요구 사양·예산 안에서 최적을 다시 고른다).

필수 조건(용도·예산·우선순위)이 견적 점검에 없었으면 추천을 바로 시작하지 않고, 부족한 항목과 함께 새 세션 id를
돌려준다 — 호출자는 그 세션에서 조건 대화를 이어(POST /session/{id}/message 등) 채운 뒤 추천을 받는다.
"""
from __future__ import annotations

from uuid import UUID

from src.auth.deps import Principal
from src.categories import load_category
from src.errors import ValidationFailed
from src.repo.plan_repo import PlanRepo
from src.services import quote_review_service as qrs
from src.services import recommendation_service, session_service

MAX_SLOTS = 8


def apply_alternative(conn, list_id: UUID, principal: Principal, slots: list[str]) -> dict:
    slots = list(dict.fromkeys(s.strip() for s in slots if s and s.strip()))   # 순서 유지, 중복 제거
    if not slots:
        raise ValidationFailed("업그레이드할 부품을 하나 이상 골라주세요.", field="slots")
    if len(slots) > MAX_SLOTS:
        raise ValidationFailed(f"부품은 한 번에 {MAX_SLOTS}개까지 고를 수 있습니다.", field="slots")
    cat_def = load_category("computer")
    slot_structure = cat_def["slot_structure"]
    unknown = [s for s in slots if s not in slot_structure]
    if unknown:
        raise ValidationFailed(f"부품군이 아닙니다: {', '.join(unknown)}. 부품군: {', '.join(slot_structure)}",
                               field="slots")

    review = qrs.get_review(conn, list_id, principal)          # 소유권 확인 + 저장된 견적 점검 결과
    specs = dict((review.get("input") or {}).get("current_specs") or {})
    kept_specs = {slot: text for slot, text in specs.items() if slot not in slots}
    conditions = (review.get("input") or {}).get("conditions") or {}

    session = session_service.create_session(conn, principal)
    new_list_id = UUID(session["list_id"])
    session_service.choose_category(conn, new_list_id, "computer", "upgrade", principal)
    repo = PlanRepo(conn)
    revision = repo.get_current_revision(new_list_id)

    if kept_specs:
        repo.upsert_condition(revision["id"], "current_specs", {"value": kept_specs}, "extracted")
    repo.upsert_condition(revision["id"], "upgrade_parts", {"value": slots}, "explicit")
    # 출처 표시 — 사용자는 견적의 나머지 부품을 아직 갖고 있지 않다. 리포트 조립 가이드가 이 계획을 업그레이드
    # (기존 부품 분리)가 아니라 새 PC 조립으로 안내하려면 일반 업그레이드와 구분돼야 한다. 카테고리 slot_schema 밖의
    # 키라 조건 대화 에이전트가 쓰거나 조건 칩에 나오지 않는다.
    repo.upsert_condition(revision["id"], "plan_origin", {"value": "quote_review", "source_list_id": str(list_id)}, "explicit")
    for key in ("purpose", "resolution", "priority", "budget_max"):
        if conditions.get(key) is not None:
            repo.upsert_condition(revision["id"], key, {"value": conditions[key]}, "explicit")
    if conditions.get("games"):
        repo.upsert_condition(revision["id"], "games", {"value": conditions["games"]}, "explicit")

    full = repo.load_full(revision["id"])
    values = {row["condition_key"]: row["value"].get("value") for row in full["conditions"]}
    missing = session_service.compute_missing(cat_def, values)

    result = {"list_id": str(new_list_id), "browser_token": session["browser_token"], "revision_id": str(revision["id"]),
              "slots": slots, "missing": missing, "run_id": None}
    if not missing:
        accepted = recommendation_service.start_recommendation(conn, revision["id"])
        result["run_id"] = accepted["run_id"]
    return result
