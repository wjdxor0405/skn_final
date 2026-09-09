"""
`data/reviews_raw/` → `data/reviews_clean/` + `data/reviews_snapshot.json`.

    .venv/bin/python scripts/clean_reviews.py

**두 갈래로 나가는 것이 요점이다.**

    reviews_clean/   엔진이 실제로 읽는 문장. `.gitignore` 로 막혀 있다
    reviews_snapshot.json   건수·분포·출처만. 이것만 커밋된다

2026-09-08 방침(리뷰 원문을 그대로 저장·노출하지 않는다)과 데모 재현성 사이의
타협점이다 — 원문은 **모은 사람의 기계에만** 남고, 저장소에는 "무엇을 몇 건
모아서 몇 건을 왜 버렸는가"만 남는다. 스냅샷만 보고도 판정의 표본 규모를
검증할 수 있고, 원문은 재수집해야 한다.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reviews.clean import clean_part          # noqa: E402

RAW = ROOT / "data" / "reviews_raw"
CLEAN = ROOT / "data" / "reviews_clean"
SNAPSHOT = ROOT / "data" / "reviews_snapshot.json"


def main() -> None:
    if not RAW.exists():
        raise SystemExit(f"{RAW} 가 없습니다. 먼저 scripts/collect_reviews.py collect 를 도세요.")
    CLEAN.mkdir(parents=True, exist_ok=True)

    entries, totals = [], Counter()
    for path in sorted(RAW.glob("*.json")):
        doc = json.loads(path.read_text())
        cleaned = clean_part(doc)
        (CLEAN / path.name).write_text(
            json.dumps(cleaned, ensure_ascii=False, indent=1), encoding="utf-8")

        kinds = Counter(r["kind"] for r in cleaned["reviews"])
        lengths = sorted(len(r["text"]) for r in cleaned["reviews"])
        dates = sorted(r["date"][:11] for r in cleaned["reviews"] if r.get("date"))
        entries.append({
            "part_code": cleaned["part_code"],
            "name": cleaned["name"],
            "type": cleaned["type"],
            "source": cleaned["source"],
            "product_id": cleaned["product_id"],
            "product_title": cleaned["product_title"],
            "product_url": cleaned["product_url"],
            "collected_at": cleaned["collected_at"],
            "counts": {"raw": cleaned["cleaning"]["in"],
                       "clean": cleaned["cleaning"]["out"], **dict(kinds)},
            "dropped": cleaned["cleaning"]["dropped"],
            "pii_masked": cleaned["cleaning"]["pii_masked"],
            "with_risk_signal": cleaned["cleaning"]["with_risk_signal"],
            "text_length": {"median": lengths[len(lengths) // 2] if lengths else 0,
                            "max": lengths[-1] if lengths else 0},
            "date_range": [dates[0], dates[-1]] if dates else [],
        })
        totals["raw"] += cleaned["cleaning"]["in"]
        totals["clean"] += cleaned["cleaning"]["out"]
        for k, v in cleaned["cleaning"]["dropped"].items():
            totals[f"버림:{k}"] += v

    SNAPSHOT.write_text(json.dumps({
        "as_of": "2026-09-09",
        "policy": ("리뷰 원문은 이 파일에 없습니다. 2026-09-08 방침(원문을 그대로 "
                   "저장·노출하지 않는다)에 따라 건수·분포·출처만 싣습니다. "
                   "엔진이 읽는 문장은 data/reviews_clean/ 에 있고 커밋되지 않습니다."),
        "totals": dict(totals),
        "parts": entries,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"품목 {len(entries)}개 · 원문 {totals['raw']}건 → 클렌징 후 {totals['clean']}건")
    for key, n in sorted(totals.items()):
        if key.startswith("버림:"):
            print(f"   {key} {n}건")
    print(f"  {CLEAN} (커밋 안 됨) · {SNAPSHOT} (커밋됨)")


if __name__ == "__main__":
    main()
