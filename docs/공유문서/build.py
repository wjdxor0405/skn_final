#!/usr/bin/env python3
"""
공유용 HTML 페이지 두 개를 스냅샷에서 다시 만든다.

    python docs/공유문서/build.py [--out-dir DIR]

[왜 템플릿과 스크립트로 두는가]
두 페이지 모두 스냅샷에서 뽑은 데이터를 품고 있다. 뷰어는 568KB 중 546KB가
그 파생 데이터다. 완성본을 그대로 커밋하면 원본(data/nexar_snapshot.json)과
파생본이 저장소에 함께 있게 되고, 스냅샷을 다시 뜨면 조용히 어긋난다.
`docs/decisions/0002` 가 기각한 구조다 — 진실이 두 곳이면 어느 쪽이 맞는지 물어야 한다.

그래서 저장소에는 템플릿(사람이 쓴 부분)만 두고, 데이터는 여기서 매번 만든다.
"""

import argparse
import collections
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CATALOG_SOURCE", "snapshot")

from app import snapshot_catalog as sc  # noqa: E402


def _raw() -> dict:
    return json.loads((ROOT / "data" / "nexar_snapshot.json").read_text(encoding="utf-8"))


def viewer_data() -> dict:
    """뷰어용 — 품목마다 판매자·수량구간을 전부 담는다(인가 여부 토글을 페이지에서 한다)."""
    raw = _raw()
    items = []
    for match in raw["response"]["supMultiMatch"]:
        for part in match.get("parts") or []:
            best: dict[str, dict] = {}
            for s in part.get("sellers") or []:
                for o in s.get("offers") or []:
                    breaks = sorted(
                        [{"q": p["quantity"], "p": round(p["convertedPrice"])}
                         for p in (o.get("prices") or []) if p.get("convertedPrice")],
                        key=lambda b: b["q"])
                    if not breaks:
                        continue
                    row = {"n": s["company"]["name"], "a": bool(s.get("isAuthorized")),
                           "c": s.get("country"), "stock": o.get("inventoryLevel") or 0,
                           "moq": o.get("moq") or 0, "lead": o.get("factoryLeadDays") or 0,
                           "sku": o.get("sku"), "pkg": o.get("packaging"),
                           "url": o.get("clickUrl"), "breaks": breaks}
                    # 같은 판매자의 중복 오퍼는 최저가만 — 로더와 같은 규칙
                    k = row["n"]
                    if k not in best or breaks[0]["p"] < best[k]["breaks"][0]["p"]:
                        best[k] = row
            med = part.get("medianPrice1000") or {}
            items.append({
                "mpn": part["mpn"], "ref": match.get("reference"),
                "desc": part.get("shortDescription") or "",
                "mfr": (part.get("manufacturer") or {}).get("name"),
                "cat": (part.get("category") or {}).get("name"),
                "url": part.get("octopartUrl"),
                "ds": (part.get("bestDatasheet") or {}).get("url"),
                "avail": part.get("totalAvail"),
                "median": round(med["convertedPrice"]) if med.get("convertedPrice") else None,
                "sellers": sorted(best.values(), key=lambda x: x["breaks"][0]["p"]),
            })
    items.sort(key=lambda i: i["mpn"])
    sellers = {s["n"] for i in items for s in i["sellers"]}
    return {
        "meta": {
            "fetched_at": raw["provenance"]["fetched_at"],
            "endpoint": raw["provenance"]["endpoint"],
            "n_items": len(items),
            "n_offers": sum(len(i["sellers"]) for i in items),
            "n_auth": len({s["n"] for i in items for s in i["sellers"] if s["a"]}),
            "n_all": len(sellers),
        },
        "items": items,
    }


def report_data() -> dict:
    """인수인계 문서용 — 본문이 인용하는 수치만."""
    raw = _raw()
    M = raw["response"]["supMultiMatch"]
    parts = [p for m in M for p in m["parts"]]
    sellers = [s for p in parts for s in p["sellers"]]
    offers = [o for s in sellers for o in s["offers"]]
    prices = [pr for o in offers for pr in (o.get("prices") or [])]

    median = {p["mpn"]: (p.get("medianPrice1000") or {}).get("convertedPrice") for p in parts}
    ratio = []
    for it in sc.list_items():
        code = it["code"]
        rows = sc.sellers_for(code, 1000)
        if rows and median.get(code):
            low = min(s.offer_price for s in rows)
            ratio.append({"mpn": code, "best": low, "med": round(median[code]),
                          "r": round(low / median[code], 3)})
    ratio.sort(key=lambda x: x["r"])

    def filled(key):
        return round(100 * sum(1 for o in offers
                               if o.get(key) not in (None, "", [], {})) / len(offers), 1)

    return {
        "counts": {
            "items": len(parts), "sellers_rows": len(sellers), "offers": len(offers),
            "prices": len(prices),
            "sellers_all": len({s["company"]["name"] for s in sellers}),
            "sellers_auth": len({s["company"]["name"] for s in sellers if s["isAuthorized"]}),
        },
        "coverage": [(k, filled(k)) for k in (
            "inventoryLevel", "sku", "updated", "prices", "moq",
            "orderMultiple", "packaging", "factoryLeadDays",
            "onOrderQuantity", "factoryPackQuantity")],
        "ratio": ratio,
        "ratio_median": round(statistics.median(x["r"] for x in ratio), 2),
        "fetched_at": raw["provenance"]["fetched_at"],
    }


PAGES = {"catalog-page": viewer_data, "nexar-report": report_data}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(HERE / "build"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, build in PAGES.items():
        template = (HERE / f"{name}.template.html").read_text(encoding="utf-8")
        if template.count("__DATA__") != 1:
            sys.exit(f"{name}.template.html 에 __DATA__ 자리표시자가 정확히 하나 있어야 합니다.")
        blob = json.dumps(build(), ensure_ascii=False, separators=(",", ":"))
        # </script> 가 데이터에 들어가면 스크립트 블록이 조기 종료된다
        if "</script" in blob.lower():
            sys.exit(f"{name}: 데이터에 </script 가 들어 있습니다.")
        out = out_dir / f"{name}.html"
        out.write_text(template.replace("__DATA__", blob), encoding="utf-8")
        print(f"{out}  {out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
