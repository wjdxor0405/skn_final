"""추천 서비스 — [추천 실행] → 엔진 파이프라인 → 결과 영속화.

계약: docs/frontend_외부수정요청.md §D-4-2 (202 접수 + GET /result 폴링).
start_recommendation() 이 요청 트랜잭션 안에서 run 행만 만들고 즉시 반환하고,
execute_recommendation() 이 백그라운드 태스크(자체 커넥션)에서 엔진을 실제로 돌려 저장한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from uuid import UUID

from src.categories import load_category
from src.dto import PipelineResult, RequirementSpec
from src.engine.brands import SLOT_SYNONYMS
from src.engine.lang import fmt_money
from src.engine.slots import slots_from_conditions as _slots_from_conditions
from src.engine.stage3c_verify import AXIS_SLOTS as _AXIS_SLOTS
from src.errors import Conflict, NotFound, ValidationFailed
from src.pipeline import run_pipeline as _run_scenario

log = logging.getLogger(__name__)


def run_from_scenario(scenario_name: str) -> PipelineResult:
    """개발용: 시나리오 파일로 파이프라인 1회 (DB 미사용)."""
    return _run_scenario(scenario_name, on_log=lambda _m: None, catalog_source="mock")


# 예산의 이 비율 이상을 남겼을 때만 안내한다 — 조금 남는 건 흔해서 알릴 일이 아니다.
UNDERSPENT_RATIO = 0.15
# 우선순위가 "싼 쪽"을 고르게 하는 것들의 설명. 성능 우선은 예산을 채우는 쪽이라 여기 없다.
_UNDERSPENT_REASON = {
    "value": "가성비 우선이라 요구 성능을 만족하는 구성 중 가격 부담이 적은 쪽을 골랐어요.",
    "quiet": "저소음 우선이라 소음과 가격을 함께 보고 골랐어요.",
}


def budget_notice(totals: dict | None, values: dict) -> dict | None:
    """예산을 많이 남긴 새 구성에, 왜 남았는지와 성능 우선으로 다시 받는 길을 안내하는 문장.

    점수 규칙은 건드리지 않는다 — 결과는 그대로고 설명만 붙인다. 업그레이드는 예산이 바꿀 부품에 쓸 돈이라
    남는 게 자연스러워 제외하고, 예산 초과·조금 남은 경우·성능 우선(이미 채우는 쪽)도 안내하지 않는다."""
    budget = values.get("budget_max")
    reason = _UNDERSPENT_REASON.get(values.get("priority"))
    if not totals or not budget or reason is None or values.get("mode") != "build" or totals.get("over_budget"):
        return None
    spent = int(totals.get("selected_price") or 0)
    remaining = int(budget) - spent
    if spent <= 0 or remaining < budget * UNDERSPENT_RATIO:
        return None
    return {
        "message": (f"예산 {fmt_money(budget)} 중 {fmt_money(spent)}을 썼어요. {reason} "
                    f"남은 {fmt_money(remaining)}으로 성능을 더 올리려면 '성능 우선'으로 다시 추천받을 수 있어요."),
        "budget": int(budget), "spent": spent, "remaining": remaining, "suggest_priority": "performance",
    }


def assess_budget_feasibility(conn, category: str | None, values: dict) -> dict | None:
    """조건 화면용 사전 경고 — 지금 채워진 조건으로 [2]를 세워 "요구 성능의 최저가 합계"를 예산과 견준다.

    경고할 게 없으면(예산 없음·판정 ok·PC 아님) None. 조건이 덜 채워졌거나 카탈로그를 못 읽으면
    그냥 None — 이 경고 때문에 조건 대화 자체가 실패해선 안 된다."""
    if category != "computer" or not values.get("budget_max"):
        return None
    from src.engine import feasibility as feasibility_engine
    from src.engine import stage2_requirement
    from src.repo.catalog_repo import load_candidates_by_slot_from_db

    try:
        cat_def = load_category(category)
        spec = stage2_requirement.run(_slots_from_conditions(category, cat_def, values), cat_def, lambda _m: None)
        with conn.transaction():   # 세이브포인트 — 카탈로그 조회가 실패해도 요청 트랜잭션은 그대로 둔다
            pools = load_candidates_by_slot_from_db(conn)
        result = feasibility_engine.assess(spec, pools)
    except Exception:  # noqa: BLE001 — 사전 경고는 부가 기능이다
        log.warning("예산 사전 판정 실패", exc_info=True)
        return None
    if result["level"] == feasibility_engine.LEVEL_OK and not result["message"]:
        return None
    return {"level": result["level"], "message": result["message"],
            "estimated_min": result["estimated_min"], "budget": result["budget"]}


def start_recommendation(
    conn,
    revision_id: UUID,
    *,
    strategy: str = "default",
) -> dict:
    """POST /recommend 가 호출 — run 행을 만들고 즉시 접수 응답만 반환한다 (202).

    실제 엔진 실행은 여기서 하지 않는다 — 호출 쪽(라우터)이 execute_recommendation을
    BackgroundTasks 로 별도 커넥션에서 돌린다.
    """
    from src.repo.engine_repo import EngineRepo
    from src.repo.plan_repo import PlanRepo

    prepo, erepo = PlanRepo(conn), EngineRepo(conn)
    revision = prepo.get_revision(revision_id)
    if revision is None:
        raise ValidationFailed("계획 버전을 찾을 수 없습니다.", field="list_id")
    full = prepo.load_full(revision_id)
    values = {
        row["condition_key"]: row["value"].get("value")
        for row in full["conditions"]
    }
    category = values.get("category")
    if category is None:
        raise Conflict("카테고리를 먼저 선택하세요.", code="category_required")
    if category != "computer":
        raise ValidationFailed(f"지원하지 않는 카테고리입니다: {category}", field="category")

    cat_def = load_category(category)
    from src.services import session_service
    missing = session_service.compute_missing(cat_def, values)
    if missing:
        raise ValidationFailed(f"필수 조건이 아직 안 채워졌습니다: {missing}", field="conditions", code="conditions_incomplete")
    if erepo.has_running_run(revision_id):
        raise Conflict("이미 추천을 실행하는 중입니다.", code="run_in_progress")

    input_snapshot = {
        "values": values,
        "strategy": strategy,
    }
    input_hash = hashlib.sha256(
        json.dumps(input_snapshot, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()

    run_id = erepo.start_run(
        revision_id, revision["domain_version_id"],
        input_snapshot=input_snapshot,
        input_hash=input_hash,
        draft_lock_version=revision["lock_version"],
        engine_versions={"pipeline": "computer-v1"},
    )
    return {"run_id": str(run_id), "status": "running"}


def execute_recommendation(revision_id: UUID, run_id: UUID) -> None:
    """백그라운드 태스크 — 엔진 [2]~[5] 실행 + 저장 + run 종료. 자체 커넥션을 연다."""
    from src.db import get_conn
    from src.engine import feasibility as feasibility_engine
    from src.engine import stage2_requirement, stage3a_hardfilter, stage3c_verify, stage4_optimize, stage5_explain
    from src.engine import research_loop
    from src.repo.catalog_repo import load_candidates_by_slot_from_db
    from src.repo.engine_repo import EngineRepo
    from src.repo.plan_repo import PlanRepo
    from src.rag.care_guides import search_care_guide
    from src.engine.owned_parts import upgrade_tier_notes
    from src.services import review_service

    noop = lambda _m: None  # noqa: E731

    # get_conn() 은 with 블록 전체를 트랜잭션 하나로 묶어 블록이 끝날 때 한 번만 커밋한다.
    # 그래서 [2]~[4]+검증과 [5]를 같은 with 블록에 두면 complete_run을 앞당겨 불러도
    # [5]가 끝나기 전엔 아무것도 커밋되지 않아 폴링 중인 GET /result가 여전히 못 본다.
    # 두 블록(=두 트랜잭션)으로 쪼개야 부품표가 [5] 완료 전에 실제로 보인다.
    try:
        with get_conn() as conn:
            prepo, erepo = PlanRepo(conn), EngineRepo(conn)
            full = prepo.load_full(revision_id)
            values = {
                row["condition_key"]: row["value"].get("value")
                for row in full["conditions"]
            }
            category = values["category"]
            cat_def = load_category(category)
            slots = _slots_from_conditions(category, cat_def, values)

            current_tiers: dict = {}
            spec = stage2_requirement.run(slots, cat_def, noop)
            spec.list_id = str(revision_id)
            if spec.mode == "upgrade" and not spec.targets:
                raise ValidationFailed("업그레이드할 부품을 선택해 주세요.", field="upgrade_parts",
                                       code="upgrade_parts_required")

            req_id_by_slot = {}
            for slot in spec.targets:
                node_id = prepo.ensure_node(revision_id, slot, slot)
                req_id_by_slot[slot] = prepo.ensure_requirement(revision_id, node_id, spec.targets[slot])

            by_slot = load_candidates_by_slot_from_db(conn)
            if spec.mode == "upgrade":
                # 사용자가 그대로 쓰는 부품은 견적에 넣지 않고, 적어 준 것만 호환성 검사에 쓴다.
                from src.engine.owned_parts import constrain_targets, current_part_tiers, owned_for_conditions
                from src.services.live_spec_lookup import cached_for_kept_parts
                spec.owned = owned_for_conditions(values, by_slot, spec.targets, cat_def.get("slot_structure", []))
                cached_for_kept_parts(conn, spec.owned, values.get("current_specs"))
                constrain_targets(spec)
                # 지금 쓰는 CPU·GPU 보다 낮은 등급은 "업그레이드"가 아니다 — 알면 그 등급을 하한으로 건다.
                current_tiers = current_part_tiers(values.get("current_specs"), by_slot, spec.targets)
                for slot, info in current_tiers.items():
                    spec.targets[slot]["perf_tier_min"] = max(spec.targets[slot].get("perf_tier_min", 0), info["tier"])
            elif values.get("wanted_parts"):
                # 신규 조립에서 사용자가 원했지만 카탈로그에 없는 부품 — 소켓·메모리 규격만 나머지 부품의 호환 조건으로 건다
                # (그 부품을 견적에 넣지는 않는다. docs/미보유부품_실시간스펙검색_설계.md §11).
                from src.engine.wanted_parts import apply_wanted_constraints
                for note in apply_wanted_constraints(spec, values["wanted_parts"], by_slot):
                    log.info("wanted_parts [%s]: %s", revision_id, note)
            missing_slots = [slot for slot in spec.targets if not by_slot.get(slot)]
            if missing_slots:
                raise ValidationFailed(
                    f"가격이 확인된 PC 후보가 없는 슬롯: {', '.join(missing_slots)}",
                    field="catalog", code="catalog_incomplete",
                )
            feasibility = feasibility_engine.assess(spec, by_slot)
            spec.budget["feasibility"] = feasibility["level"]
            spec.budget["feasibility_detail"] = feasibility
            hf = stage3a_hardfilter.run(spec, by_slot, noop)
            from src.services.review_ranking import rank_with_review_aspects
            rank, _review_profiles = rank_with_review_aspects(
                hf, spec, slots, noop, conn=conn, run_id=run_id, catalog_source="db",
            )
            # 지금 쓰는 부품 자체를 다시 추천하지 않는다(같은 제품으로 '교체'하는 견적이 나왔다).
            already_owned = {(slot, key) for slot, info in current_tiers.items() for key in info["keys"]}
            computer_rules = stage2_requirement.load_computer_rules()
            compat_rules = computer_rules["verification"]
            # 점수 영향이 큰 슬롯(보통 CPU·GPU)은 "스펙 데이터가 없다"는 이유만으로 빼지 않는다 —
            # 그러면 성능이 눈에 띄게 낮은 구성이 "확인 필요 건수 감소"만으로 채택될 수 있다(리뷰 지적).
            impact_slots = frozenset(computer_rules["ranking"].get("impact_slots", []))

            def _verify(b, _round_index):
                # E5 — chosen(슬롯→Candidate, provenance 포함)을 넘겨야 verify_build 가 쟁점에
                # 카탈로그 출처 근거(evidence)를 붙인다. research_loop.resolve_chosen 이 [3-B] 가 남긴
                # 전체 스펙(provenance 포함)으로 다시 찾아 준다.
                b_chosen = research_loop.resolve_chosen(rank, b)
                return stage3c_verify.verify_build(b, category, noop, chosen=b_chosen, spec=spec, rules=compat_rules)

            def _choose_exclusion(b, v):
                chosen = research_loop.resolve_chosen(rank, b)
                return research_loop.rule_based_exclusion(b, v, chosen, spec, compat_rules, impact_slots=impact_slots)

            # E1: [4] 세트 최적화 → [3-C] 검증을 최대 MAX_RESEARCH_ROUNDS회 돌며 "근사(확인 필요)" 누적을
            # 규칙 기반으로 줄여 본다(C5). 데모 경로(pipeline._run_computer_branch)와 같은 루프 함수다.
            build, verification, research_rounds = research_loop.run_set_with_research(
                rank, spec, noop, verify=_verify, choose_exclusion=_choose_exclusion,
                base_exclude=frozenset(already_owned),
            )
            build.list_id = str(revision_id)

            from src.services.recommendation_review_snapshot import snapshot_step
            erepo.store_review_snapshot(run_id, snapshot_step(rank, build, run_id, values))

            # 부품·가격·검증은 여기서 이미 확정됐다. [5] 설명 문장(LLM)은 아직 안 돌았으므로
            # reason=None 으로 저장한다 — add_candidate가 자동으로 reason_status='pending' 처리.
            candidate_id_by_slot: dict[str, UUID] = {}
            for item in build.items:
                if item.variant_id is None:
                    raise ValidationFailed("추천 후보의 DB 상품 ID가 없습니다.", field="catalog", code="catalog_incomplete")
                variant_id = UUID(item.variant_id)
                if item.offer_observation_id is None:
                    raise ValidationFailed("추천 후보의 가격 관측 ID가 없습니다.", field="catalog", code="catalog_incomplete")
                offer_observation_id = UUID(item.offer_observation_id)
                candidate_id_by_slot[item.slot] = erepo.add_candidate(
                    run_id, req_id_by_slot[item.slot], variant_id, result="selected",
                    score=item.score, score_method_version="v1", reason=None,
                    offer_observation_id=offer_observation_id,
                )

            _store_validations(erepo, run_id, verification)

            erepo.complete_run(run_id)

            # P8 FB03: 결과가 (처음으로) 만들어졌다는 append-only 행. GET/폴링은 이 함수를
            # 다시 부르지 않으므로(get_stored_result만 읽는다) run당 한 번만 기록된다.
            from src.services import feedback_service
            feedback_service.emit_shown(
                conn, plan_id=full["plan_id"], revision_id=revision_id, run_id=run_id,
                version=full["lock_version"],
            )
        # ↑ with 블록이 끝나며 여기서 커밋된다 — 부품·가격·검증이 done으로 확정.
    except Exception:  # noqa: BLE001 — 실패해도 running으로 영원히 남지 않게 별도 커넥션으로 failed 처리
        with get_conn() as fail_conn:
            fail_conn.execute(
                "UPDATE engine.recommendation_run SET status='failed', completed_at=now(), updated_at=now() "
                "WHERE id=%s AND status='running'",
                (run_id,),
            )
        raise

    # [5] 설명 문장(LLM 호출) — 별도 트랜잭션. 실패해도 위에서 이미 커밋한 부품·가격·검증에는
    # 영향이 없다. run.status는 건드리지 않고 문장 쪽 상태(reason_status/explanation_status)만 옮긴다.
    try:
        with get_conn() as conn:
            erepo = EngineRepo(conn)
            # rank 를 넘겨야 [3-B] 가 후보에 남긴 리뷰 관측 플래그를 [5] 가 읽는다.
            # 빼면 모든 슬롯이 "관측 없음" 이 되고, 감점만 남고 근거가 사라진다 (기본값이 None 이라 조용히).
            explanation = stage5_explain.run(
                build,
                verification,
                noop,
                rank=rank,
                conditions=values,
                **_explanation_extras(spec, tier_notes=upgrade_tier_notes(current_tiers, build.items)),
            )

            for it in explanation.items:
                candidate_id = candidate_id_by_slot.get(it.slot)
                if candidate_id is not None and it.reason is not None:
                    erepo.update_candidate_reason(candidate_id, it.reason)

            # "구매 전 확인"(checks) — 부품 사용 가이드 RAG 검색. 슬롯 고정 매핑이 아니라
            # 품목명까지 넣은 질의로 임베딩 유사도 검색을 실제로 돌린다(src/rag/care_guides.py).
            for it in build.items:
                candidate_id = candidate_id_by_slot.get(it.slot)
                if candidate_id is None:
                    continue
                hits = search_care_guide(f"{it.slot} {it.name} 사용 시 확인할 점", k=1, slot=it.slot)
                if hits:
                    erepo.update_candidate_checks(candidate_id, hits[0]["text"])

            # [5] 의 리뷰 관측(review_line_by_slot)·확인 필요(caveats)를 저장 경로에 싣는다.
            # 여기서 안 실으면 [3-B] 감점은 되는데 "왜" 가 화면에 안 간다 (review_service 주석 참고).
            trace_rows = [
                ("조건 정리", f"카테고리 {category}, 예산 {fmt_money(values.get('budget_max'))}" if values.get("budget_max") else "조건 정리"),
                ("후보 수집", f"세트 {len(build.items)}개 부품"),
                ("설명 생성", explanation.headline),
            ]
            games_row = _games_trace_row(spec)
            if games_row is not None:
                trace_rows.insert(1, games_row)
            # E1: 재탐색이 2라운드 이상 돌았을 때만 "왜 세트가 바뀌었는지"를 trace 에 남긴다.
            # 신뢰도 숫자는 넣지 않는다(결정 0003) — 확인 필요 건수만으로 서술한다(엔진 쪽 순수 함수).
            research_detail = research_loop.research_trace_detail(research_rounds)
            if research_detail is not None:
                trace_rows.insert(-1, ("세트 재검토", research_detail))
            trace = [{"step": s, "title": s, "detail": d} for s, d in trace_rows]
            # 기여도는 저장할 전용 컬럼이 없어(스키마 기준선 4파일 고정) 추적 기록에 구조화해 싣는다.
            # get_stored_result 가 이 항목에서 explanation.contribution 을 꺼낸다.
            if explanation.contribution:
                trace.insert(-1, {"step": "기여도", "title": "기여도",
                                  "detail": " · ".join(f"{axis} {pct}%" for axis, pct in explanation.contribution.items()),
                                  "contribution": explanation.contribution})
            # 요청 프로필의 Q/R/α와 실제 aggregate member ID·방향을 trace에 남긴다.
            # 이 자료는 stage5가 rank snapshot에서 가져왔으므로 여기서 DB 재조회하지 않는다.
            evidence_by_slot = {it.slot: it.evidence for it in explanation.items if it.evidence}
            for step in review_service.review_trace_steps(explanation.review_line_by_slot, evidence_by_slot):
                step.update(source_run_id=str(run_id), basis="original_recommendation")
                trace.insert(-1, step)

            # 03 "추천 요약" 본문 = summary + 확인이 필요한 것. 슬롯별 reason 은 각 부품의 "추천 이유" 에
            # 따로 나가므로 여기 나열하지 않는다 (전에는 reason 8줄을 이어붙여 요약이 아니었다).
            erepo.set_explanation(
                run_id, headline=explanation.headline,
                text=review_service.explanation_text_with_caveats(explanation.summary, explanation.caveats),
                reasoning_log=trace,
            )
    except Exception:  # noqa: BLE001 — [5] 실패는 문장만 failed, 부품표는 이미 done인 채로 둔다
        with get_conn() as fail_conn:
            fail_erepo = EngineRepo(fail_conn)
            for candidate_id in candidate_id_by_slot.values():
                fail_erepo.fail_candidate_reason(candidate_id)
                fail_erepo.fail_candidate_checks(candidate_id)
            fail_erepo.fail_explanation(run_id)





def _store_validations(erepo, run_id: UUID, verification) -> None:
    """세트 검증 쟁점을 engine.validation_result 행으로 저장한다(결과 화면의 verification·구매 전 확인이 읽는다)."""
    target = verification.targets[0] if verification.targets else None
    for issue in target.issues if target else []:
        erepo.add_validation(
            run_id, rule_key=issue.axis, rule_version="v1", executor_version="v1",
            status="fail" if issue.penalty >= 15 else "unknown",
            severity="warning" if issue.penalty >= 15 else "info",
            measured_values={"penalty": issue.penalty, "confidence": target.confidence, "round": target.rounds,
                             "evidence": issue.evidence},
            threshold={}, message=issue.text or issue.judge or issue.axis,
            checked_at=datetime.now(timezone.utc),
        )


def _current_set(conn, revision_id: UUID, cvals: dict, stored: list[dict]):
    """저장된 후보 행 → 지금 선택된 부품({슬롯: Candidate})과 유지 부품이 든 RequirementSpec. 재검증과 상세 조회가 같이 쓴다.

    신규 조립(build)은 지금 고른 부품(picked)만 있으면 되니 그만큼만 읽는다 — upgrade일 때만
    "보유 부품" 이름 매칭에 카탈로그 전체가 필요해서 그때만 전체를 읽는다(개발요청 5번 —
    스왑·수량 변경마다 카탈로그 전체를 읽던 비용을 없앤다)."""
    from src.engine.owned_parts import owned_for_conditions
    from src.repo.catalog_repo import load_candidates_by_slot_from_db, load_candidates_by_variant
    from src.services.live_spec_lookup import cached_for_kept_parts

    picked = [row for row in stored if row.get("selected") is not False]
    if cvals.get("mode") == "upgrade":
        pool = load_candidates_by_slot_from_db(conn)
        by_variant = {c.variant_id: c for cands in pool.values() for c in cands}
    else:
        pool = {}
        by_variant = load_candidates_by_variant(
            conn, [(row["product_type"], str(row["variant_id"])) for row in picked]
        )
    chosen = {row["slot"]: by_variant[str(row["variant_id"])] for row in picked if str(row["variant_id"]) in by_variant}
    owned = owned_for_conditions(cvals, pool, {r["slot"] for r in stored},
                                 load_category("computer").get("slot_structure", []))
    cached_for_kept_parts(conn, owned, cvals.get("current_specs"))
    spec = RequirementSpec(
        list_id=str(revision_id), category="computer",
        mode="upgrade" if cvals.get("mode") == "upgrade" else "build",
        owned=owned,
    )
    return chosen, spec


def compat_checks(conn, revision_id: UUID, cvals: dict, stored: list[dict]) -> list[dict]:
    """결과 화면의 "호환성 점검 상세" — 검사별로 무엇을 비교했고 결과가 어땠는지(PC 만). 저장하지 않고 지금 선택된
    부품으로 그때그때 계산해서, 교체·담기 뒤에도 항상 현재 구성을 따라간다."""
    if cvals.get("category") != "computer" or not stored:
        return []
    from src.engine.stage2_requirement import load_computer_rules
    from src.engine.stage4_optimize import pc_compat_details

    chosen, spec = _current_set(conn, revision_id, cvals, stored)
    rows = pc_compat_details(chosen, spec, load_computer_rules()["verification"])
    if cvals.get("budget_max"):
        total = sum((int(r["price"]) if r["price"] is not None else 0) * int(r["qty"] or 1)
                    for r in stored if r.get("selected") is not False)
        over = total > cvals["budget_max"]
        rows.append({"axis": "budget", "label": "예산", "state": "fail" if over else "ok",
                     "detail": f"선택한 구성 합계 {fmt_money(total)} {'>' if over else '≤'} 예산 {fmt_money(cvals['budget_max'])}"})
    return rows


def reverify_set(conn, revision_id: UUID, run: dict) -> None:
    """교체·담기/빼기·수량 변경 뒤 세트 전체를 다시 점검해 저장된 검증을 바꾼다(PC 만).

    처음 추천은 [4] 가 고른 세트를 점검했고, 이후 사용자가 바꾼 세트는 그 결과와 달라진다 — 소켓·전력·크기가
    안 맞는 부품으로 바꿔도 화면은 "문제 없음"으로 남았다. 같은 점검(stage4.pc_link_check)과 같은 문장화
    (stage3c.verify_build)를 지금 선택된 부품으로 다시 돌린다. 순위·설명 문장은 다시 만들지 않는다.
    """
    from src.engine import stage3c_verify
    from src.engine.owned_parts import owned_for_conditions
    from src.engine.stage2_requirement import load_computer_rules
    from src.engine.stage4_optimize import pc_link_check
    from src.dto import BuildResult
    from src.repo.catalog_repo import load_candidates_by_slot_from_db
    from src.repo.engine_repo import EngineRepo
    from src.repo.plan_repo import PlanRepo

    cvals = {r["condition_key"]: r["value"].get("value") for r in PlanRepo(conn).load_full(revision_id)["conditions"]}
    if cvals.get("category") != "computer":
        return
    erepo = EngineRepo(conn)
    stored = erepo.get_candidates(run["id"])
    picked = [row for row in stored if row.get("selected") is not False]
    chosen, spec = _current_set(conn, revision_id, cvals, stored)
    link_check = pc_link_check(chosen, spec, load_computer_rules()["verification"])

    budget_max = cvals.get("budget_max") or 0
    total = sum((int(r["price"]) if r["price"] is not None else 0) * int(r["qty"] or 1) for r in picked)
    if budget_max and total > budget_max:
        link_check["budget"] = "fail"
    build = BuildResult(list_id=str(revision_id), link_check=link_check,
                        budget={"max": budget_max, "used": total,
                                "used_pct": round(total / budget_max * 100, 1) if budget_max else 0.0})
    verification = stage3c_verify.verify_build(
        build, "computer", chosen=chosen, spec=spec, rules=load_computer_rules()["verification"])
    erepo.delete_validations(run["id"])
    _store_validations(erepo, run["id"], verification)


def _conditions_summary(cat_def: dict, values: dict) -> str:
    from src.services import session_service
    fields = session_service._build_fields(cat_def, values)
    parts = [f["display"] for f in fields if f["status"] == "confirmed" and f["display"]]
    return " · ".join(parts)


_SWAP_RE = re.compile(r"자동 추천은 '(.+?)'\((\$?[\d,]+원?)\)였고 이 후보는 ([+-]\$?[\d,]+원?)")


def memo_suggestion(result: dict, values: dict) -> str:
    """04 리스트 확정 "메모" 초기값. 저장된 사실만 — 조건, 확정 구성, 사용자가 직접 바꾼 것, 확인이 필요한 것.
    LLM 없음(요약 문장은 이미 03 에서 만들었고, 메모는 사용자가 고쳐 쓰는 칸이다). 1,000자 제한 안."""
    items = result.get("items") or []
    totals = result.get("totals") or {}
    m = fmt_money
    lines: list[str] = []
    cond = result.get("conditions_summary") or ""
    if result.get("budget_max") and m(result["budget_max"]) not in cond:   # 조건 요약에 이미 예산이 있으면 반복 안 함
        cond += (" · " if cond else "") + f"예산 {m(result['budget_max'])}"
    if cond:
        lines.append("[조건] " + cond)
    chosen = [it for it in items if it["selected"]]
    if chosen:
        parts = [f"{it['slot']} {it['product']['name']}" + (f" ×{it['qty']}" if it["qty"] > 1 else "")
                 for it in chosen]
        tail = ""
        if totals.get("budget_remaining") is not None:
            tail = (f", 예산 초과 {m(-totals['budget_remaining'])}" if totals.get("over_budget")
                    else f", 예산 잔여 {m(totals['budget_remaining'])}")
        lines.append(f"[구성] {len(chosen)}개 부품 {m(totals.get('selected_price', 0))}{tail} — " + ", ".join(parts))
    removed = [it["slot"] for it in items if not it["selected"]]
    if removed:
        lines.append("[뺀 것] " + ", ".join(removed))
    swapped = []
    for it in items:
        rt = (it.get("reason") or {}).get("text") or ""
        sw = _SWAP_RE.search(rt)
        if sw:
            swapped.append(f"{it['slot']} {sw.group(1)} → {it['product']['name']} ({sw.group(3)})")
    if swapped:
        lines.append("[직접 바꾼 것] " + "; ".join(swapped)
                     + " — 호환 점검은 교체 후 구성 기준")
    headline = (result.get("explanation") or {}).get("headline")
    if headline:
        lines.append("[요약] " + headline)
    checks = []
    if values.get("extra"):
        checks.append("추가 요청 미반영: " + ", ".join(map(str, values["extra"]))
                      + " — 직접 확인")
    # 세트 신뢰도 점수는 메모에 넣지 않는다 — 규칙 스캐폴드 값이라 사용자에게 보일 단계가 아니다(docs/decisions/0003).
    # 쟁점이 있다는 사실만 남긴다.
    if (result.get("verification") or {}).get("issues"):
        checks.append("세트 검증 쟁점 있음 — 추천 과정 보기에서 확인")
    if checks:
        lines.append("[확인] " + " · ".join(checks))
    text = "\n".join(lines)
    return text if len(text) <= 1000 else text[:997] + "…"


def explanation_text(summary: str, caveats: list[str]) -> str:
    """explanation.text — 요약 문단 + "확인이 필요한 것". 화면은 한 상자에 그대로 보여준다."""
    if not caveats:
        return summary
    return summary + "\n\n확인이 필요한 것: " + " · ".join(caveats)


# _AXIS_SLOTS: [3-C] 검증 축 → 슬롯 매핑. 엔진(stage3c_verify.AXIS_SLOTS)으로 옮겨 여기선 import 해서 쓴다(E1).
_SWAP_REASON_PREFIX = "사용자 요청으로 교체한 부품입니다"


def _item_checks(item: dict, validations: list[dict], guide: dict | None = None) -> dict:
    """"구매 전 확인" — 사용 가이드(RAG, develop 이 저장한 것)를 앞에 두고, 코드가 아는 사실을 잇는다:
    이 슬롯에 걸린 세트 검증 쟁점, 교체 여부. 전에는 `pending` 하드코딩이었다.

    리뷰 관측·세트 신뢰도 문장은 여기서 뺐다(docs/개발요청_리뷰클렌징_요약_구조화.md 요청 B) —
    03 리뷰 패널이 ReviewBriefOut.signals(구조화 필드, review_service._review_signals)로 따로
    받는다. 신뢰도 표시 자체도 verify_build()가 아직 규칙 스캐폴드(RAG 미연결)라 노출하지 않는다.
    """
    slot = item["slot"]
    parts: list[str] = []
    if guide and guide.get("status") == "ready" and guide.get("text"):
        parts.append("사용 가이드: " + guide["text"])
    hit = [v for v in validations
           if slot in _AXIS_SLOTS.get(v["rule_key"], ()) or v["rule_key"] not in _AXIS_SLOTS]
    if hit:
        from src.engine.stage3c_verify import axis_label
        for v in hit:
            label, message = axis_label(v["rule_key"]), v["message"]
            parts.append(message if message.startswith(label) else f"{label}: {message}")
    else:
        parts.append("이 부품에 걸린 세트 검증 쟁점 없음")
    reason_text = (item.get("reason") or {}).get("text") or ""
    if reason_text.startswith(_SWAP_REASON_PREFIX):
        parts.append("교체한 부품 — 호환 점검은 교체 후 구성 기준입니다")
    return {"status": "ready", "text": " · ".join(parts)}


def get_stored_result(conn, revision_id: UUID) -> dict | None:
    """GET /result 가 호출 — 저장된 실행/후보/검증만 읽어 RecommendResult 모양으로 조립한다.

    폴링 대상: status가 running이면 items/verification/explanation은 아직 비어있거나 pending.
    """
    from src.repo.engine_repo import EngineRepo
    from src.repo.plan_repo import PlanRepo
    from src.repo.product_repo import ProductRepo
    from src.repo.catalog_repo import PC_TYPE_TO_SLOT, pc_catalog_key
    from src.services import review_service

    erepo, prepo, prodrepo = EngineRepo(conn), PlanRepo(conn), ProductRepo(conn)
    run = erepo.get_latest_run(revision_id)
    if run is None:
        return None
    from src.services.recommendation_review_snapshot import (
        original_review, review_for_candidate, snapshot_from_run,
    )
    review_snapshot = snapshot_from_run(run)

    revision = prepo.get_revision(revision_id)
    full = prepo.load_full(revision_id)
    values = {row["condition_key"]: row["value"].get("value") for row in full["conditions"]}
    category = values.get("category")
    cat_def = load_category(category) if category else {}

    status = {"queued": "running", "running": "running", "completed": "done",
              "failed": "failed", "stale": "failed"}.get(run["status"], run["status"])

    result: dict = {
        "list_id": str(revision["plan_id"]), "revision_id": str(revision_id),
        "lock_version": revision["lock_version"], "run_id": str(run["id"]), "status": status,
        "progress": [
            {"step": "conditions", "label": "조건 정리", "status": "done"},
            {"step": "candidates", "label": "후보 수집", "status": "done" if status != "running" else "running"},
        ],
        "category": category,
        "conditions_summary": _conditions_summary(cat_def, values) if cat_def else "",
        "budget_max": values.get("budget_max"),
        "items": [], "totals": None,
        "verification": {"status": "pending", "confidence": None, "issues": []},
        "explanation": {"status": "pending", "text": None},
        "reasoning_log": run.get("reasoning_log") or [],
        "data_notice": ("PC 상품·가격은 수집 파일 기반으로 실시간 정보가 아닙니다. "
            "추천 리뷰 상세는 해당 실행에 저장된 실제 리뷰 속성 관측을 기준으로 제공합니다. "
            "상세 미제공은 리뷰가 없다는 뜻이 아닙니다. 별점·리뷰 총수와는 별개이며, "
            "합성 데모 요약은 실제 추천 근거와 구분합니다."
            if category == "computer" else "상품·가격·리뷰는 합성 데이터입니다."),
    }
    if status == "failed":
        result["error"] = {"code": "recommend_failed", "message": "추천을 만드는 중 오류가 발생했어요."}
        return result
    if status == "running":
        return result

    candidates_by_slot = prodrepo.candidates_by_slot()
    items = []
    candidate_rows = erepo.get_candidates(run["id"])
    from src.repo.catalog_repo import load_specs_by_variant
    specs_by_variant = load_specs_by_variant(
        conn, [(row["product_type"], str(row["variant_id"])) for row in candidate_rows]
    )
    for row in candidate_rows:
        product_key = (pc_catalog_key(row["product_type"], row["brand"], row["product_key"])
                       if category == "computer" and row["product_type"] in PC_TYPE_TO_SLOT
                       else row["product_key"])
        spec_summary = _spec_summary(row["slot"], specs_by_variant.get(str(row["variant_id"])))
        price = int(row["price"]) if row["price"] is not None else 0
        slot_variants = candidates_by_slot.get(row["slot"], [])
        alternatives_count = sum(1 for c in slot_variants if c["variant_id"] != row["variant_id"])
        items.append({
            "item_id": str(row["id"]), "slot": row["slot"], "slot_label": row["slot_label"],
            "product": {
                "product_key": product_key, "variant_id": str(row["variant_id"]),
                "name": row["product_name"], "brand": row["brand"] or "",
                "spec_summary": spec_summary, "image_url": row["image_url"],
                "purchase_url": row["purchase_url"],
            },
            "price": price, "price_source": "observed" if category == "computer" else "synthetic",
            "price_observed_at": row["observed_at"].isoformat() if row["observed_at"] else None,
            "qty": row["qty"], "selected": row["selected"], "timing": row["timing"], "budget_share": None,
            "review": review_service.review_brief(product_key),
            "review_detail": review_for_candidate(
                review_snapshot, variant_id=row["variant_id"], product_id=row["product_id"],
                slot=row["slot"], selection_source="user_swap" if row["score"] is None else "automatic",
            ),
            "original_review_detail": original_review(
                review_snapshot, slot=row["slot"], current_variant_id=row["variant_id"],
            ),
            "reason": {"status": row["reason_status"], "text": row["reason"]},
            "checks": {"status": row["checks_status"], "text": row["checks"]},
            "alternatives_count": alternatives_count,
        })
    selected_price = sum(i["price"] * i["qty"] for i in items if i["selected"])
    selected_units = sum(i["qty"] for i in items if i["selected"])
    for item in items:
        item["budget_share"] = round(item["price"] * item["qty"] / selected_price, 3) if item["selected"] and selected_price else None
    budget_max = values.get("budget_max")
    result["items"] = items
    result["totals"] = {
        "selected_price": selected_price, "selected_units": selected_units,
        "budget_remaining": (budget_max - selected_price) if budget_max else None,
        "over_budget": bool(budget_max and selected_price > budget_max),
    }

    result["budget_notice"] = budget_notice(result["totals"], values)

    validations = erepo.get_validations(run["id"])
    penalty = sum((v["measured_values"] or {}).get("penalty", 0) for v in validations)
    confidence = max(0, 100 - penalty)
    for item in items:
        item["checks"] = _item_checks(item, validations, guide=item.get("checks"))
    result["verification"] = {
        "status": "ready", "confidence": confidence,
        "issues": [
            {"axis": v["rule_key"], "severity": "major" if v["severity"] in ("warning", "critical") else "minor", "text": v["message"]}
            for v in validations
        ],
    }
    contribution = next((step["contribution"] for step in (run.get("reasoning_log") or [])
                         if isinstance(step, dict) and step.get("contribution")), None)
    result["explanation"] = {
        "status": run.get("explanation_status") or "pending",
        "headline": run.get("explanation_headline"),
        "text": run.get("explanation_text"),
        "contribution": contribution,
    }
    result["compat_checks"] = compat_checks(conn, revision_id, values, candidate_rows)
    result["memo_suggestion"] = memo_suggestion(result, values)
    return result


# ── 결과 화면 상호작용 (§D-4-2: 담기/빼기·수량·구매시점 / 후보 교체 / 결과 대화) ──

def _require_done_run(conn, revision_id: UUID) -> tuple:
    from src.repo.engine_repo import EngineRepo
    erepo = EngineRepo(conn)
    run = erepo.get_latest_run(revision_id)
    if run is None or run["status"] != "completed":
        raise NotFound("추천 결과가 없습니다. 먼저 /recommend 를 호출하세요.")
    return erepo, run


def _find_candidate(rows: list[dict], item_id: UUID) -> dict:
    for row in rows:
        if row["id"] == item_id:
            return row
    raise NotFound("해당 품목을 찾을 수 없습니다.")


def patch_item(conn, revision_id: UUID, item_id: UUID, *, selected: bool | None,
                qty: int | None, timing: str | None, user_id: UUID | None = None) -> dict:
    from src.repo.plan_repo import PlanRepo
    from src.services import feedback_service

    erepo, run = _require_done_run(conn, revision_id)
    current = _find_candidate(erepo.get_candidates(run["id"]), item_id)
    if selected is True:
        values = {r["condition_key"]: r["value"].get("value")
                  for r in PlanRepo(conn).load_full(revision_id)["conditions"]}
    erepo.update_candidate_state(item_id, selected=selected, qty=qty, timing=timing)
    if selected is not None or qty is not None:
        reverify_set(conn, revision_id, run)      # 빼거나 수량을 바꾸면 총액(예산)·점검 대상 부품이 달라진다

    # P8 FB03: selected true→false는 "이 항목을 뺐다" — 담아 두는 동안의 수량/시점 조정은
    # 그 자체로 이벤트가 아니다(빈 것을 담았다 뺐다 하는 게 아니라, 실제로 제외했을 때만).
    if current["selected"] and selected is False:
        revision = PlanRepo(conn).get_revision(revision_id)
        feedback_service.emit_removed(
            conn, plan_id=revision["plan_id"], revision_id=revision_id,
            run_id=run["id"], item_id=item_id, version=revision["lock_version"],
            user_id=user_id,
            payload={"removed_variant_id": str(current["variant_id"]), "slot": current["slot"]},
        )
    return get_stored_result(conn, revision_id)


# 슬롯별로 spec_summary에 넣을 (specs 키, 포맷) — catalog_repo._specs_from_row가 채우는 키만 쓴다.
# 값이 없으면 그 조각은 뺀다(없는 스펙을 지어내지 않는다). CPU 코어 수는 스펙 테이블에 없어 뺐다.
_SPEC_DISPLAY_FIELDS: dict[str, list[tuple[str, str]]] = {
    "CPU": [("socket", "{} 소켓"), ("mem_type", "{}")],
    "GPU": [("vram_gb", "VRAM {:g}GB"), ("pcie_interface", "{}"), ("power_w", "{}W")],
    "RAM": [("mem_type", "{}"), ("capacity_gb", "{:g}GB"), ("speed_mts", "{}MT/s")],
    "메인보드": [("socket", "{} 소켓"), ("mem_type", "{}"), ("form_factor", "{}")],
    "저장장치": [("interface", "{}"), ("protocol", "{}")],
    "파워": [("wattage_w", "{}W"), ("efficiency_rating", "80+ {}"), ("form_factor", "{}")],
    "케이스": [("case_type", "{}")],
    "쿨러": [("cooling_type", "{}"), ("supported_socket", "{}")],
}


def _spec_summary(slot: str, specs: dict | None) -> str | None:
    """개발요청 3번 — 슬롯별 실제 스펙 문자열. "성능 티어 N"은 맨 끝에 그대로 둔다 —
    result_agent.py(100~102행)가 split("티어")로 파싱하는 형식을 안 바꾼다."""
    specs = specs or {}
    parts = [fmt.format(specs[key]) for key, fmt in _SPEC_DISPLAY_FIELDS.get(slot, []) if specs.get(key) not in (None, "")]
    tier = specs.get("perf_tier")
    if tier is not None:
        parts.append(f"성능 티어 {tier:g}")
    return " · ".join(parts) if parts else None


def _current_candidate_as_variant_row(current: dict) -> dict:
    """engine.get_candidates() 행(`product_name` 등 다른 컬럼명)을 candidates_by_slot() 행과 같은
    모양으로 바꿔 `_alternative_out`에 그대로 넘길 수 있게 한다."""
    return {
        "variant_id": current["variant_id"], "product_id": current.get("product_id"),
        "product_key": current.get("product_key"), "product_type": current.get("product_type"),
        "name": current.get("product_name"), "brand": current.get("brand"),
        "attributes": current.get("attributes"), "image_url": current.get("image_url"),
        "purchase_url": current.get("purchase_url"), "price": current.get("price"),
    }


def _alternative_out(row: dict, *, current: bool, current_price: int, slot: str, specs: dict | None = None,
                     review_snapshot: dict | None = None) -> dict:
    from src.repo.catalog_repo import PC_TYPE_TO_SLOT, pc_catalog_key
    from src.services.recommendation_review_snapshot import review_for_candidate
    price = int(row["price"]) if row.get("price") is not None else 0
    delta = price - current_price
    label = ("현재 선택" if current else
             "절약형 후보" if delta < 0 else
             "프리미엄 후보" if delta > 0 else "동급 후보")
    product_key = (pc_catalog_key(row["product_type"], row["brand"], row["product_key"])
                   if row.get("product_type") in PC_TYPE_TO_SLOT else row.get("product_key") or str(row["product_id"]))
    return {
        "candidate_id": str(row["variant_id"]), "label": label, "current": current,
        "product": {
            "product_key": product_key,
            "variant_id": str(row["variant_id"]), "name": row["name"], "brand": row.get("brand") or "",
            "spec_summary": _spec_summary(slot, specs),
            "image_url": row.get("image_url"), "purchase_url": row.get("purchase_url"),
        },
        "price": price, "price_delta": delta, "review": None,
        "review_detail": review_for_candidate(
            review_snapshot, variant_id=row["variant_id"], product_id=row.get("product_id"),
            slot=slot, selection_source="alternative",
        ),
    }


def _drop_incompatible_alternatives(conn, stored: list[dict], current: dict, variants: list[dict],
                                    cvals: dict) -> list[dict]:
    """PC: 지금 구성(과 업그레이드에서 유지하는 부품)과 소켓·메모리·크기·전력이 *확정적으로* 안 맞는
    후보를 대안 목록에서 뺀다. 교체 때문에 새로 생기는 비호환만 본다 — 현재 구성에 이미 있는 문제는
    후보 탓이 아니다. 판정에 쓸 정보가 없는 후보는 남긴다(모르는 것을 비호환으로 단정하지 않는다).
    재현: 예전엔 CPU 대안 39개 중 23개가 메인보드 소켓과 안 맞았고 가격순 목록 맨 위부터 나왔다."""
    if cvals.get("category") != "computer":
        return variants
    from src.engine.owned_parts import owned_for_conditions
    from src.engine.stage2_requirement import load_computer_rules
    from src.engine.stage4_optimize import _pc_known_failures
    from src.repo.catalog_repo import load_candidates_by_slot_from_db
    from src.services.live_spec_lookup import cached_for_kept_parts

    pool = load_candidates_by_slot_from_db(conn)
    by_variant = {c.variant_id: c for cands in pool.values() for c in cands}
    slot = current["slot"]
    chosen = {}
    for row in stored:
        cand = by_variant.get(str(row["variant_id"]))
        if row["slot"] != slot and row.get("selected") is not False and cand is not None:
            chosen[row["slot"]] = cand
    owned = owned_for_conditions(cvals, pool, {r["slot"] for r in stored},
                                 load_category("computer").get("slot_structure", []))
    cached_for_kept_parts(conn, owned, cvals.get("current_specs"))
    spec = RequirementSpec(
        list_id="alternatives", category="computer",
        mode="upgrade" if cvals.get("mode") == "upgrade" else "build",
        owned=owned,
    )
    rules = load_computer_rules()["verification"]
    now = by_variant.get(str(current["variant_id"]))
    baseline = _pc_known_failures({**chosen, slot: now}, spec, rules) if now is not None else set()
    kept = []
    for row in variants:
        cand = by_variant.get(str(row["variant_id"]))
        if cand is not None and _pc_known_failures({**chosen, slot: cand}, spec, rules) - baseline:
            continue
        kept.append(row)
    return kept


def _unknown_games(spec) -> list[str]:
    return [u["value"] for u in spec.unresolved if u.get("key") == "games"]


def _explanation_extras(spec, tier_notes: list[str] | None = None) -> dict:
    """결과에 코드가 붙이는 안내: 업그레이드의 견적 범위(요약 뒤)·미확인 항목, 요구사양 표에 없는 게임."""
    caveats: list[str] = []
    out: dict = {}
    if spec.mode == "upgrade" and spec.targets:
        from src.engine.owned_parts import upgrade_notes, upgrade_scope_note

        caveats += upgrade_notes(list(spec.targets), spec.owned)
        caveats += tier_notes or []
        out["scope_note"] = upgrade_scope_note(spec.targets)
    unknown = _unknown_games(spec)
    if unknown:
        caveats.append(f"게임 {', '.join(repr(g) for g in unknown)}은(는) 요구사양 표에 없어 해상도 기준으로 구성했습니다 — "
                       "해당 게임의 권장 사양을 직접 확인해 주세요.")
    # E7 — 인식된 게임 제목 중 팀 확인 전(provisional) 값이 하나라도 있으면 코드가 caveat 을 붙인다
    # (LLM 문안 아님, 결정 0003과 같은 원칙 — 판정·수치는 코드). 표에 없는 게임 caveat 과는 별개다.
    if "games_provisional" in spec.flags:
        caveats.append("게임별 요구 등급은 배급사 권장 사양을 대략 옮긴 잠정값이라, "
                       "구매 전 해당 게임의 공식 권장 사양을 확인해 주세요.")
    if caveats:
        out["extra_caveats"] = caveats
    return out


def _games_trace_row(spec) -> tuple[str, str] | None:
    """게임 제목이 요구 사양에 반영됐으면 추론 과정에 한 줄 — 화면 "분석 과정"에서 근거로 보인다.

    E7 — `game_titles`의 승인 상태(팀 확인 전 provisional / 배급사 권장 사양 확인된 approved)를
    구분해 서술한다. 잠정값은 "반영했다"처럼 단정하지 않는다 — 후보를 실제로 탈락시키는 값인데
    근거가 아직 팀 확인 전이기 때문이다(R-11).
    """
    if "games_applied" not in spec.flags:
        return None
    gpu = (spec.targets.get("GPU") or {}).get("perf_tier_min")
    cpu = (spec.targets.get("CPU") or {}).get("perf_tier_min")
    tier_note = f" (GPU 등급 {gpu} 이상, CPU 등급 {cpu} 이상)" if gpu is not None and cpu is not None else ""
    games = spec.games
    approved = [g for g in games if g["status"] == "approved"]

    if not games or not approved:
        # 인식된 제목을 모두 잠정값으로 다룬다(games 를 못 얻은 방어적 경우도 포함) — 단정하지 않는다.
        detail = "입력한 게임의 권장 사양을 대략 옮긴 잠정 기준으로 최소 등급을 정했습니다" + tier_note
    elif len(approved) == len(games):
        detail = "입력한 게임의 배급사 권장 사양을 확인해 최소 등급을 정했습니다" + tier_note
    else:
        per_title = [
            f"{g['label']}: 배급사 권장 사양 확인({(g.get('source') or {}).get('checked_at')})"
            if g["status"] == "approved" else f"{g['label']}: 잠정값"
            for g in games
        ]
        detail = "게임별 요구 등급 확인 상태 — " + ", ".join(per_title) + tier_note
    unknown = _unknown_games(spec)
    if unknown:
        detail += f". 표에 없는 게임: {', '.join(unknown)}"
    return ("게임 요구사양", detail)


_SIMILAR_TIER_ALTERNATIVES = 3


def _name_dedupe_key(name: str) -> str:
    """표기만 다른 같은 제품(`RTX 3050 (6GB)` vs `RTX 3050 6GB`)을 하나로 묶는 키."""
    return re.sub(r"[^0-9a-z가-힣]", "", (name or "").lower())


def _dedupe_alternatives_by_name(variants: list[dict]) -> list[dict]:
    """이름만 다르게 중복 등록된 같은 제품 중 더 싼 쪽만 남긴다(개발요청 2번)."""
    best: dict[str, dict] = {}
    order: list[str] = []
    for row in variants:
        key = _name_dedupe_key(row.get("name"))
        price = row["price"] if row.get("price") is not None else 0
        prev = best.get(key)
        if prev is None:
            order.append(key)
            best[key] = row
        elif price < (prev["price"] if prev.get("price") is not None else 0):
            best[key] = row
    return [best[k] for k in order]


def list_alternatives(conn, revision_id: UUID, item_id: UUID) -> dict:
    from src.repo.catalog_repo import load_specs_by_variant
    from src.repo.product_repo import ProductRepo
    erepo, run = _require_done_run(conn, revision_id)
    from src.services.recommendation_review_snapshot import snapshot_from_run
    review_snapshot = snapshot_from_run(run)
    stored = erepo.get_candidates(run["id"])
    current = _find_candidate(stored, item_id)
    current_price = int(current["price"]) if current["price"] is not None else 0
    slot_variants = ProductRepo(conn).candidates_by_slot().get(current["slot"], [])
    from src.repo.plan_repo import PlanRepo
    cvals = {r["condition_key"]: r["value"].get("value") for r in PlanRepo(conn).load_full(revision_id)["conditions"]}
    slot_variants = _drop_incompatible_alternatives(conn, stored, current, slot_variants, cvals)
    slot_variants = _dedupe_alternatives_by_name(slot_variants)
    others = [row for row in slot_variants if row["variant_id"] != current["variant_id"]]

    specs_by_variant = load_specs_by_variant(conn, [
        (row.get("product_type"), str(row["variant_id"])) for row in [current, *others]
    ])
    current_tier = specs_by_variant.get(str(current["variant_id"]), {}).get("perf_tier")
    if current_tier is not None:
        # 워크스테이션·전문가용까지 섞인 전체 카탈로그 대신, 지금 고른 성능 등급에 가까운 후보만
        # 추린다(개발요청 2번 — "비슷한 성능 3개"). 같은 등급 차이면 가격이 가까운 쪽을 우선한다.
        def _distance(row: dict) -> tuple[float, int]:
            tier = specs_by_variant.get(str(row["variant_id"]), {}).get("perf_tier")
            tier_gap = abs(float(tier) - float(current_tier)) if tier is not None else float("inf")
            price = row["price"] if row.get("price") is not None else 0
            return (tier_gap, abs(price - current_price))

        others = sorted(others, key=_distance)[:_SIMILAR_TIER_ALTERNATIVES]

    items = [_alternative_out(_current_candidate_as_variant_row(current), current=True, current_price=current_price,
                               slot=current["slot"], specs=specs_by_variant.get(str(current["variant_id"])),
                               review_snapshot=review_snapshot)]
    items += [
        _alternative_out(row, current=False, current_price=current_price, slot=current["slot"],
                          specs=specs_by_variant.get(str(row["variant_id"])), review_snapshot=review_snapshot)
        for row in sorted(others, key=lambda r: r["price"] if r["price"] is not None else 0)
    ]
    return {"items": items}


def swap_item(conn, revision_id: UUID, item_id: UUID, candidate_id: UUID,
              user_id: UUID | None = None) -> dict:
    """candidate_id는 alternatives가 돌려준 variant_id다. item_id(행 자체)는 그대로 두고
    내용만 바꿔치기한다 — 계약상 item_id는 후보 교체 후에도 고정."""
    from src.repo.product_repo import ProductRepo
    from src.repo.plan_repo import PlanRepo
    from src.services import feedback_service

    erepo, run = _require_done_run(conn, revision_id)
    current = _find_candidate(erepo.get_candidates(run["id"]), item_id)
    slot_variants = ProductRepo(conn).candidates_by_slot().get(current["slot"], [])
    target = next((row for row in slot_variants if row["variant_id"] == candidate_id), None)
    if target is None:
        raise NotFound("해당 후보를 찾을 수 없습니다.")
    erepo.update_candidate_variant(item_id, variant_id=candidate_id,
                                    offer_observation_id=target.get("offer_observation_id"))
    # update_candidate_variant 가 reason 을 pending 으로 되돌리는데 다시 채우는 경로가 없어서 화면의
    # "추천 이유" 가 영원히 "정리하는 중…" 이었다. [5] 를 다시 돌릴 수 없으니(rank·build 는 메모리에만
    # 있었다) 코드가 아는 사실만으로 한 줄 적는다 — 판단이 아니라 교체 기록이다.
    old_price = int(current["price"]) if current["price"] is not None else 0
    new_price = int(target["price"]) if target.get("price") is not None else 0
    cvals = {r["condition_key"]: r["value"].get("value") for r in PlanRepo(conn).load_full(revision_id)["conditions"]}
    delta = fmt_money(new_price - old_price, signed=True)
    erepo.update_candidate_reason(item_id, (f"사용자 요청으로 교체한 부품입니다 — 자동 추천은 '{current['product_name']}'({fmt_money(old_price)})였고 "
        f"이 후보는 {delta}입니다. 순위는 교체 전 구성 기준이고, 호환 점검은 교체 후 구성으로 다시 했습니다."))
    reverify_set(conn, revision_id, run)
    # 요약(explanation)도 교체 전 구성 기준이다. [5] 를 다시 돌릴 수 없으니 그 사실을 본문 끝에 적는다.
    if run.get("explanation_status") == "ready" and run.get("explanation_text"):
        note = f"※ 이후 {current['slot']}를 '{target['name']}'(으)로 교체했습니다({delta}). 이 요약은 교체 전 구성 기준입니다."
        erepo.set_explanation(run["id"], headline=run.get("explanation_headline") or "",
                              text=run["explanation_text"].rstrip() + "\n\n" + note,
                              reasoning_log=run.get("reasoning_log") or [])

    # P8 FB03: 실제로 바꿔치기가 성공한 뒤에만 기록한다 — 위의 not-found 거부는 아무 것도
    # 남기지 않는다.
    # 무엇에서 무엇으로 바꿨는지는 payload 에 남긴다 — 선호 신호 배치(from/to·slot)와 견적 리스트 히스토리
    # (item_id)가 읽는다. item_id 는 키에만 들어가고 컬럼이 없어서 payload 에도 넣는다. 같은 품목을 다시 바꾸면
    # seq 로 키를 달리한다(lock_version 은 조건이 바뀔 때만 올라 두 번째 교체가 중복으로 버려졌다).
    revision = PlanRepo(conn).get_revision(revision_id)
    feedback_service.emit_replaced(
        conn, plan_id=revision["plan_id"], revision_id=revision_id,
        run_id=run["id"], item_id=item_id, version=revision["lock_version"],
        user_id=user_id, seq=feedback_service.replaced_count(conn, run_id=run["id"], item_id=item_id),
        payload={"item_id": str(item_id), "from_variant_id": str(current["variant_id"]),
                 "to_variant_id": str(candidate_id), "slot": current["slot"]},
    )
    return get_stored_result(conn, revision_id)


_SLOT_SYNONYMS = SLOT_SYNONYMS    # 선호 신호 저장도 같은 표를 쓴다(src/engine/brands.py)
_CHEAPER_WORDS = ("저렴", "싸게", "싼", "가성비", "낮은", "절약")
_PRICIER_WORDS = ("고급", "좋은", "성능", "비싼", "상위", "프리미엄")
_SUMMARY_WORDS = ("총평", "전체 평가", "요약")
_PREVIOUS_WORDS = ("지난번", "저번", "이전 견적", "전 견적", "예전 견적", "지난 견적")
_DIFF_WORDS = ("달라", "다른", "차이", "비교", "바뀐", "바꼈", "바뀌", "변했", "변한")


def _match_slot(text: str, known_slots: set[str]) -> str | None:
    lowered = text.lower()
    for keyword, slot in _SLOT_SYNONYMS.items():
        if keyword in lowered and slot in known_slots:
            return slot
    return next((slot for slot in known_slots if slot.lower() in lowered), None)


def _is_previous_comparison_request(text: str) -> bool:
    """"지난번이랑 뭐가 달라?" — 이전 견적과 비교해 달라는 말(B1)."""
    return any(w in text for w in _PREVIOUS_WORDS) and any(w in text for w in _DIFF_WORDS)


def _is_result_summary_request(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _SUMMARY_WORDS)


# 질문 표지 — 방향어("성능"·"가성비")가 들어 있어도 묻는 말이면 교체하지 않는다.
# "이 그래픽카드 성능 괜찮아?" 가 RTX 5070 Ti 로 교체되던 오탐(2026-09-14 실측).
_QUESTION_MARKERS = ("?", "？", "괜찮", "나아", "어때", "맞아", "일까", "인가", "할까", "좋을까", "뭐", "어떤", "무엇")


def _parse_swap_request(text: str, known_slots: set[str]) -> tuple[str | None, str | None, bool]:
    """규칙 경로의 해석 — (슬롯, 방향 'cheaper'|'pricier'|None, 질문인가). 순수 함수라 테스트 가능."""
    slot = _match_slot(text, known_slots)
    cheaper = any(w in text for w in _CHEAPER_WORDS)
    pricier = any(w in text for w in _PRICIER_WORDS)
    direction = "cheaper" if cheaper and not pricier else "pricier" if pricier and not cheaper else None
    is_question = any(m in text for m in _QUESTION_MARKERS) or (cheaper and pricier)
    return slot, direction, is_question


def handle_result_message(conn, revision_id: UUID, text: str, user_id: UUID | None = None) -> dict:
    """결과 화면 채팅 — 실제 처리는 `_handle_result_message_inner`, 여기서는 그 앞뒤로 대화를 저장한다
    (CHAT-08). 조건 대화(session_service)와 같은 `identity.conversation`에 시간순으로 쌓여, 저장한
    견적을 다시 열면 `GET /session/{id}`가 조건 대화·결과 대화를 이어서 그대로 복원한다 — 새 API가
    필요 없다. 에이전트가 이어 말하기 맥락을 볼 때도 이 DB 이력을 쓴다(`result_agent._history_messages`).

    `user_id`는 채팅으로 바꾼 부품도 버튼 스왑과 같은 신호 소스로 잡히게 한다
    (docs/사용자_선호비선호_기록_설계.md — 이전엔 이 경로만 빠져 있었다)."""
    from src.repo.plan_repo import PlanRepo
    from src.repo.user_repo import ConversationRepo

    conversation_id = PlanRepo(conn).get_revision(revision_id)["conversation_id"]
    convo = ConversationRepo(conn)
    # 사용자 말을 먼저 저장한다 — 메시지 시각이 저장 순간(clock_timestamp)이라, 처리 뒤에 저장하면 이 말 때문에 바뀐
    # 부품(교체 이벤트)이 말보다 앞선 시각이 되어 히스토리가 "말 → 바뀐 것"을 잇지 못한다. 에이전트의 이전 대화
    # 맥락(result_agent._db_history)은 질문·답 쌍만 읽으므로 아직 답이 없는 이 말은 섞이지 않는다.
    # 처리하다 실패하면 트랜잭션째 롤백되어 이 말도 남지 않는다(예전과 같다).
    convo.add_message(conversation_id, "user", text)
    turn_log: dict = {"path": "rules"}
    started = time.perf_counter()
    turn = _handle_result_message_inner(conn, revision_id, text, user_id=user_id, turn_log=turn_log)
    from src.agent.result_agent import with_condition_notice
    noticed = with_condition_notice(text, turn["reply"])
    if noticed != turn["reply"]:
        turn["reply"], turn_log["notice"] = noticed, "conditions"
    turn_log["latency_ms"] = round((time.perf_counter() - started) * 1000)
    convo.add_message(conversation_id, "assistant", turn["reply"], metadata={"turn": turn_log})
    return turn


def _handle_result_message_inner(
    conn,
    revision_id: UUID,
    text: str,
    user_id: UUID | None = None,
    turn_log: dict | None = None,
) -> dict:
    """결과 화면 채팅의 실제 처리. 에이전트(RESULT_AGENT=1)가 있으면 도구 호출로 후보 조회·교체·담기/빼기·
    근거 설명을 처리하고, 없거나 실패하면 아래 규칙 경로 — "그래픽카드를 더 저렴한 걸로" 같은 요청만
    해석하고 슬롯·방향을 못 찾으면 아무것도 바꾸지 않고 이해하지 못했다는 답만 돌려준다."""
    # turn_log — 이 턴의 기록(P1-3). 호출자가 답 메시지의 metadata["turn"]에 남긴다. 화면 응답에는 나가지 않는다
    # (session_service._messages_out 은 metadata 를 내보내지 않는다). 키: path(agent | rules | rules_after_agent_error
    # | previous_compare), intent, tools, trace, outputs(도구 결과 원문), changed, guard, error, model, tokens,
    # latency_ms.
    turn_log = {} if turn_log is None else turn_log
    # 이전 견적 비교는 규칙으로 답한다(B1) — 에이전트는 이전 견적을 모르므로 먼저 가로챈다.
    if _is_previous_comparison_request(text):
        turn_log["path"] = "previous_compare"
        from src.services.previous_compare import compare_with_previous
        return {"reply": compare_with_previous(conn, revision_id)["text"], "result": get_stored_result(conn, revision_id)}
    from src.agent import result_agent
    from src.config import LLM_MODEL
    if result_agent.available():
        _require_done_run(conn, revision_id)
        try:
            turn = result_agent.run_turn(conn, revision_id, get_stored_result(conn, revision_id), text,
                                         user_id=user_id)
            log.info("result agent [%s]: %s", revision_id, " | ".join(turn.trace) or "(도구 호출 없음)")
            turn_log.update(path="agent", intent=turn.intent, tools=[c.split("(", 1)[0] for c in turn.trace],
                            trace=turn.trace, outputs=turn.outputs, changed=turn.changed, guard=turn.guard, error=turn.error,
                            model=LLM_MODEL, tokens=turn.usage)
            return {"reply": turn.reply, "result": turn.result}
        except Exception as exc:  # noqa: BLE001 — 모델·네트워크 오류는 이번 턴만 규칙으로
            log.warning("result agent failed, falling back to rules: %s", exc)
            # 실패한 호출의 토큰은 모른다(예외가 에이전트를 감춘다) — 경로와 예외 이름만 남긴다
            turn_log.update(path="rules_after_agent_error", error=type(exc).__name__, model=LLM_MODEL)
            import psycopg
            if isinstance(exc, psycopg.Error):
                conn.rollback()        # 실패한 트랜잭션 위에서는 규칙 경로의 SQL 도 전부 거부된다

    erepo, run = _require_done_run(conn, revision_id)
    result = get_stored_result(conn, revision_id)
    if _is_result_summary_request(text):
        explanation = (result or {}).get("explanation") or {}
        summary = explanation.get("text") or explanation.get("headline")
        if summary:
            return {"reply": summary, "result": result}
        return {"reply": "전체 구성 총평을 아직 준비하고 있어요. 잠시 후 다시 물어봐 주세요.", "result": result}

    rows = erepo.get_candidates(run["id"])
    from src.repo.plan_repo import PlanRepo
    values = {r["condition_key"]: r["value"].get("value")
              for r in PlanRepo(conn).load_full(revision_id)["conditions"]}
    known_slots = {r["slot"] for r in rows}
    slot, direction, is_question = _parse_swap_request(text, known_slots)
    from src.agent.result_agent import classify_intent
    from src.services import result_advice
    intent = classify_intent(text)
    if not (slot and direction and intent == "ask"):
        # 남은 예산·절약·점검·게임처럼 슬롯·방향 없이 묻는 말 — 에이전트와 같은 계산 함수(P5 규칙 경로)
        advice = result_advice.rule_reply(conn, revision_id, text, slot)
        if advice is not None:
            return {"reply": advice, "result": result}
    if slot is None:
        return {"reply": "무엇을 바꿀지 이해하지 못했어요. 부품 이름(예: 그래픽카드)과 원하시는 "
                          "방향(더 저렴한/더 좋은)을 함께 말씀해 주세요.",
                "result": get_stored_result(conn, revision_id)}
    hint = ({"cheaper": "더 저렴한", "pricier": "더 좋은"}.get(direction) or "더 저렴한/더 좋은")
    whatif = intent == "ask"
    preview_ok = whatif and values.get("category") == "computer"
    # 물음표가 있어도 "바꿔줄래?"처럼 바꾸라는 말이면 바꾼다
    if direction is None or (((is_question and intent != "change") or whatif) and not preview_ok):
        # 묻는 말(또는 방향이 애매한 말)은 실행하지 않고 되묻는다 — "바꿔드릴까요?" 는 사용자가 확정해야 룰 동작이 된다
        return {"reply": f"{slot}를 {hint} 후보로 바꿔드릴까요? 바꾸려면 '{slot} {hint} 걸로'라고 말씀해 주세요. "
                          "지금은 아무것도 바꾸지 않았어요.",
                "result": get_stored_result(conn, revision_id)}
    cheaper = direction == "cheaper"

    current = next(r for r in rows if r["slot"] == slot)
    current_price = int(current["price"]) if current["price"] is not None else 0
    from src.repo.product_repo import ProductRepo
    slot_variants = ProductRepo(conn).candidates_by_slot().get(slot, [])
    others = [r for r in slot_variants if r["variant_id"] != current["variant_id"] and r["price"] is not None]
    # 지금 구성과 확정적으로 안 맞는 후보(소켓·크기·전력 …)는 채팅으로도 고르지 않는다 — 대안 목록과 같은 기준.
    others = _drop_incompatible_alternatives(conn, rows, current, others, values)
    # "더 저렴한"/"더 좋은"은 방향이 있는 요청이다 — 후보가 있어도 그 방향으로 안 가면
    # (지금이 이미 최저가/최고가) 엉뚱한 방향으로 바꾸지 않고 그렇다고 말한다.
    candidates = [r for r in others if r["price"] < current_price] if cheaper \
        else [r for r in others if r["price"] > current_price]
    if not candidates:
        state = "가장 저렴해요" if cheaper else "가장 고급이에요"
        return {"reply": f"지금 선택된 {slot}가 이미 {state}. 더 {'저렴한' if cheaper else '좋은'} 후보가 없어요.",
                "result": get_stored_result(conn, revision_id)}
    # 한 단계: "더 저렴한"은 지금보다 싼 것 중 가장 비싼 것, "더 좋은"은 지금보다 비싼 것 중 가장 싼 것.
    # (전에는 최저가/최고가로 바로 뛰어서 "더 좋은 걸로"가 목록에서 가장 비싼 부품이 됐다.)
    target = max(candidates, key=lambda r: r["price"]) if cheaper else min(candidates, key=lambda r: r["price"])
    if preview_ok:
        # "CPU 더 좋은 걸로 바꿔도 문제없어?" — 바꾸지 않고 그 한 단계 후보로 바꿨을 때를 계산해 보여 준다
        preview = result_advice.preview_swap(conn, revision_id, slot, str(target["variant_id"]))
        return {"reply": preview + f"\n바꾸려면 '{slot} {hint} 걸로 바꿔줘'라고 말씀해 주세요.", "result": result}
    # swap_item 을 거쳐야 교체 기록 reason 이 같이 적힌다 (직접 update_candidate_variant 하면 pending 으로 남는다)
    result = swap_item(conn, revision_id, current["id"], target["variant_id"], user_id=user_id)
    tier = "더 저렴한" if cheaper else "더 좋은"
    return {"reply": f"{slot}를 {tier} '{target['name']}'(으)로 바꿨어요.", "result": result}
