"""
`REVIEW_SOURCE=collected` — 모아서 클렌징한 **실제 리뷰**를 읽는다.

[합성과 무엇이 다른가]
셋이 다르다. 그리고 셋 다 엔진 쪽에 이미 자리가 있다.

    may_quote = False   원문을 인용해 내보내지 않는다. 2026-09-08 방침이다.
                        `Evidence.excerpt_policy` 가 "withheld" 로 나가고
                        화면은 `sources` 의 링크를 가리킨다
    risk = None         조작 확률이 **안 매겨져 있다.** 0.0 으로 채우면 20%
                        필터가 한 건도 못 거르면서 에러도 안 난다.
                        `gather()` 가 이걸 `unscored_risk` 로 세어 내보낸다
    라벨 없음           `bears_on`·`contradicts` 가 비어 있다. 그래서 이 소스에서는
                        `MATCH_MODE=label` 이 아무것도 못 찾는다 — 실데이터에는
                        정답이 없고, 의미 대조를 모델이 해야 한다

**셋째가 이 소스를 붙이는 진짜 이유다.** `decisions/0008` 이 가장 큰 함정으로
적어 둔 것이 "합성이면 닿는가/어긋나는가가 공짜"라는 것이었다. 실 리뷰가 붙으면
그 공짜가 사라지고 3단계가 진짜로 도는지 드러난다.

[원문은 저장소에 없다]
`data/reviews_clean/` 은 `.gitignore` 로 막혀 있다. 그래서 이 소스는 **모은
사람의 기계에서만** 돈다. 없으면 조용히 빈 결과를 내지 않고 무엇을 해야 하는지
말한다 — 조용한 빈 결과는 "근거 없음" 판정으로 둔갑해서, 데이터가 없는 것과
반증이 없는 것이 구분되지 않는다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..engine.schemas import Review

DEFAULT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "reviews_clean"
# 카탈로그 품목 코드 → 리뷰를 모은 실제 제품. **없어도 된다.**
ALIASES = Path(__file__).resolve().parent.parent.parent / "data" / "review_aliases.json"
BUCKETS = ("0-20%", "20-40%", "40-60%", "60-80%", "80-100%")


class CollectedReviews:
    """클렌징된 실 리뷰를 품목별로 읽는다. 파일 하나가 품목 하나다."""

    name = "collected"
    # **기본이 False 여야 한다.** 우리가 지어낸 문장이 아니라서 인용해 내보낼 수 없다.
    may_quote = False

    def __init__(self, directory: Path | None = None, threshold: float = 0.20) -> None:
        self.dir = Path(directory or os.environ.get("REVIEWS_DIR", DEFAULT_DIR))
        self.threshold = threshold
        self._cache: dict[str, list[Review]] = {}
        # [왜 별칭표가 필요한가]
        # 지금 데모 카탈로그는 익명 품목이다(`CPU-A6` = "A사 6코어 12스레드").
        # 리뷰는 실제 제품(`cpu-intel-core-i5-12400f`)에 붙어 있으므로 그대로는
        # 한 건도 안 붙는다 — 그러면 모든 주장이 "근거 없음"으로 끝나는데, 그건
        # 리뷰가 없어서가 아니라 **이름이 안 맞아서**다. 둘을 구분할 수 없게
        # 되는 것이 문제라 다리를 명시한다. 카탈로그가 실제 제품으로 바뀌면
        # 이 파일은 필요 없어진다.
        self._alias: dict[str, str] = {}
        if ALIASES.exists():
            self._alias = {k: v for k, v in json.loads(ALIASES.read_text()).items()
                           if not k.startswith("_")}
        if not self.dir.exists():
            raise RuntimeError(
                f"{self.dir} 가 없습니다. 실 리뷰는 저장소에 커밋되지 않습니다 "
                f"(2026-09-08 방침). 먼저 모으세요:\n"
                f"  .venv/bin/python scripts/collect_reviews.py probe\n"
                f"  .venv/bin/python scripts/collect_reviews.py collect --top 50\n"
                f"  .venv/bin/python scripts/clean_reviews.py\n"
                f"또는 REVIEW_SOURCE=synthetic 으로 도세요.")

    def fetch(self, part_code: str) -> list[Review]:
        """
        그 품목의 리뷰 전부. **조작 확률이 높은 것도 함께 준다** — 거르는 것은
        소스가 아니라 대조하는 쪽 일이고, 몇 건을 왜 뺐는지 화면에 공개해야 한다.
        """
        if part_code in self._cache:
            return self._cache[part_code]
        path = self.dir / f"{self._alias.get(part_code, part_code)}.json"
        if not path.exists():
            self._cache[part_code] = []
            return []
        doc = json.loads(path.read_text())
        reviews = [
            Review(
                review_id=r["review_id"],
                part_code=part_code,
                kind=r.get("kind", "리뷰"),
                text=r["text"],
                source_url=r.get("source_url", ""),
                # 안 잰 것은 안 잰 채로 둔다. `RISK_MODE` 가 매기거나, 못 매기면
                # `unscored_risk` 로 세어져 화면 고지가 바뀐다.
                risk=r.get("risk"),
            )
            for r in doc.get("reviews", [])
        ]
        self._cache[part_code] = reviews
        return reviews

    def risk_distribution(self) -> dict[str, int]:
        """읽은 리뷰의 조작 확률 분포. **못 잰 것은 분포에 넣지 않는다.**"""
        out = {b: 0 for b in BUCKETS}
        for reviews in self._cache.values():
            for r in reviews:
                if r.risk is None:
                    continue
                out[BUCKETS[min(int(r.risk * 5), 4)]] += 1
        return out

    def unscored_count(self) -> int:
        """조작 확률을 못 잰 건수. 0 이 아니면 '20% 이상을 걸렀다'고 말할 수 없다."""
        return sum(1 for rs in self._cache.values() for r in rs if r.risk is None)

    def disclosure(self) -> str:
        return (
            "이 판정에 쓰인 리뷰는 가격비교 사이트에서 모은 실제 리뷰입니다. "
            "원문은 저장·공개하지 않으며 판정 근거는 출처 링크로 대신합니다. "
            "조작 확률이 매겨지지 않은 리뷰가 섞여 있을 수 있습니다."
        )
