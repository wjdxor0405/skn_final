"""리포트 조립 가이드(규칙) — docs/조립가이드_기획.md 4.1·4.2. DB 없이 돈다."""
from __future__ import annotations

import json

import pytest

from src.config import CARE_GUIDES_JSON
from src.services import assembly_guide as ag

SLOTS = ["CPU", "GPU", "RAM", "메인보드", "저장장치", "파워", "케이스", "쿨러"]


def _items(*slots: str) -> list[dict]:
    return [{"slot": s, "product": {"name": f"테스트 {s}"}} for s in slots]


def _titles(text: str) -> list[str]:
    return [line.split(". ", 1)[1] for line in text.splitlines() if line[:1].isdigit()]


def _step(text: str, title_prefix: str) -> list[str]:
    """제목이 title_prefix 로 시작하는 단계의 본문 줄."""
    out, inside = [], False
    for line in text.splitlines():
        if line[:1].isdigit():
            inside = line.split(". ", 1)[1].startswith(title_prefix)
        elif inside:
            out.append(line.strip())
    return out


ROWS = [
    {"axis": "gpu_len", "label": "GPU 길이", "state": "ok", "detail": "GPU 길이 305mm ≤ 케이스 허용 400mm"},
    {"axis": "gpu_slots", "label": "GPU 두께", "state": "unknown",
     "detail": "GPU 두께: 정보 없음 · 케이스 확장 슬롯: 7 — 스펙 정보가 부족해 확인하지 못했습니다."},
    {"axis": "socket", "label": "소켓", "state": "fail", "detail": "CPU A(AM5) ≠ 메인보드 B(LGA1700)"},
    {"axis": "psu_form", "label": "파워 규격", "state": "skipped", "detail": "이번 견적에서 바뀌는 부품이 아니라 확인하지 않았습니다."},
    {"axis": "budget", "label": "예산", "state": "ok", "detail": "합계 ≤ 예산"},
    {"axis": "bios", "label": "BIOS", "state": "ok", "detail": "CPU X ∈ 메인보드 지원 계열 Y (출고 BIOS 버전은 데이터에 없어 확인하지 않음)"},
]


# ── 새 PC ────────────────────────────────────────────────────────────────

def test_new_build_is_task_ordered_board_outside_first():
    text = ag.build("build", _items(*reversed(SLOTS)))["text"]
    assert _titles(text) == ["준비", "보드 밖 조립 — CPU·RAM·저장장치·쿨러", "케이스 준비", "파워 장착",
                             "메인보드 장착·배선", "그래픽카드 장착", "첫 부팅 점검"]
    assert text.startswith("0. 준비")
    # G1: CPU 는 메인보드를 케이스에 넣기(4단계) 전에 장착한다 — 순서가 서로 모순되지 않는다
    assert any(line.startswith("설치: CPU 테스트 CPU") for line in _step(text, "보드 밖 조립"))
    assert not any(line.startswith("설치: CPU") for line in _step(text, "메인보드 장착"))


def test_compat_rows_become_this_combo_lines_with_the_computed_numbers():
    text = ag.build("build", _items(*SLOTS), ROWS)["text"]
    gpu = _step(text, "그래픽카드 장착")
    assert "이 조합: GPU 길이 — 그래픽카드 길이 305mm, 케이스 허용 400mm — 95mm 여유가 있어 들어갑니다." in gpu
    assert "이 조합: GPU 두께 — GPU 두께 정보가 없어 확인하지 못했습니다. 설명서에서 직접 확인하세요." in gpu
    assert any("확정 전 호환 검사에서 맞지 않는 것으로 나왔습니다" in line and "AM5" in line
               for line in _step(text, "보드 밖 조립"))
    assert "예산" not in text and "바뀌는 부품이 아니라" not in text     # 비호환 항목·skipped 는 싣지 않는다


def test_attention_items_are_gathered_first_with_the_step_they_belong_to():
    text = ag.build("build", _items(*SLOTS), ROWS)["text"]
    assert _titles(text)[0] == "조립 전에 알아둘 것"
    first = _step(text, "조립 전에 알아둘 것")
    assert len(first) == 2          # fail(소켓) + unknown(GPU 두께). ok·skipped·예산은 없다
    assert any(line.startswith("주의: 소켓 — 확정 전") and line.endswith("(보드 밖 조립 단계)") for line in first)
    assert any(line.startswith("주의: GPU 두께") and line.endswith("(그래픽카드 장착 단계)") for line in first)


def test_all_ok_means_no_attention_step():
    ok_rows = [r for r in ROWS if r["state"] == "ok"]
    assert _titles(ag.build("build", _items(*SLOTS), ok_rows)["text"])[0] == "준비"


def test_cable_list_is_in_the_psu_step():
    rows = [{"axis": "gpu_connector", "label": "GPU 전원", "state": "ok",
             "detail": "GPU 1× 8-pin — 필요 PCIe 커넥터 1개 ≤ 파워 제공 4개"},
            {"axis": "m2", "label": "M.2", "state": "ok", "detail": "M.2 슬롯 1개"}]
    psu = _step(ag.build("build", _items(*SLOTS), rows)["text"], "파워 장착")
    cable = next(line for line in psu if "뽑아 둘 케이블" in line)
    assert "메인보드 24핀 1개" in cable and "그래픽카드 PCIe 전원 1개(파워에 4개 있음)" in cable
    assert "M.2 SSD는 케이블이 필요 없음" in cable


def test_no_compat_rows_still_builds_the_guide_without_combo_lines():
    text = ag.build("build", _items(*SLOTS), None)["text"]
    combo = [line for line in text.splitlines() if "이 조합:" in line]
    assert len(combo) == 1 and "뽑아 둘 케이블" in combo[0]      # 검사가 없으면 케이블 목록만(개수는 추측하지 않는다)
    assert "카드 옆면 커넥터 수만큼" in combo[0] and "설치:" in text


def test_only_assembly_time_care_documents_are_quoted():
    """G2: 구매 전 문구(phase=purchase)는 조립 가이드에 싣지 않는다."""
    docs = json.loads(CARE_GUIDES_JSON.read_text(encoding="utf-8"))
    care = [d for d in docs if d.get("kind", "care") == "care"]
    assert all(d.get("phase") in ("purchase", "assembly", "after") for d in care)
    text = ag.build("build", _items(*SLOTS))["text"]
    for d in care:
        if d["phase"] == "purchase":
            assert d["text"] not in text, d["id"]
    for step in ag.BUILD_STEPS:
        for doc_id in step["cautions"]:
            assert next(d for d in care if d["id"] == doc_id)["phase"] != "purchase", doc_id


@pytest.mark.parametrize(("cpu", "profile"), [("AMD 라이젠5-6세대 7600", "EXPO"), ("인텔 코어i5-14세대 14400F", "XMP")])
def test_first_boot_names_the_memory_profile_by_cpu_vendor(cpu, profile):
    items = _items(*SLOTS)
    items[0]["product"]["name"] = cpu
    boot = _step(ag.build("build", items)["text"], "첫 부팅")
    assert any(line.startswith("확인:") and f"BIOS에서 {profile}" in line for line in boot)


def test_same_input_same_text():
    """G7: 인쇄할 때마다 같은 문장."""
    assert ag.build("build", _items(*SLOTS), ROWS) == ag.build("build", _items(*SLOTS), ROWS)


# ── 범위 ────────────────────────────────────────────────────────────────

def test_peripherals_are_ignored_and_no_pc_parts_means_no_guide():
    assert ag.build("build", _items("모니터", "키보드")) == {"status": "pending", "text": None}
    assert ag.build("build", []) == {"status": "pending", "text": None}
    text = ag.build("build", _items("GPU", "모니터"))["text"]
    assert "테스트 모니터" not in text


# ── 업그레이드 ─────────────────────────────────────────────────────────────

def test_upgrade_is_prepare_remove_install_check():
    text = ag.build("upgrade", _items("GPU"), ROWS, current_specs={"CPU": "라이젠 5 5600"})["text"]
    assert _titles(text) == ["조립 전에 알아둘 것", "준비", "교체 전에 — GPU", "기존 부품 분리 — GPU", "새 부품 장착 — GPU",
                             "교체 뒤 확인"]
    install = _step(text, "새 부품 장착")
    assert "이 조합: GPU 길이 — 그래픽카드 길이 305mm, 케이스 허용 400mm — 95mm 여유가 있어 들어갑니다." in install
    assert any(line.startswith("이 조합: 연결할 케이블 — ") for line in install)
    assert "라이젠 5 5600" not in text     # 유지 부품은 단계로 만들지 않는다
    # 스펙이 없어 못 본 항목은 줄마다 흩지 않고 맨 앞 요약에 한 줄로 묶는다
    assert ("주의: 스펙 정보가 없어 확인하지 못한 항목 — GPU 두께. 기존 부품과 새 부품의 설명서에서 직접 확인하세요."
            in _step(text, "조립 전에 알아둘 것"))
    assert not any(line.startswith("이 조합: GPU 두께") for line in install)


def test_cpu_upgrade_updates_bios_first_and_reseats_the_cooler():
    text = ag.build("upgrade", _items("CPU"), ROWS)["text"]
    pre = _step(text, "교체 전에")
    assert any("BIOS를 새 CPU를 지원하는 버전으로 먼저 업데이트" in line for line in pre)
    assert any(line.startswith("이 조합: BIOS") and "출고 BIOS 버전" in line for line in pre)
    assert "기존 부품 분리 — 쿨러·CPU" in _titles(text)     # CPU를 바꾸면 쿨러를 떼었다 다시 단다
    assert any(line.startswith("설치: 쿨러(기존)") for line in _step(text, "새 부품 장착"))
    assert not any("BIOS" in line for line in _step(text, "새 부품 장착") if line.startswith("이 조합"))


def test_ram_upgrade_reminds_to_turn_the_memory_profile_back_on():
    after = _step(ag.build("upgrade", _items("RAM"))["text"], "교체 뒤")
    assert any("다시 켜야" in line for line in after)


@pytest.mark.parametrize("slot", ["메인보드", "케이스"])
def test_replacing_board_or_case_is_a_rebuild(slot):
    text = ag.build("upgrade", _items(slot, "GPU"), current_specs={"CPU": "라이젠 5 5600"})["text"]
    assert "보드 밖 조립 — CPU·RAM·저장장치·쿨러" in _titles(text)
    prep = _step(text, "준비")
    assert any("다시 조립하는 것입니다" in line for line in prep)
    assert any(line.startswith("설치: CPU 라이젠 5 5600 (기존 부품)") for line in _step(text, "보드 밖 조립"))


# ── 타사 견적에서 대안 적용 (G8) ─────────────────────────────────────────────

def test_quote_review_origin_is_guided_as_a_new_build_not_an_upgrade():
    quote = {"CPU": "라이젠 5 7600 250,000원", "메인보드": "MSI PRO B650M-P", "RAM": "DDR5 16GB 6만원"}
    text = ag.build("upgrade", _items("GPU"), ROWS, current_specs=quote, origin="quote_review")["text"]
    assert "분리" not in "".join(_titles(text)) and "교체 전에" not in text
    assert "BIOS를 새 CPU를 지원하는 버전으로 먼저" not in text
    outside = _step(text, "보드 밖 조립")
    assert any(line.startswith("설치: CPU 라이젠 5 7600 (받은 견적 부품 — 따로 구매)") for line in outside)
    assert any(line.startswith("설치: RAM DDR5 16GB (받은") for line in outside)      # 견적 원문의 가격은 뗀다
    assert any(line.startswith("설치: GPU 테스트 GPU —") for line in _step(text, "그래픽카드 장착"))
    assert any(line.startswith("확인: 받은 견적 부품끼리의 호환") for line in _step(text, "준비"))
    assert any("BIOS에서 EXPO" in line for line in _step(text, "첫 부팅"))     # "라이젠" 도 AMD
