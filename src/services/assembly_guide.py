"""리포트 "조립·설치 가이드" — 규칙으로만 만든다 (docs/조립가이드_기획.md).

- 단계는 부품 단위가 아니라 작업 단위다. 새 PC는 보드를 케이스 밖에서 먼저 조립하는 순서(4.1),
  업그레이드는 선행 → 분리 → 장착 → 확인(4.2).
- 단계 안의 줄은 네 종류다. `설치`(작업 방법 문서), `이 조합`(호환 검사 결과 — 결과 화면 "호환성 점검 상세"와 같은
  `compat_checks` 행의 문장을 그대로 옮긴다), `주의`(care 문서 중 조립 시점 phase 만), `확인`(조립 뒤 점검).
- 수치는 호환 검사가 계산한 것만 쓴다. 이 모듈은 수치를 만들지도 해석하지도 않는다(P1). 검사가 모르면 모른다고 싣는다(P3).
- 리포트를 인쇄할 때마다 같은 문장이 나와야 해서 LLM을 부르지 않는다(G7).

문서는 id 로 직접 찾는다. 슬롯·단계가 정해지면 읽을 문서도 정해지므로 검색이 할 일이 없다 — 실제 제조사 매뉴얼이
들어오면(RAG-01) `설치` 줄의 출처만 검색으로 바뀐다. 출력은 화면 파서(`guideStepsFromWire`)가 읽는 문자열 형식이다:
`N. 제목` 줄 + `   라벨: 본문` 줄.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from src.config import CARE_GUIDES_JSON, DATA_DIR

ASSEMBLY_STEPS_JSON = DATA_DIR / "pc_assembly_steps.json"

PC_SLOTS = ("CPU", "GPU", "RAM", "메인보드", "저장장치", "파워", "케이스", "쿨러")

# 호환 검사 axis → 관련 슬롯. 업그레이드에서 그 axis 줄을 어느 부품 장착 단계에 붙일지 정한다.
AXIS_SLOTS: dict[str, tuple[str, ...]] = {
    "socket": ("CPU", "메인보드"), "memory": ("RAM", "메인보드"), "motherboard_case": ("메인보드", "케이스"),
    "gpu_len": ("GPU", "케이스"), "cooler_height": ("쿨러", "케이스"), "cooler_socket": ("CPU", "쿨러"),
    "bios": ("CPU", "메인보드"), "power": ("CPU", "GPU", "파워"), "psu_form": ("파워", "케이스"),
    "gpu_connector": ("GPU", "파워"), "psu_length": ("파워", "케이스"), "gpu_slots": ("GPU", "케이스"),
    "radiator": ("쿨러", "케이스"), "ram_slots": ("RAM", "메인보드"), "ram_speed": ("RAM", "메인보드"),
    "m2": ("저장장치", "메인보드"),
}

INSTALL_DOC = {
    "CPU": "install_cpu", "GPU": "install_gpu", "RAM": "install_ram", "메인보드": "install_mainboard",
    "저장장치": "install_storage", "파워": "install_psu", "케이스": "install_case", "쿨러": "install_cooler",
}

# 새 PC 조립 단계(4.1). slots: 이 단계에서 장착하는 부품, axes: 붙일 호환 검사, cautions: 조립 시점 care 문서.
BUILD_STEPS: tuple[dict, ...] = (
    {"title": "준비", "doc": "step_prep", "slots": (), "axes": (), "cautions": ()},
    {"title": "보드 밖 조립", "doc": "step_board_outside", "slots": ("CPU", "RAM", "저장장치", "쿨러"),
     "axes": ("socket", "memory", "cooler_socket", "ram_slots", "m2"), "cautions": ("storage_thermal",)},
    {"title": "케이스 준비", "doc": None, "slots": ("케이스",), "axes": ("motherboard_case",), "cautions": ()},
    {"title": "파워 장착", "doc": None, "slots": ("파워",), "axes": ("psu_form", "psu_length"), "cautions": ("psu_cabling",)},
    {"title": "메인보드 장착·배선", "doc": "step_board_mount", "slots": (), "axes": ("cooler_height", "radiator"),
     "cautions": ("build_connectors",)},
    {"title": "그래픽카드 장착", "doc": None, "slots": ("GPU",), "axes": ("gpu_len", "gpu_slots", "gpu_connector", "power"),
     "cautions": ()},
    {"title": "첫 부팅 점검", "doc": "step_first_boot", "slots": (), "axes": ("ram_speed", "bios"), "cautions": ()},
)

# 업그레이드: 교체 부품별 선행·분리·확인 문서(4.2). 순서는 분리하기 쉬운 것부터.
UPGRADE_ORDER = ("GPU", "파워", "저장장치", "RAM", "쿨러", "CPU")
UPGRADE_PRE = {"CPU": "upgrade_pre_cpu", "GPU": "upgrade_pre_gpu", "저장장치": "upgrade_pre_storage", "파워": "upgrade_pre_psu"}
UPGRADE_REMOVE = {s: f"upgrade_remove_{k}" for s, k in
                  (("GPU", "gpu"), ("RAM", "ram"), ("저장장치", "storage"), ("파워", "psu"), ("쿨러", "cooler"), ("CPU", "cpu"))}
UPGRADE_AFTER = {s: f"upgrade_after_{k}" for s, k in
                 (("CPU", "cpu"), ("GPU", "gpu"), ("RAM", "ram"), ("저장장치", "storage"), ("파워", "psu"), ("쿨러", "cooler"))}
REBUILD_SLOTS = ("메인보드", "케이스")      # 이 둘을 바꾸면 업그레이드가 아니라 재조립이다

ORIGIN_QUOTE_REVIEW = "quote_review"

# 견적 원문 끝의 가격("250,000원", "25만원") — 부품 이름으로 보일 때는 뗀다. 가격은 견적 점검 화면의 몫이다.
_TRAILING_PRICE = re.compile(r"\s*[\d,.]+\s*(만\s*)?원\s*$")


@lru_cache(maxsize=1)
def _docs() -> dict[str, dict]:
    docs = json.loads(ASSEMBLY_STEPS_JSON.read_text(encoding="utf-8"))
    docs += json.loads(CARE_GUIDES_JSON.read_text(encoding="utf-8"))
    return {d["id"]: d for d in docs}


def _text(doc_id: str) -> str | None:
    doc = _docs().get(doc_id)
    return doc["text"] if doc else None


def _caution(doc_id: str) -> str | None:
    """조립 시점(assembly·after) care 문서만. 구매 전 문구(purchase)는 조립 중에 읽을 말이 아니다(G2)."""
    doc = _docs().get(doc_id)
    return doc["text"] if doc and doc.get("phase") in ("assembly", "after") else None


def compat_line(row: dict) -> str | None:
    """호환 검사 한 행 → `이 조합` 줄 본문. 검사 문장(detail)은 코드가 아는 값만 담고 있어 그대로 옮긴다."""
    state, label, detail = row.get("state"), row.get("label") or row.get("axis"), (row.get("detail") or "").strip()
    if state == "ok":
        return f"{label} — {detail}"
    if state == "unknown":
        tail = " 제품 설명서에서 직접 확인하세요." if "확인하지 못했" in detail else ""
        return f"{label} — {detail}{tail}"
    if state == "fail":
        return f"{label} — 확정 전 호환 검사에서 맞지 않는 것으로 나왔습니다: {detail}"
    return None     # skipped(이번에 바뀌지 않는 부품끼리) · 예산 같은 비호환 항목은 싣지 않는다


def memory_profile_name(cpu_name: str | None) -> str:
    name = (cpu_name or "").lower()
    if "ryzen" in name or "amd" in name or "라이젠" in name:
        return "EXPO(메모리가 XMP만 지원하면 XMP·DOCP)"
    if "intel" in name or "인텔" in name or "core" in name or "코어" in name:
        return "XMP"
    return "XMP(인텔) 또는 EXPO(AMD)"


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.n = 0

    def step(self, title: str) -> None:
        self.lines.append(f"{self.n}. {title}")
        self.n += 1

    def line(self, label: str, text: str | None) -> None:
        if text:
            self.lines.append(f"   {label}: {text}")

    def text(self) -> str:
        return "\n".join(self.lines)


def _part_label(slot: str, parts: dict[str, dict]) -> str:
    part = parts.get(slot)
    if part is None:
        return f"{slot}(이 목록에 없는 부품)"
    suffix = {"owned": " (기존 부품)", "quote": " (받은 견적 부품 — 따로 구매)"}.get(part.get("source"), "")
    return f"{slot} {part['name']}{suffix}"


def _rows_by_axis(compat_rows: list[dict]) -> dict[str, dict]:
    return {r["axis"]: r for r in compat_rows if r.get("axis") in AXIS_SLOTS}


QUOTE_NOTE = ("받은 견적 부품끼리의 호환은 여기서 다시 적지 않았습니다 — 견적 점검 결과의 호환 검사를 참고하세요. "
              "아래 '이 조합' 줄은 새로 산 부품이 관련된 검사만입니다.")


def _build(parts: dict[str, dict], compat_rows: list[dict], lead: list[str] | None = None,
           lead_lines: list[tuple[str, str]] | None = None) -> str:
    rows = _rows_by_axis(compat_rows)
    w = _Writer()
    for spec in BUILD_STEPS:
        slots = [s for s in spec["slots"]]
        title = spec["title"] + (f" — {'·'.join(slots)}" if len(slots) > 1 else "")
        w.step(title)
        if spec["doc"] == "step_prep":
            for doc_id in lead or ():
                w.line("설치", _text(doc_id))
        w.line("설치", _text(spec["doc"]) if spec["doc"] else None)
        if spec["doc"] == "step_prep":
            for label, text in lead_lines or ():
                w.line(label, text)
        for slot in slots:
            w.line("설치", f"{_part_label(slot, parts)} — {_text(INSTALL_DOC[slot])}")
        for axis in spec["axes"]:
            if axis in rows:
                w.line("이 조합", compat_line(rows[axis]))
        for doc_id in spec["cautions"]:
            w.line("주의", _caution(doc_id))
        if spec["doc"] == "step_first_boot":
            profile = memory_profile_name((parts.get("CPU") or {}).get("name"))
            w.line("확인", (_text("step_memory_profile") or "").format(profile=profile))
            w.line("확인", _text("step_after_os"))
    return w.text()


def _upgrade(parts: dict[str, dict], compat_rows: list[dict]) -> str:
    replaced = [s for s in UPGRADE_ORDER if parts.get(s, {}).get("source") == "bought"]
    rows = _rows_by_axis(compat_rows)
    w = _Writer()
    w.step("준비")
    w.line("설치", _text("upgrade_prep"))

    pre = [s for s in replaced if s in UPGRADE_PRE]
    if pre:
        w.step("교체 전에 — " + "·".join(pre))
        for slot in pre:
            w.line("설치", f"{slot}: {_text(UPGRADE_PRE[slot])}")
            if slot == "CPU" and "bios" in rows:
                w.line("이 조합", compat_line(rows["bios"]))

    # CPU를 바꾸면 쿨러를 바꾸지 않아도 떼었다 다시 달아야 한다.
    remove = list(replaced)
    if "CPU" in remove and "쿨러" not in remove:
        remove.insert(remove.index("CPU"), "쿨러")
    w.step("기존 부품 분리 — " + "·".join(remove))
    for slot in remove:
        w.line("설치", f"{slot}: {_text(UPGRADE_REMOVE[slot])}")

    w.step("새 부품 장착 — " + "·".join(replaced))
    placed: set[str] = {"bios"} if pre and "CPU" in pre else set()
    for slot in replaced:
        w.line("설치", f"{_part_label(slot, parts)} — {_text(INSTALL_DOC[slot])}")
        # 업그레이드에서는 기존 부품 스펙을 모르는 경우가 많다 — 스펙이 없어 못 본 항목은 한 줄로 묶는다.
        unchecked: list[str] = []
        for axis, slots in AXIS_SLOTS.items():
            if slot in slots and axis in rows and axis not in placed:
                row = rows[axis]
                placed.add(axis)
                if row.get("state") == "unknown" and "확인하지 못했" in (row.get("detail") or ""):
                    unchecked.append(row.get("label") or axis)
                    continue
                w.line("이 조합", compat_line(row))
        if unchecked:
            w.line("이 조합", f"스펙 정보가 없어 확인하지 못한 항목 — {', '.join(unchecked)}. "
                            "기존 부품과 새 부품의 설명서에서 직접 확인하세요.")
    if "CPU" in replaced and "쿨러" not in replaced:
        w.line("설치", f"쿨러(기존) — {_text('install_cooler')}")

    w.step("교체 뒤 확인")
    for slot in replaced:
        w.line("확인", f"{slot}: {_text(UPGRADE_AFTER[slot])}")
    if "RAM" in replaced:
        profile = memory_profile_name((parts.get("CPU") or {}).get("name"))
        w.line("확인", (_text("step_memory_profile") or "").format(profile=profile))
    return w.text()


def build(mode: str, items: list[dict], compat_rows: list[dict] | None = None,
          current_specs: dict | None = None, origin: str | None = None) -> dict:
    """{"status": "ready", "text": str} — 본체 품목이 없으면 {"status": "pending", "text": None}.

    items: 확정 스냅샷 품목(`slot`, `product.name`). 주변기기 등 본체 슬롯이 아닌 품목은 무시한다.
    compat_rows: `recommendation_service.compat_checks` 결과(없으면 `이 조합` 줄 없이 만든다).
    current_specs: 업그레이드 계획의 유지 부품 {슬롯: 자유 텍스트}.
    origin: "quote_review" 면 타사 견적에서 대안을 적용한 계획 — 사용자가 나머지 부품을 아직 갖고 있지 않으므로
            업그레이드가 아니라 새 PC 순서로 안내한다(G8).
    """
    parts: dict[str, dict] = {}
    for it in items:
        slot, name = it.get("slot"), ((it.get("product") or {}).get("name") or "").strip()
        if slot in PC_SLOTS and name and slot not in parts:
            parts[slot] = {"name": name, "source": "bought"}
    if not parts:
        return {"status": "pending", "text": None}
    compat_rows = compat_rows or []

    kept_source = "quote" if origin == ORIGIN_QUOTE_REVIEW else "owned"
    for slot, text in (current_specs or {}).items():
        name = _TRAILING_PRICE.sub("", str(text or "")).strip()
        if slot in PC_SLOTS and slot not in parts and name:
            parts[slot] = {"name": name, "source": kept_source}

    if origin == ORIGIN_QUOTE_REVIEW:
        return {"status": "ready", "text": _build(parts, compat_rows, lead_lines=[("확인", QUOTE_NOTE)])}
    if mode != "upgrade":
        return {"status": "ready", "text": _build(parts, compat_rows)}
    if any(parts.get(s, {}).get("source") == "bought" for s in REBUILD_SLOTS):
        return {"status": "ready", "text": _build(parts, compat_rows, lead=["upgrade_prep", "upgrade_rebuild"])}
    return {"status": "ready", "text": _upgrade(parts, compat_rows)}
