"""조립 가이드 `이 조합` 줄을 사람 말로 — 호환 검사 문장(detail)의 숫자를 그대로 옮겨 문장 틀에 넣는다.

detail 은 `stage4_optimize.pc_compat_details` 가 만든 문장이다(`GPU 길이 204mm ≤ 케이스 허용 330mm`). 여기서는 그
문장에서 숫자·규격을 읽어 "126mm 여유가 있습니다"처럼 바꾼다. 새로 계산하는 값은 두 수의 차(여유)뿐이고, 판정은
바꾸지 않는다(P1). 문장 형식이 바뀌어 틀에 안 맞으면 None 을 돌려주고 호출부는 원래 문장을 쓴다 — 틀린 말보다
어려운 말이 낫다.
"""
from __future__ import annotations

import re
from collections.abc import Callable

_Handler = Callable[[re.Match], str]
_OK: dict[str, list[tuple[re.Pattern, _Handler]]] = {}


def _ok(axis: str, pattern: str):
    def register(fn: _Handler) -> _Handler:
        _OK.setdefault(axis, []).append((re.compile(pattern), fn))
        return fn
    return register


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _fmt(n: float) -> str:
    return f"{n:g}"


@_ok("gpu_len", r"^GPU 길이 ([\d.]+)mm ≤ 케이스 허용 ([\d.]+)mm$")
def _(m):
    gpu, case = _num(m[1]), _num(m[2])
    return f"그래픽카드 길이 {m[1]}mm, 케이스 허용 {m[2]}mm — {_fmt(case - gpu)}mm 여유가 있어 들어갑니다."


@_ok("cooler_height", r"^쿨러 높이 ([\d.]+)mm ≤ 케이스 허용 ([\d.]+)mm$")
def _(m):
    cooler, case = _num(m[1]), _num(m[2])
    return f"쿨러 높이 {m[1]}mm, 케이스 허용 {m[2]}mm — {_fmt(case - cooler)}mm 여유가 있어 옆판이 닫힙니다."


@_ok("psu_length", r"^파워 길이 ([\d.]+)mm ≤ 케이스 허용 ([\d.]+)mm$")
def _(m):
    psu, case = _num(m[1]), _num(m[2])
    return f"파워 길이 {m[1]}mm, 케이스 허용 {m[2]}mm — {_fmt(case - psu)}mm 여유가 있습니다."


@_ok("gpu_slots", r"^GPU 두께 ([\d.]+)슬롯 → (\d+)칸 ≤ 케이스 확장 슬롯 (\d+)칸$")
def _(m):
    return (f"그래픽카드가 확장 슬롯 {m[2]}칸을 차지합니다 — 케이스 뒷면의 확장 슬롯 덮개 {m[2]}개를 떼세요"
            f"(케이스에 {m[3]}칸 있음).")


@_ok("gpu_connector", r"^GPU (.+?) — 필요 PCIe 커넥터 (\d+)개 ≤ 파워 제공 (\d+)개$")
def _(m):
    return f"그래픽카드 전원은 {m[1]} — 파워의 PCIe 전원 케이블 {m[2]}개를 연결합니다(파워에 {m[3]}개 있음)."


@_ok("gpu_connector", r"^GPU (.+?) — 필요 16핀 (\d+)개 ≤ 파워 제공 (\d+)개$")
def _(m):
    return f"그래픽카드 전원은 {m[1]} — 파워의 16핀(12V-2x6) 케이블 {m[2]}개를 연결합니다(파워에 {m[3]}개 있음)."


@_ok("gpu_connector", r"^GPU (.+?) — 보조 전원 불필요$|^보조 전원 불필요")
def _(m):
    return "그래픽카드에 보조 전원 케이블이 필요 없습니다 — 슬롯 전원만으로 동작합니다."


@_ok("gpu_connector", r"^GPU (.+?) ← 파워 (.+?) \(파워의 커넥터 개수 데이터가 없어 개수는 확인하지 않음\)$")
def _(m):
    return (f"그래픽카드 전원은 {m[1]} — 파워가 {m[2]} 커넥터를 제공합니다. 파워의 케이블 개수는 데이터가 없어 "
            "확인하지 않았으니, 카드 옆면 커넥터 수만큼 케이블이 있는지 확인하세요.")


@_ok("power", r"^CPU( 최대)? ([\d.]+)W \+ GPU ([\d.]+)W = ([\d.]+)W ≤ 파워 ([\d.]+)W × ([\d.]+) = ([\d.]+)W$")
def _(m):
    # 계수(0.9)는 추천엔진 규칙이다. 업계 기준처럼 읽히지 않게 서비스 기준이라고 밝힌다.
    pct = round(float(m[6]) * 100)
    return f"CPU{m[1] or ''} {m[2]}W와 그래픽카드 {m[3]}W를 합쳐 {m[4]}W — 파워 {m[5]}W의 {pct}%({m[7]}W)보다 낮아 이 서비스의 용량 기준을 통과합니다."


@_ok("socket", r"^CPU .+\((.+?)\) = 메인보드 .+\((.+?)\)$")
def _(m):
    return f"CPU와 메인보드 소켓이 모두 {m[1]}로 맞습니다."


@_ok("memory", r"^RAM (\S+) = 메인보드 (\S+)$")
def _(m):
    return f"메모리와 메인보드가 모두 {m[1]}입니다."


@_ok("cooler_socket", r"^CPU 소켓 (\S+) ∈ 쿨러 지원 소켓 (.+)$")
def _(m):
    return f"쿨러가 {m[1]} 소켓을 지원합니다 — 쿨러 설명서에서 {m[1]}용 장착 방법(다른 소켓과 같은 부품일 수 있음)을 확인해 쓰세요."


@_ok("motherboard_case", r"^메인보드 (\S+) → 케이스가 지원 (.+)$")
def _(m):
    return f"메인보드({m[1]})가 케이스에 맞습니다 — 케이스의 {m[1]} 위치 나사 기둥(스탠드오프)을 쓰세요."


@_ok("psu_form", r"^파워 (\S+) → 케이스가 지원 (.+)$")
def _(m):
    return f"파워 규격({m[1]})이 케이스 파워 자리에 맞습니다."


@_ok("ram_slots", r"^RAM 모듈 (\d+)개 ≤ 메인보드 DIMM 슬롯 (\d+)개")
def _(m):
    modules, slots = int(m[1]), int(m[2])
    if modules == 1:
        where = "메인보드 설명서가 권장하는 슬롯에 꽂으세요(제조사마다 A 또는 B 슬롯)"
    elif modules == 2 and slots >= 4:
        where = "A2·B2 슬롯(대개 CPU 쪽에서 2번째·4번째)에 나눠 꽂아야 듀얼 채널로 동작합니다"
    elif modules == slots:
        where = "슬롯을 모두 채웁니다"
    else:
        where = "메인보드 설명서가 권장하는 순서대로 꽂으세요"
    return f"메모리 {modules}개, 메인보드 슬롯 {slots}개 — {where}."


@_ok("ram_speed", r"^RAM (\d+)MT/s ≤ 메인보드 최대 (\d+)MT/s$")
def _(m):
    return (f"메모리 {m[1]}MT/s는 메인보드 지원 범위(최대 {m[2]}MT/s) 안입니다 — BIOS에서 메모리 프로필을 켜면 대개 이 속도로 "
            "동작하지만, CPU나 메모리 개수에 따라 낮아질 수 있습니다.")


@_ok("m2", r"^M\.2 슬롯 (\d+)개( · SSD PCIe ([\d.]+) ≤ 슬롯 최대 ([\d.]+))?$")
def _(m):
    tail = f" SSD(PCIe {m[3]})의 속도를 그대로 낼 수 있습니다." if m[2] else ""
    return f"M.2 SSD를 꽂을 슬롯이 메인보드에 {m[1]}개 있습니다.{tail}"


@_ok("bios", r"^CPU (.+) ∈ 메인보드 지원 계열 (.+?)( \(출고 BIOS 버전은 데이터에 없어 확인하지 않음\))?$")
def _(m):
    tail = (" 다만 메인보드 출고 BIOS 버전은 알 수 없습니다 — 첫 부팅에서 화면이 나오지 않으면 BIOS 업데이트가 필요할 수 "
            "있습니다(메인보드 제조사 사이트의 CPU 지원 목록 확인)." if m[3] else "")
    return f"메인보드가 이 CPU 계열을 지원합니다.{tail}"


@_ok("radiator", r"^라디에이터 (\d+)mm → 케이스 (.+) 장착 가능$")
def _(m):
    return f"케이스 사양상 수랭 라디에이터({m[1]}mm)를 {m[2]}에 달 수 있습니다 — 두께·메모리 높이 제한은 케이스 설명서를 확인하세요."


_MISSING_TAIL = " — 스펙 정보가 부족해 확인하지 못했습니다."


def plain(row: dict) -> str | None:
    """호환 검사 한 행 → 사람 말 문장. 틀에 안 맞거나 ok·unknown 이 아니면 None."""
    detail, state = (row.get("detail") or "").strip(), row.get("state")
    if state == "ok":
        for pattern, fn in _OK.get(row.get("axis") or "", ()):
            m = pattern.search(detail)
            if m:
                return fn(m)
        return None
    if state == "unknown" and row.get("axis") == "bios" and "지원 목록이 없어 확인하지 못했습니다" in detail:
        return ("메인보드가 이 CPU를 지원하는지 데이터로 확인하지 못했습니다. 메인보드 제조사 사이트의 CPU 지원 목록에서 "
                "확인하고, 필요한 BIOS 버전을 적어 두세요.")
    if state == "unknown" and detail.endswith(_MISSING_TAIL):
        # missing() 형식: "A: 값 · B: 정보 없음 — 스펙 정보가 부족해 확인하지 못했습니다."
        pairs = [p.split(": ", 1) for p in detail[: -len(_MISSING_TAIL)].split(" · ")]
        if all(len(p) == 2 for p in pairs):
            absent = [name for name, value in pairs if value == "정보 없음"]
            if absent:
                return f"{'·'.join(absent)} 정보가 없어 확인하지 못했습니다. 설명서에서 직접 확인하세요."
    return None


# ── 이 조합에 필요한 케이블 ─────────────────────────────────────────────────────

def gpu_cable(row: dict | None) -> str:
    """그래픽카드 전원 케이블 한 구절. 호환 검사 gpu_connector 행에서 읽는다."""
    detail, state = ((row or {}).get("detail") or ""), (row or {}).get("state")
    if state == "ok":
        m = re.search(r"필요 PCIe 커넥터 (\d+)개 ≤ 파워 제공 (\d+)개", detail)
        if m:
            return f"그래픽카드 PCIe 전원 {m[1]}개(파워에 {m[2]}개 있음)"
        m = re.search(r"필요 16핀 (\d+)개 ≤ 파워 제공 (\d+)개", detail)
        if m:
            return f"그래픽카드 16핀(12V-2x6) {m[1]}개(파워에 {m[2]}개 있음)"
        if "보조 전원 불필요" in detail:
            return "그래픽카드 보조 전원은 필요 없음"
        m = re.search(r"GPU (.+?) ← 파워", detail)
        if m:
            return f"그래픽카드 전원 {m[1]}(개수는 카드 옆면 커넥터 수만큼)"
    if "어댑터" in detail:
        return "그래픽카드 전원 — 그래픽카드에 동봉된 어댑터로 파워의 PCIe 케이블을 연결"
    return "그래픽카드 전원 — 카드 옆면 커넥터 수만큼 파워의 PCIe 케이블을 준비"


def storage_cable(row: dict | None) -> str | None:
    detail, state = ((row or {}).get("detail") or ""), (row or {}).get("state")
    if state == "skipped" and "M.2 슬롯을 쓰지 않아" in detail:
        return "SATA 저장장치 — SATA 데이터 케이블(메인보드 상자) 1개와 파워의 SATA 전원 케이블 1개"
    if state in ("ok", "unknown") and detail.startswith(("M.2 슬롯", "SSD는 PCIe")):
        return "M.2 SSD는 케이블이 필요 없음"
    return None


def cable_list(rows: dict[str, dict]) -> str:
    """새 PC 조립에서 파워에서 뽑아 둘 케이블 목록. 모르는 개수는 추측하지 않고 확인할 곳을 말한다."""
    parts = ["메인보드 24핀 1개",
             "CPU 보조전원(보드 왼쪽 위 커넥터 수만큼: 8핀, 8+4핀, 8+8핀 — 자리가 있으면 모두 꽂기를 권장)",
             gpu_cable(rows.get("gpu_connector"))]
    storage = storage_cable(rows.get("m2"))
    if storage:
        parts.append(storage)
    return " · ".join(parts)
