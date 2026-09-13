"""GET /reviews/summary/{product_key} — 실측 관측과 합성 데모 블록이 섞이지 않는다."""
import json

import pytest

pytest.importorskip("httpx")                      # TestClient 의존성 — `uv sync --group test`
from fastapi.testclient import TestClient         # noqa: E402

from src.api import app
from src.config import REVIEW_SUMMARIES_DEMO
from src.repo import review_repo
from src.repo.review_repo import ProductRiskStore, ReviewSummaryDemoFile
from src.services import review_service


@pytest.fixture
def client(tmp_path, monkeypatch):
    """관계 축 산출물을 손으로 만든 최소 JSON 으로 대체 — pandas·실데이터 없이 돈다."""
    risk = {
        "meta": {"min_reviews": 30, "labels": None, "control_scope": "test", "source": "test"},
        "controls": {"burst7": 0.05, "one_off_rate": 0.1, "prolific_rate": 0.1, "p5": 0.6,
                     "shared_reviewers": 50, "deg": 500},
        "products": {"ASIN1": {"n": 120, "mean_rating": 4.5, "p5": 0.7, "burst7": 0.2, "burst7_count": 24,
                               "burst7_start_day": 300, "first_day": 100, "one_off_rate": 0.1,
                               "prolific_rate": 0.05, "shared_reviewers": 80, "deg": 900}},
        "cards": {"burst_top": []},
    }
    p = tmp_path / "risk.json"
    p.write_text(json.dumps(risk), encoding="utf-8")
    store = ProductRiskStore(p)
    store.alias["amd-ryzen-5-5600"] = "ASIN1"          # 데모 부품 하나를 관측에 잇는다
    monkeypatch.setattr(review_repo, "_default_store", store)
    monkeypatch.setattr(review_repo, "_default_store_tried", True)
    monkeypatch.setattr(review_service, "_demo_file", ReviewSummaryDemoFile(REVIEW_SUMMARIES_DEMO))
    return TestClient(app)


def test_observed_part_has_evidence_and_null_cleaned_rating(client):
    j = client.get("/reviews/summary/amd-ryzen-5-5600").json()
    assert j["orig_rating"] == 4.5 and j["total_reviews"] == 120
    assert j["cleaned_rating"] is None and j["cleanse_ratio"] is None      # 판정기 없음
    risk = j["product_manipulation_risk"]
    assert risk["score"] is None and risk["reliable_range"] is True
    assert any("7일 안에 몰림" in e for e in risk["evidence"]) and risk["product_ref"] == "ASIN1"
    assert j["synthetic_demo"]["is_synthetic"] is True                     # 합성 블록은 표지와 함께 분리
    assert "합성" in j["synthetic_demo"]["note"] and j["synthetic_demo"]["cleaned_rating"] is not None
    assert j["axis_scores"] == {} and j["top_summaries"] == []             # 최상위엔 실측만


def test_unobserved_demo_part_returns_demo_block_only(client):
    j = client.get("/reviews/summary/intel-core-i5-14400f").json()
    assert j["total_reviews"] == 0 and j["orig_rating"] is None
    assert j["product_manipulation_risk"]["evidence"] == []
    assert "관측 없음" in j["confidence_note"]
    assert j["synthetic_demo"]["axis_scores"]                             # 항목별 평가는 데모에서


def test_unknown_key_is_404_envelope(client):
    r = client.get("/reviews/summary/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# ── 결과 응답의 product_key 가 슬러그가 아닐 때 ─────────────────────────────
# GET /session/{id}/result 는 catalog.product.model(제품명 원문)을 product_key 로 내보내고
# 화면이 그 값을 이 엔드포인트에 그대로 넘긴다. 슬러그만 받으면 전부 404 가 된다.
def test_candidate_keys_adds_slug_form():
    from src.services.review_service import candidate_keys
    assert candidate_keys("ASUS TUF GAMING B650-PLUS WIFI") == [
        "ASUS TUF GAMING B650-PLUS WIFI", "asus-tuf-gaming-b650-plus-wifi"]


def test_candidate_keys_keeps_slug_first_and_does_not_duplicate():
    """저쪽이 슬러그를 내보내게 고쳐지면 첫 후보가 바로 맞고 변환은 일어나지 않는다."""
    from src.services.review_service import candidate_keys
    assert candidate_keys("asus-tuf-gaming-b650-plus-wifi") == ["asus-tuf-gaming-b650-plus-wifi"]
