"""조립 가이드 사람 말 문장 — 호환 검사 문장(detail)의 값만 옮기고, 형식이 다르면 원문으로 물러선다. DB 없이 돈다.

detail 예시는 2026-09-28 시연 DB에서 실제 compat_checks 가 낸 문장이다.
"""
from __future__ import annotations

import pytest

from src.services.assembly_guide_wording import cable_list, plain


def _ok(axis: str, detail: str) -> str | None:
    return plain({"axis": axis, "label": axis, "state": "ok", "detail": detail})


@pytest.mark.parametrize(("axis", "detail", "expected"), [
    ("gpu_len", "GPU 길이 204mm ≤ 케이스 허용 330mm", "그래픽카드 길이 204mm, 케이스 허용 330mm — 126mm 여유가 있어 들어갑니다."),
    ("cooler_height", "쿨러 높이 148mm ≤ 케이스 허용 160mm", "쿨러 높이 148mm, 케이스 허용 160mm — 12mm 여유가 있어 옆판이 닫힙니다."),
    ("psu_length", "파워 길이 140mm ≤ 케이스 허용 210mm", "파워 길이 140mm, 케이스 허용 210mm — 70mm 여유가 있습니다."),
    ("gpu_slots", "GPU 두께 2.0슬롯 → 2칸 ≤ 케이스 확장 슬롯 4칸",
     "그래픽카드가 확장 슬롯 2칸을 차지합니다 — 케이스 뒷면의 확장 슬롯 덮개 2개를 떼세요(케이스에 4칸 있음)."),
    ("gpu_connector", "GPU 1× 8-pin — 필요 PCIe 커넥터 1개 ≤ 파워 제공 4개",
     "그래픽카드 전원은 1× 8-pin — 파워의 PCIe 전원 케이블 1개를 연결합니다(파워에 4개 있음)."),
    ("power", "CPU 65W + GPU 165W = 230W ≤ 파워 750W × 0.9 = 675W",
     "CPU 65W와 그래픽카드 165W를 합쳐 230W — 파워 750W의 90%(675W)보다 낮아 이 서비스의 용량 기준을 통과합니다."),
    ("socket", "CPU AMD Ryzen 5 7600(AM5) = 메인보드 GIGABYTE A620M H(AM5)", "CPU와 메인보드 소켓이 모두 AM5로 맞습니다."),
    ("memory", "RAM DDR5 = 메인보드 DDR5", "메모리와 메인보드가 모두 DDR5입니다."),
    ("cooler_socket", "CPU 소켓 AM5 ∈ 쿨러 지원 소켓 LGA1700/1200/115x, AM5/AM4",
     "쿨러가 AM5 소켓을 지원합니다 — 쿨러 설명서에서 AM5용 장착 방법(다른 소켓과 같은 부품일 수 있음)을 확인해 쓰세요."),
    ("motherboard_case", "메인보드 mATX → 케이스가 지원 mATX",
     "메인보드(mATX)가 케이스에 맞습니다 — 케이스의 mATX 위치 나사 기둥(스탠드오프)을 쓰세요."),
    ("psu_form", "파워 ATX → 케이스가 지원 ATX", "파워 규격(ATX)이 케이스 파워 자리에 맞습니다."),
    ("ram_slots", "RAM 모듈 1개 ≤ 메인보드 DIMM 슬롯 2개 · RAM 총 16GB ≤ 메인보드 최대 128GB",
     "메모리 1개, 메인보드 슬롯 2개 — 메인보드 설명서가 권장하는 슬롯에 꽂으세요(제조사마다 A 또는 B 슬롯)."),
    ("ram_speed", "RAM 5600MT/s ≤ 메인보드 최대 6400MT/s",
     "메모리 5600MT/s는 메인보드 지원 범위(최대 6400MT/s) 안입니다 — BIOS에서 메모리 프로필을 켜면 대개 이 속도로 동작하지만, CPU나 메모리 개수에 따라 낮아질 수 있습니다."),
    ("m2", "M.2 슬롯 1개", "M.2 SSD를 꽂을 슬롯이 메인보드에 1개 있습니다."),
])
def test_ok_sentences_carry_the_same_numbers(axis, detail, expected):
    assert _ok(axis, detail) == expected


def test_gpu_connector_without_psu_counts_says_the_count_was_not_checked():
    text = _ok("gpu_connector", "GPU 1× 8-pin ← 파워 PCIe 8-pin (파워의 커넥터 개수 데이터가 없어 개수는 확인하지 않음)")
    assert text.startswith("그래픽카드 전원은 1× 8-pin — 파워가 PCIe 8-pin 커넥터를 제공합니다.") and "확인하지 않았으니" in text


def test_two_sticks_on_four_slots_say_which_slots():
    assert "A2·B2" in _ok("ram_slots", "RAM 모듈 2개 ≤ 메인보드 DIMM 슬롯 4개")


def test_bios_ok_keeps_the_unchecked_shipping_version_caveat():
    text = _ok("bios", "CPU AMD Ryzen 5 7600 ∈ 메인보드 지원 계열 Ryzen 7000 / 8000G / 9000 계열 (출고 BIOS 버전은 데이터에 없어 확인하지 않음)")
    assert text.startswith("메인보드가 이 CPU 계열을 지원합니다.") and "출고 BIOS 버전은 알 수 없습니다" in text


def test_bios_unknown_points_to_the_support_list():
    row = {"axis": "bios", "state": "unknown", "detail": "CPU: AMD Ryzen 7 5800X · 메인보드 지원 CPU 계열: 정보 없음 — CPU 이름의 "
           "세대·계열을 읽지 못했거나 보드의 지원 목록이 없어 확인하지 못했습니다."}
    assert plain(row).startswith("메인보드가 이 CPU를 지원하는지 데이터로 확인하지 못했습니다.")


def test_unknown_names_only_the_missing_specs():
    row = {"axis": "gpu_slots", "label": "GPU 두께", "state": "unknown",
           "detail": "GPU 두께: 2.0 · 케이스 확장 슬롯: 정보 없음 — 스펙 정보가 부족해 확인하지 못했습니다."}
    assert plain(row) == "케이스 확장 슬롯 정보가 없어 확인하지 못했습니다. 설명서에서 직접 확인하세요."


@pytest.mark.parametrize("row", [
    {"axis": "gpu_len", "state": "ok", "detail": "형식이 바뀐 문장"},
    {"axis": "gpu_len", "state": "fail", "detail": "GPU 길이 350mm > 케이스 허용 330mm"},
    {"axis": "ram_speed", "state": "unknown", "detail": "RAM 6000MT/s 가 메인보드 최대 5600MT/s 를 넘어 낮은 속도로 동작할 수 있습니다."},
])
def test_unrecognized_or_non_ok_falls_back_to_the_original(row):
    assert plain(row) is None


def test_cable_list_reads_sata_and_adapter_cases():
    rows = {"gpu_connector": {"state": "unknown", "detail": "GPU 16핀 — 16핀이 없어 PCIe 커넥터 3개로 동봉 어댑터를 연결해야 합니다"},
            "m2": {"state": "skipped", "detail": "SSD(2.5인치 SATA)는 M.2 슬롯을 쓰지 않아 검사하지 않았습니다."}}
    text = cable_list(rows)
    assert "동봉된 어댑터" in text and "SATA 데이터 케이블" in text
