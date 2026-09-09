"""
`parts_list.xlsx` → `data/parts.json`. **리뷰 수집의 대상 목록을 만드는 자리.**

[왜 파서가 따로 필요한가]
이 xlsx 는 사람이 손으로 채운 수집 마스터라 스프레드시트로서는 정상이지만
프로그램이 읽기에는 세 군데가 깨져 있다.

1. **열 이름이 없다.** `fields_needed` 한 칸에 파이프로 붙인 스펙이 들어 있고,
   그 자리 뜻이 부품군마다 다르다(cpu 4칸, gpu 6칸, case 2칸). 카테고리별
   위치 스키마를 코드에 적어 두지 않으면 값이 조용히 어긋난다
2. **브랜드 표기가 70종**이다 — `Corsair` · `CORSAIR` · `커세어 (Corsair)` 가
   따로 세어진다. 리뷰를 브랜드로 찾을 때 이게 곧 검색 실패다
3. **한 행에 두 제품인 것이 있다**(`4000D / 5000D AIRFLOW`). 리뷰는 제품마다
   다르므로 여기서 쪼개지 않으면 서로 다른 제품의 리뷰가 한 통에 섞인다 —
   `app/reviews/clean.py` 가 가장 크게 경계하는 오염이 이것이다

[O/X 필드는 "기준 충족 + 실제 값"이다]
`X (16GB GDDR7)` 처럼 아니라고 적고 괄호에 실제 값을 붙이는 형식이 다섯 부품군에
공통이다. 그래서 `flag`/`detail` 로 나눠 담고, GPU 는 괄호에서 VRAM 을 뽑는다 —
깔때기(2단계 하드 제약)가 쓰는 유일한 수치라 여기서 건지면 값이 크다.

**openpyxl 을 쓰지 않는다.** 이 저장소 의존성에 없고, 이 파일 하나 읽자고
넣을 이유가 없다. xlsx 는 zip + XML 이라 표준 라이브러리로 읽힌다.

    .venv/bin/python scripts/parse_parts_list.py
    .venv/bin/python scripts/parse_parts_list.py --xlsx data/parts_list.xlsx --out data/parts.json
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# 부품군별 `fields_needed` 위치 스키마. **파일에 열 이름이 없어서 여기가 유일한
# 정의다.** 값의 개수가 이 표와 다르면 그 행은 `warnings` 를 달고 통과시킨다 —
# 조용히 버리면 322행 중 몇이 사라졌는지 아무도 모른다.
FIELD_SCHEMA: dict[str, tuple[str, ...]] = {
    "cpu":       ("socket", "tdp", "memory", "tier"),
    "mainboard": ("socket", "chipset", "form_factor", "memory", "note"),
    "ram":       ("memory_type", "xmp_expo", "capacity", "kit"),
    "gpu":       ("tdp", "length", "slots", "power_connector", "vram", "tier"),
    "ssd":       ("interface", "small_form_factor", "capacity"),
    # psu 의 3번째 칸은 322행 전부 비어 있다. 무엇을 넣으려던 자리인지 파일에
    # 적혀 있지 않아 이름을 지어내지 않고 빈 칸으로 둔다.
    "psu":       ("watt", "efficiency", "unlabeled", "form_factor", "modular"),
    "case":      ("board_support", "max_cooler_height"),
    "cooler":    ("cooler_type", "socket", "height", "radiator"),
}

# 표기가 갈린 브랜드를 하나로 모은다. 키는 소문자·공백 제거한 원본.
BRAND_CANON = {
    "corsair": "Corsair", "커세어(corsair)": "Corsair",
    "lianli": "Lian Li", "리안리(lianli)": "Lian Li",
    "antec": "Antec", "안텍(antec)": "Antec",
    "zalman": "Zalman", "잘만(zalman)": "Zalman",
    "abko": "ABKO", "앱코(abko)": "ABKO",
    "montech": "Montech", "몬텍(montech)": "Montech",
    "micronics": "Micronics", "마이크로닉스": "Micronics",
    "coolermaster": "Cooler Master", "쿨러마스터": "Cooler Master",
    "phanteks": "Phanteks", "판텍스(phanteks)": "Phanteks",
    "daven": "DAVEN", "데이븐(daven)": "DAVEN",
    "hyte": "HYTE", "하이트(hyte)": "HYTE",
    "darkflash": "darkflash", "다크플래쉬": "darkflash",
    "fractaldesign": "Fractal Design", "프랙탈디자인": "Fractal Design",
    "samsung": "Samsung", "삼성전자": "Samsung",
    "skhynix": "SK hynix", "sk하이닉스": "SK hynix",
    "westerndigital": "Western Digital", "wd": "Western Digital",
    "intel": "intel", "amd": "amd", "nvidia": "nvidia",
}


def _cells(sheet_xml: bytes, shared: list[str]):
    """행마다 {열문자: 값} 하나씩. 빈 셀은 아예 안 담는다."""
    root = ET.fromstring(sheet_xml)
    for row in root.iter(NS + "row"):
        out: dict[str, str] = {}
        for c in row.iter(NS + "c"):
            v = c.find(NS + "v")
            if v is None or v.text is None:
                continue
            col = "".join(ch for ch in (c.get("r") or "") if ch.isalpha())
            out[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
        if out:
            yield int(row.get("r") or 0), out


def read_rows(xlsx: Path) -> list[tuple[int, dict[str, str]]]:
    z = zipfile.ZipFile(xlsx)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
    return list(_cells(z.read("xl/worksheets/sheet1.xml"), shared))


def slug(s: str) -> str:
    """part_code 에 쓸 조각. 한글은 그대로 두면 URL·파일명에서 번거로워 음역 없이 뺀다."""
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def canon_brand(raw: str) -> str:
    key = re.sub(r"\s+", "", (raw or "").lower())
    return BRAND_CANON.get(key, (raw or "").strip())


def split_names(name: str) -> list[str]:
    """
    한 칸에 여러 제품이 들어간 행을 쪼갠다.

    `4000D / 5000D AIRFLOW` 는 두 제품이고, `GeForce RTX 4060 Ti (16GB)` 는
    한 제품이다. 그래서 **슬래시 양쪽이 모두 모델명처럼 보일 때만** 쪼갠다.

    쪼갠 뒤가 더 어렵다 — 공통 낱말이 어느 쪽에 붙어 있는지가 행마다 다르다.

        4000D / 5000D AIRFLOW   → 뒤에 공통 접미(AIRFLOW). 앞에도 붙여야 한다
        LANCOOL 216 / 217       → 앞에 공통 접두(LANCOOL). 뒤에도 붙여야 한다
        Meshlicious / Meshroom S→ 공통이 아니다. 건드리면 없는 제품이 생긴다
        North / North XL        → 이미 둘 다 완성된 이름이다

    토큰 수로 가른다. 앞이 짧으면 접미를 나눠 쓴 것이고, 앞이 길면 접두를 나눠
    쓴 것이다. 접미는 **대문자 3글자 이상**일 때만 붙인다 — `Meshroom S` 의
    `S` 를 접미로 보면 `Meshlicious S` 라는 없는 제품이 생긴다.

    쪼개기가 틀리면 검색이 실패해 그 제품이 빠질 뿐이지만, **안 쪼개면 서로 다른
    제품의 리뷰가 한 통에 섞인다.** 뒤가 훨씬 위험해서 쪼개는 쪽으로 기울였다.
    """
    if " / " not in name:
        return [name]
    parts = [p.strip() for p in name.split(" / ") if p.strip()]
    if len(parts) < 2:
        return [name]
    if not all(re.match(r"^[A-Z0-9]", p) and len(p.split()) <= 4 for p in parts):
        return [name]

    head = parts[0].split()
    out: list[str] = []
    for p in parts:
        toks = p.split()
        if p is not parts[0] and toks[0] == head[0]:
            out.append(p)                       # 이미 완성된 이름 (North XL)
        elif len(head) < len(toks):             # 공통 접미를 뒤가 들고 있다
            out.append(p)
        elif len(head) > len(toks):             # 공통 접두를 앞이 들고 있다
            out.append(f"{head[0]} {p}")
        else:
            out.append(p)

    # 앞쪽(첫 항목)에도 뒤의 접미를 붙여야 하는 경우를 따로 처리한다.
    tail_toks = parts[-1].split()
    if len(parts[0].split()) == 1 and len(tail_toks) > 1:
        keep = [t for t in tail_toks[1:] if t.isupper() and len(t) >= 3]
        if keep and not parts[-1].startswith(parts[0]):
            out[0] = " ".join([parts[0]] + keep)

    seen: list[str] = []
    for n in out:
        if n not in seen:
            seen.append(n)
    return seen


def parse_fields(part_type: str, raw: str) -> tuple[dict, list[str]]:
    """`fields_needed` 한 칸을 위치 스키마로 편다."""
    warnings: list[str] = []
    schema = FIELD_SCHEMA.get(part_type)
    values = [v.strip() for v in (raw or "").split("|")]
    if schema is None:
        return {"raw": raw}, [f"모르는 부품군: {part_type}"]
    if len(values) != len(schema):
        warnings.append(f"필드 수가 스키마와 다릅니다: {len(values)} != {len(schema)}")
    specs: dict[str, object] = {}
    for key, val in zip(schema, values):
        if not val:
            continue
        m = re.match(r"^([OX])\s*(?:\((.*)\))?$", val)
        if m:                                    # O/X + 괄호 안 실제 값
            specs[key] = {"flag": m.group(1) == "O", "detail": (m.group(2) or "").strip()}
        else:
            specs[key] = val
    return specs, warnings


def derive(part_type: str, specs: dict) -> dict:
    """
    수치로 쓸 수 있는 것만 따로 뽑는다. **원본은 건드리지 않는다** — 해석은
    읽는 시점에 한다는 `decisions/0002` 와 같은 배치다.
    """
    out: dict[str, object] = {}
    if part_type == "gpu":
        vram = specs.get("vram")
        detail = vram.get("detail") if isinstance(vram, dict) else ""
        m = re.search(r"(\d+)\s*GB", detail or "")
        if m:
            out["vram_gb"] = int(m.group(1))
        m = re.match(r"(\d+)\s*W", str(specs.get("tdp") or ""))
        if m:
            out["tdp_w"] = int(m.group(1))
        m = re.match(r"(\d+)\s*mm", str(specs.get("length") or ""))
        if m:
            out["length_mm"] = int(m.group(1))
    elif part_type == "cpu":
        m = re.match(r"(\d+)\s*W", str(specs.get("tdp") or ""))
        if m:
            out["tdp_w"] = int(m.group(1))
    elif part_type == "psu":
        m = re.match(r"(\d+)\s*W", str(specs.get("watt") or ""))
        if m:
            out["watt"] = int(m.group(1))
    elif part_type == "case":
        m = re.match(r"(\d+)\s*mm", str(specs.get("max_cooler_height") or ""))
        if m:
            out["max_cooler_height_mm"] = int(m.group(1))
    elif part_type == "cooler":
        m = re.match(r"(\d+)", str(specs.get("height") or ""))
        if m:
            out["height_mm"] = int(m.group(1))
        m = re.match(r"(\d+)", str(specs.get("radiator") or ""))
        if m:
            out["radiator_mm"] = int(m.group(1))
    return out


def build(xlsx: Path) -> dict:
    rows = read_rows(xlsx)
    parts: list[dict] = []
    seen: dict[str, int] = {}
    skipped = 0
    for rownum, cell in rows:
        ptype = (cell.get("A") or "").strip()
        if not ptype or ptype.startswith("#") or ptype == "type":
            skipped += 1
            continue
        name_raw = (cell.get("B") or "").strip()
        brand_raw = (cell.get("C") or "").strip()
        specs, warnings = parse_fields(ptype, cell.get("D") or "")
        names = split_names(name_raw)
        if len(names) > 1:
            warnings = warnings + [f"한 행에 {len(names)}개 제품이라 쪼갰습니다: {name_raw}"]
        for name in names:
            brand = canon_brand(brand_raw)
            code = f"{ptype}-{slug(brand)}-{slug(name)}".strip("-")
            if code in seen:
                # 같은 제품이 두 번 나온다(쪼갠 이름이 다른 행과 겹친 경우가 있다).
                # 두 번째를 새 품목으로 만들면 같은 제품의 리뷰가 둘로 쪼개져
                # 양쪽 다 표본 임계값에 못 미치게 된다. 그래서 만들지 않고
                # 앞엣것에 표시만 남긴다.
                parts[seen[code]]["warnings"].append(
                    f"{rownum}행에 같은 제품이 다시 있습니다: {name_raw}")
                continue
            seen[code] = len(parts)
            parts.append({
                "part_code": code,
                "type": ptype,
                "name": name,
                "name_raw": name_raw,
                "brand": brand,
                "brand_raw": brand_raw,
                "specs": specs,
                "derived": derive(ptype, specs),
                "fields_raw": (cell.get("D") or "").strip(),
                # URL 은 앞뒤 공백이 붙은 것이 많다. 그대로 두면 요청이 그 자리에서 깨진다.
                "spec_url": (cell.get("E") or "").strip(),
                "cpu_support_url": (cell.get("F") or "").strip(),
                "row": rownum,
                "warnings": warnings,
            })
    return {
        "as_of": "2026-09-09",
        "source": f"{xlsx.name} (팀이 손으로 채운 수집 마스터)",
        "note": "리뷰 수집 대상 목록. 스펙은 원본 표기 그대로 두고 수치는 derived 에 따로 뽑는다.",
        "parts": parts,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="parts_list.xlsx 를 data/parts.json 으로")
    ap.add_argument("--xlsx", default="data/parts_list.xlsx")
    ap.add_argument("--out", default="data/parts.json")
    args = ap.parse_args()

    doc = build(Path(args.xlsx))
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    parts = doc["parts"]
    from collections import Counter
    print(f"{len(parts)}개 → {args.out}")
    print("  부품군:", dict(Counter(p["type"] for p in parts)))
    print("  브랜드 표기:", len({p["brand"] for p in parts}), "종 (원본", len({p["brand_raw"] for p in parts}), "종)")
    print("  spec_url 있음:", sum(1 for p in parts if p["spec_url"]))
    warned = [p for p in parts if p["warnings"]]
    print("  경고 달린 행:", len(warned))
    for p in warned[:5]:
        print("   -", p["part_code"], p["warnings"])


if __name__ == "__main__":
    main()
