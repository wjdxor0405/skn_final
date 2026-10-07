"""결과 화면의 "묻는 말" 계산(src/services/result_advice.py)과 규칙 경로 연결 — 실제 카탈로그 DB 로.

남은 예산으로 올릴 후보, 바꾸면 어떻게 되나(저장 안 함), 줄일 수 있는 것, 게임 요구 사양 비교.
공통 확인: 어떤 함수도 구성표를 바꾸지 않는다.
"""
from __future__ import annotations

import re
import uuid

import psycopg
import pytest

from src.auth.deps import Principal
from src.config import DATABASE_URL
from src.repo.engine_repo import EngineRepo
from src.repo.plan_repo import PlanRepo
from src.services import recommendation_service, result_advice, session_service

_ID = re.compile(r"candidate_id=([0-9a-f-]{36})")


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DATABASE_URL, prepare_threshold=None, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("로컬 PostgreSQL(DATABASE_URL)에 연결할 수 없습니다 — db/setup_all.py로 준비하세요.")
    try:
        yield connection
    finally:
        connection.close()


def _build(conn, budget: int = 1_500_000, purpose: str = "game") -> uuid.UUID:
    created = session_service.create_session(conn, Principal(user_id=None, browser_token=None))
    principal = Principal(user_id=None, browser_token=created["browser_token"])
    list_uuid = uuid.UUID(created["list_id"])
    session_service.choose_category(conn, list_uuid, "computer", "build", principal)
    for field, value in (("purpose", purpose), ("budget_max", budget), ("priority", "value")):
        session_service.patch_slot(conn, list_uuid, field, value, principal)
    revision_id = PlanRepo(conn).get_current_revision(list_uuid)["id"]
    accepted = recommendation_service.start_recommendation(conn, revision_id, strategy="default")
    recommendation_service.execute_recommendation(revision_id, uuid.UUID(accepted["run_id"]))
    return revision_id


def _snapshot(conn, revision_id) -> list[tuple]:
    run = recommendation_service._require_done_run(conn, revision_id)[1]
    return [(r["slot"], str(r["variant_id"]), r["qty"], r["selected"]) for r in EngineRepo(conn).get_candidates(run["id"])]


def test_upgrade_options_lists_only_better_compatible_parts_within_budget(conn):
    revision_id = _build(conn, budget=3_000_000)
    before = _snapshot(conn, revision_id)
    out = result_advice.upgrade_options(conn, revision_id)
    ctx = result_advice._context(conn, revision_id)
    budget = ctx.budget_max - ctx.total()
    ids = _ID.findall(out)
    assert ids, out
    for cid in ids:
        cand = ctx.by_variant[cid]
        row = ctx.row(cand.slot)
        current = ctx.by_variant[str(row["variant_id"])]
        assert cand.slot in ("CPU", "GPU", "RAM")
        assert result_advice._metric(cand.slot, cand.specs) > result_advice._metric(cand.slot, current.specs)
        assert 0 < (cand.price - row["price"]) * (row["qty"] or 1) <= budget
        assert not result_advice._new_failures(ctx, cand.slot, cand)
        assert result_advice._requirement_verdict(ctx, cand.slot, cand)[0] != "Fail"
    assert _snapshot(conn, revision_id) == before


def test_upgrade_options_with_new_budget_reports_remaining_against_the_new_budget(conn):
    from src.agent.result_agent import ResultSession
    revision_id = _build(conn)                       # 예산 150만
    result = recommendation_service.get_stored_result(conn, revision_id)
    out = ResultSession(conn=conn, revision_id=revision_id, result=result).upgrade_options(new_budget="200만원")
    pairs = re.findall(r"바꾼 뒤 총액 ([\d,]+)원, 바꾼 뒤 잔여 (-?[\d,]+)원", out)
    assert pairs, out
    for after, left in pairs:
        assert int(left.replace(",", "")) == 2_000_000 - int(after.replace(",", "")), out


def test_upgrade_options_with_tiny_amount_says_nothing_fits(conn):
    revision_id = _build(conn)
    out = result_advice.upgrade_options(conn, revision_id, 1)
    assert "추가 금액 1원" in out and "올릴 수 있는 후보가 없습니다" in out and not _ID.findall(out)


def test_preview_swap_reports_socket_mismatch_without_saving(conn):
    revision_id = _build(conn)
    before = _snapshot(conn, revision_id)
    ctx = result_advice._context(conn, revision_id)
    board_socket = ctx.chosen()["메인보드"].specs["socket"]
    other = next(c for c in ctx.pool["CPU"] if c.specs.get("socket") and c.specs["socket"] != board_socket)
    out = result_advice.preview_swap(conn, revision_id, "CPU", other.variant_id)
    assert out.startswith("(가정 계산 — 구성표는 그대로)")
    assert "⚠ 호환 점검 문제" in out and "소켓" in out
    assert _snapshot(conn, revision_id) == before


def test_preview_swap_up_picks_the_next_tier_and_reports_budget(conn):
    revision_id = _build(conn)
    ctx = result_advice._context(conn, revision_id)
    out = result_advice.preview_swap(conn, revision_id, "GPU", direction="up")
    cand = ctx.by_variant[_ID.findall(out)[0]]
    now = result_advice._metric("GPU", ctx.chosen()["GPU"].specs)
    higher = [result_advice._metric("GPU", c.specs) for c in ctx.pool["GPU"]
              if (result_advice._metric("GPU", c.specs) or 0) > now and not result_advice._new_failures(ctx, "GPU", c)]
    assert result_advice._metric("GPU", cand.specs) == min(higher)
    assert "바꾼 뒤 총액" in out and ("잔여" in out or "예산 초과" in out)
    assert "전력:" in out or "⚠ 호환 점검 문제" in out


def test_savings_options_target_beyond_reach_says_so(conn):
    revision_id = _build(conn)
    out = result_advice.savings_options(conn, revision_id, 10_000_000)
    assert "을 줄이는 조합(부품 3개까지)은 없습니다" in out and "아직 아무것도 바꾸지 않았습니다" in out


def test_game_check_compares_table_tiers(conn):
    revision_id = _build(conn)
    out = result_advice.game_check(conn, revision_id, "사이버펑크")
    assert out.startswith("사이버펑크") and "GPU 성능 등급 요구" in out and "fps" in out
    assert "표에 없어" in result_advice.game_check(conn, revision_id, "없는게임123")


def test_rule_path_answers_questions_without_changing_the_build(conn):
    """에이전트가 없을 때(MOCK_MODE) — 묻는 말은 계산 결과로 답하고 구성표는 그대로다(P5)."""
    revision_id = _build(conn)
    before = _snapshot(conn, revision_id)

    up = recommendation_service.handle_result_message(conn, revision_id, "돈 남았는데 바꿀 거 추천해 줄 수 있나?")
    assert "올릴 수 있는 부품" in up["reply"] and "candidate_id" not in up["reply"]

    whatif = recommendation_service.handle_result_message(conn, revision_id, "그래픽카드 더 좋은 걸로 바꿔도 돼?")
    assert whatif["reply"].startswith("(가정 계산 — 구성표는 그대로)") and "바꿔줘" in whatif["reply"]

    power = recommendation_service.handle_result_message(conn, revision_id, "파워 용량 충분해?")
    assert "파워 용량" in power["reply"]
    assert "성능 등급과 이 용도 기준 등급: CPU" in power["reply"]          # 병목 질문의 근거 — 등급만, fps 아님

    game = recommendation_service.handle_result_message(conn, revision_id, "배그 돌아가?")
    assert "배틀그라운드" in game["reply"]
    assert _snapshot(conn, revision_id) == before

    # 바꾸라는 말은 예전처럼 바꾼다
    done = recommendation_service.handle_result_message(conn, revision_id, "그래픽카드를 더 저렴한 걸로 바꿔줘")
    assert "바꿨어요" in done["reply"] and _snapshot(conn, revision_id) != before


def test_step_down_prefers_parts_that_still_meet_the_requirement(conn):
    """"파워 더 싼 걸로"가 요구 용량·효율을 못 채우는 파워를 고르던 것 — 채우는 후보가 있으면 그 안에서 고른다."""
    revision_id = _build(conn)
    ctx = result_advice._context(conn, revision_id)
    cheaper_ok = [c for c in ctx.pool["파워"] if c.price < ctx.row("파워")["price"]
                  and result_advice._requirement_verdict(ctx, "파워", c)[0] != "Fail"
                  and not result_advice._new_failures(ctx, "파워", c)]
    cand = result_advice.step_candidate(ctx, "파워", "down")
    out = result_advice.preview_swap(conn, revision_id, "파워", direction="down")
    if cheaper_ok:
        assert cand.variant_id == max(cheaper_ok, key=lambda c: c.price).variant_id
        assert "요구 사양을 못 채움" not in out
    elif cand is not None:                      # 채우는 후보가 없으면 그 사실을 먼저 밝힌다
        assert out.startswith("이 견적의 요구 사양을 채우는 한 단계 아래 파워는 카탈로그에 없어") and "⚠" in out


def test_box_cooler_value_reaches_check_and_cpu_preview(conn):
    """카탈로그의 cpu_spec.cooler_included 를 채팅 근거로 쓴다 — 예전엔 아무도 읽지 않아 "데이터에 없다"고 답했다."""
    revision_id = _build(conn)
    ctx = result_advice._context(conn, revision_id)
    cpu_vid = ctx.row("CPU")["variant_id"]
    raw = result_advice.box_cooler(conn, cpu_vid)
    out = result_advice.check_build(conn, revision_id)
    assert f"CPU 기본 쿨러(카탈로그 값): {raw or '정보 없음'}" in out

    # 별도 쿨러를 빼고, 기본 쿨러가 없는 CPU 로 바꾼다고 가정하면 경고
    cooler = ctx.row("쿨러")
    recommendation_service.patch_item(conn, revision_id, cooler["id"], selected=False, qty=None, timing=None)
    ctx = result_advice._context(conn, revision_id)
    no_box = next((c for c in ctx.pool["CPU"]
                   if result_advice._has_box_cooler(result_advice.box_cooler(conn, c.variant_id)) is False
                   and not result_advice._new_failures(ctx, "CPU", c)), None)
    if no_box is not None:
        preview = result_advice.preview_swap(conn, revision_id, "CPU", no_box.variant_id)
        assert "⚠ 별도 쿨러가 빠져 있는데 이 CPU는 기본 쿨러가 없을 수 있음" in preview


def test_target_plan_loses_the_least_performance_not_the_most_money(conn):
    """"10만원 정도 절약할 부품" — 예전엔 절약액 큰 것부터 더해 CPU 를 등급 8→4 로 내리며 18.6만원을 줄였다(시연).
    목표를 채우는 조합 중 성능 손실이 가장 적은 것을 고른다: 목표를 채우는 어떤 단일 교체보다 손실이 크지 않다."""
    revision_id = _build(conn, budget=1_240_000, purpose="office")
    ctx = result_advice._context(conn, revision_id)
    options = {r["slot"]: result_advice._cheaper_options(ctx, r["slot"]) for r in ctx.picked}
    target = 100_000
    plan = result_advice._target_plan(ctx, options, target)
    singles = [o for opts in options.values() for o in opts if o[0] >= target]
    if plan is None:
        assert not singles
        return
    (loss, _, saving), swaps = plan
    assert saving >= target
    assert all(loss <= o[1] for o in singles)
    out = result_advice.savings_options(conn, revision_id, target)
    assert "성능을 가장 적게 잃는 조합" in out


def test_relaxed_target_plan_is_summed_by_code(conn):
    """"20만원 줄이려면" — 요구 사양을 지켜선 안 될 때, 기준을 낮춘 조합의 합계도 코드가 낸다(모델이 더하다 틀렸다)."""
    revision_id = _build(conn)
    ctx = result_advice._context(conn, revision_id)
    loose = {r["slot"]: result_advice._cheaper_options(ctx, r["slot"], allow_fail=True) for r in ctx.picked}
    plan = result_advice._target_plan(ctx, loose, 200_000)
    out = result_advice.savings_options(conn, revision_id, 200_000)
    if result_advice._target_plan(ctx, {r["slot"]: result_advice._cheaper_options(ctx, r["slot"]) for r in ctx.picked},
                                  200_000) is None and plan is not None:
        (_, _, saving), _ = plan
        assert f"요구 사양을 낮추면 200,000원 이상 줄일 수 있는 조합" in out and f"합계 절약 {saving:,}원" in out


def test_relaxing_only_lowers_performance_never_power_or_socket(conn):
    revision_id = _build(conn)
    ctx = result_advice._context(conn, revision_id)
    for row in ctx.picked:
        for saving, loss, cand in result_advice._cheaper_options(ctx, row["slot"], allow_fail=True):
            verdict, reasons = result_advice._requirement_verdict(ctx, row["slot"], cand)
            if verdict == "Fail":
                assert row["slot"] in ("CPU", "GPU", "RAM"), (row["slot"], reasons)


def test_swap_refuses_a_candidate_below_the_build_requirement(conn):
    """실패 7(10/3 리허설) — "램 더 싼 걸로 바꿔줘"에 32GB 가 8GB 로 바뀌었다. 요구 사양 미달 후보는 도구가 바꾸지 않고,
    후보 목록에도 이유와 함께 ⚠ 를 단다(성능 등급만 보던 목록은 용량 미달을 못 잡았다)."""
    from src.agent.result_agent import ResultSession
    revision_id = _build(conn, purpose="video_edit")
    ctx = result_advice._context(conn, revision_id)
    result = recommendation_service.get_stored_result(conn, revision_id)
    session = ResultSession(conn=conn, revision_id=revision_id, result=result, read_only=False)
    found = None
    for slot in ("RAM", "CPU", "GPU", "파워", "저장장치"):
        if ctx.row(slot) is None:
            continue
        for line in session.list_alternatives(slot).splitlines():
            cid = (_ID.findall(line) or [None])[0]
            cand = ctx.by_variant.get(cid) if cid else None
            if cand is not None and result_advice._requirement_verdict(ctx, slot, cand)[0] == "Fail":
                found = (slot, cid, line)
                break
        if found:
            break
    assert found, "후보 목록에 요구 사양을 못 채우는 후보가 없다"
    slot, cid, line = found
    assert "⚠ 요구 사양 미달(" in line, line
    before = _snapshot(conn, revision_id)
    out = session.swap(slot, cid)
    assert out.startswith("바꾸지 않음"), out
    assert not session.changed and _snapshot(conn, revision_id) == before
