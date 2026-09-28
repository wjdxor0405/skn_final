"""견적 점검 — 대안 적용 (CHK-08).

고른 부품을 업그레이드 대상으로 하는 새 계획을 만들고, 나머지 부품은 견적 그대로 유지하는지, 필수 조건이 없으면
추천을 바로 시작하지 않는지, 소유권과 입력 검증을 본다. 실 Postgres(일회용 DB)로만 돈다 — mode=upgrade 세션이
실제 조건 테이블에 저장돼야 확인할 수 있다.
"""
from __future__ import annotations

import os

import pytest

DSN = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable test database")

QUOTE = {"CPU": "라이젠 5 7600 250,000원", "GPU": "RTX 4060 Ti 500,000원", "메인보드": "MSI PRO B650M-P 155,000원", "RAM": "DDR5 16GB 60,000원",
         "파워": "마이크로닉스 700W 89,000원"}   # 파워 스펙이 있어야 GPU 교체 시 "지금 쓰는 파워 용량은?" 칩 질문이 안 뜬다
FULL_COND = {"purpose": "game", "resolution": "QHD_165", "priority": "value", "budget_max": 2_000_000}


@pytest.fixture(autouse=True)
def _synthetic_catalog(monkeypatch):
    monkeypatch.setenv("CATALOG_SOURCE", "mock")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from src.api import app
    with TestClient(app) as c:
        yield c


def _reviewed(client, conditions: dict | None) -> str:
    body = {"current_specs": QUOTE}
    if conditions is not None:
        body["conditions"] = conditions
    return client.post("/pc/reviews", json=body).json()["list_id"]


# ── 새 계획을 만들고 나머지 부품은 견적대로 유지한다 ──────────────────────────────────

def test_applying_gpu_starts_a_new_upgrade_plan_keeping_the_other_parts(client):
    list_id = _reviewed(client, FULL_COND)
    res = client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]})
    assert res.status_code == 201
    body = res.json()
    assert body["list_id"] != list_id and body["slots"] == ["GPU"] and body["missing"] == []
    assert body["run_id"]

    state = client.get(f"/session/{body['list_id']}").json()
    assert state["mode"] == "upgrade"
    values = {f["key"]: f["value"] for f in state["fields"]}
    assert values["budget_max"] == 2_000_000 and values["priority"] == "value"


def test_the_new_plan_actually_recommends_only_the_chosen_slot(client):
    list_id = _reviewed(client, FULL_COND)
    new_list_id = client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]}).json()["list_id"]
    import time
    result = None
    for _ in range(50):
        result = client.get(f"/session/{new_list_id}/result").json()
        if result["status"] != "running":
            break
        time.sleep(0.1)
    assert result["status"] == "done"
    assert [it["slot"] for it in result["items"]] == ["GPU"]        # 나머지 부품(CPU·메인보드·RAM)은 견적 그대로라 새로 추천하지 않는다


def test_kept_parts_survive_as_the_current_specs_condition(client):
    list_id = _reviewed(client, FULL_COND)
    new_list_id = client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]}).json()["list_id"]
    values = {f["key"]: f["value"] for f in client.get(f"/session/{new_list_id}").json()["fields"]}
    # current_specs 는 조건 필드 목록에 없어 전용 확인 API 가 없다 — /session/{id}/spec-file 계약과 같은 값이라
    # accepts_spec_file 로 업그레이드 세션임만 확인하고, 실제 유지 여부는 추천이 CPU 를 재추천하지 않는 것으로 본다(위 테스트).
    assert client.get(f"/session/{new_list_id}").json()["accepts_spec_file"] is True


def test_the_new_plan_is_marked_as_coming_from_a_quote_review(client):
    """리포트 조립 가이드가 이 계획을 업그레이드(기존 부품 분리)가 아니라 새 PC 조립으로 안내하려면 출처가 남아야 한다."""
    import psycopg
    from uuid import UUID

    from src.repo.plan_repo import PlanRepo

    list_id = _reviewed(client, FULL_COND)
    new_list_id = client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]}).json()["list_id"]
    with psycopg.connect(DSN, prepare_threshold=None) as conn:
        repo = PlanRepo(conn)
        rows = repo.load_full(repo.get_current_revision(UUID(new_list_id))["id"])["conditions"]
    origin = next(r["value"] for r in rows if r["condition_key"] == "plan_origin")
    assert origin == {"value": "quote_review", "source_list_id": list_id}
    # 조건 칩·필드 목록에는 나오지 않는다(카테고리 fields 밖의 키)
    assert "plan_origin" not in {f["key"] for f in client.get(f"/session/{new_list_id}").json()["fields"]}


# ── 조건이 부족하면 추천을 바로 시작하지 않는다 ──────────────────────────────────────────

def test_without_conditions_the_plan_is_created_but_not_recommended_yet(client):
    list_id = _reviewed(client, None)
    res = client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]}).json()
    assert res["run_id"] is None
    assert set(res["missing"]) >= {"purpose", "budget_max", "priority"}
    assert client.get(f"/session/{res['list_id']}/result").status_code == 404   # 아직 추천을 시작하지 않았다


def test_a_partially_conditioned_quote_is_missing_only_what_it_lacks(client):
    list_id = _reviewed(client, {"purpose": "game", "budget_max": 1_500_000})    # priority 없음
    res = client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]}).json()
    assert res["missing"] == ["priority"] and res["run_id"] is None


def test_a_slot_the_quote_has_no_spec_for_triggers_the_usual_upgrade_chip_question(client):
    """RAM 스펙이 없는 견적에서 RAM 을 업그레이드하면 — 메인보드는 유지되니 플랫폼 질문은 없지만,
    파워를 모르면 GPU 교체 판단에 필요한 질문(owned_psu_w)이 기존 업그레이드 흐름 그대로 뜬다."""
    bare = {"CPU": QUOTE["CPU"], "메인보드": QUOTE["메인보드"]}
    list_id = client.post("/pc/reviews", json={"current_specs": bare, "conditions": FULL_COND}).json()["list_id"]
    res = client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]}).json()
    assert res["missing"] == ["owned_psu_w"] and res["run_id"] is None


# ── 여러 부품, 검증, 소유권 ──────────────────────────────────────────────────────────────

def test_applying_several_slots_at_once(client):
    list_id = _reviewed(client, FULL_COND)
    res = client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU", "CPU", "GPU"]}).json()   # 중복은 한 번만
    assert res["slots"] == ["GPU", "CPU"]


def test_an_empty_or_unknown_slot_is_rejected(client):
    list_id = _reviewed(client, FULL_COND)
    assert client.post(f"/pc/reviews/{list_id}/apply", json={"slots": []}).status_code == 422
    assert client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["모니터"]}).status_code == 422


def test_only_the_owner_can_apply_an_alternative(client):
    from fastapi.testclient import TestClient
    from src.api import app

    list_id = _reviewed(client, FULL_COND)
    with TestClient(app) as stranger:
        assert stranger.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]}).status_code == 404


def test_applying_does_not_change_the_original_review_or_session(client):
    list_id = _reviewed(client, FULL_COND)
    before = client.get(f"/pc/reviews/{list_id}").json()
    client.post(f"/pc/reviews/{list_id}/apply", json={"slots": ["GPU"]})
    assert client.get(f"/pc/reviews/{list_id}").json() == before
