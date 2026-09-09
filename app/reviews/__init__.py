"""
리뷰 소스 고르기 — `REVIEW_SOURCE`.

이 파일이 없던 동안 `REVIEW_SOURCE` 는 **문서에만 있었다.** `synthetic.py` 의
docstring 과 `docs/추천엔진_API계약.md` 가 그런 환경변수가 있다고 적어 놨는데
읽는 코드가 없어서, 소스를 바꾸려면 `packs/pc.py` 의 `review_source()` 를 손으로
고쳐야 했다. 문서가 거짓이었다.

새 소스를 붙이는 자리는 여기 하나다. `ReviewSource`(`base.py`)를 만족하는 클래스를
`app/reviews/` 에 두고 아래 `_SOURCES` 에 이름을 등록하면 된다.
"""

from __future__ import annotations

import os

REVIEW_SOURCE = os.environ.get("REVIEW_SOURCE", "synthetic").lower()


def build_source(pack):
    """
    이 요청에 쓸 리뷰 소스.

    `synthetic` 은 팩이 만들어 준다 — 합성 라벨이 그 도메인의 주장에 붙어 있어서
    팩 밖에서는 만들 수 없다. 실데이터 소스는 팩과 무관하므로 여기서 만든다.
    """
    if REVIEW_SOURCE == "synthetic":
        return pack.review_source()
    if REVIEW_SOURCE == "collected":
        # 실 리뷰. **팩과 무관하다** — 합성과 달리 정답 라벨이 없어서 도메인
        # 지식이 필요 없고, 그래서 여기서 만든다. 데이터가 없으면 생성자가
        # 무엇을 해야 하는지 말하며 실패한다.
        #
        # [조합 하나를 여기서 막는다]
        # 실 리뷰 + `MATCH_MODE=label` 은 **에러 없이 전부 "근거 없음"** 이 된다.
        # 라벨을 읽는 대조기가 라벨 없는 데이터에서 아무것도 못 찾기 때문이다.
        # 화면에는 "리뷰 300건을 봤지만 근거가 없다"로 나오는데 사실은 대조를
        # 아예 안 한 것이라, `0008` 이 가장 큰 함정으로 적어 둔 착각이 그대로
        # 재현된다. 그래서 조용히 두지 않고 무엇을 해야 하는지 말한다.
        if os.environ.get("MATCH_MODE", "label").lower() == "label" \
                and os.environ.get("ALLOW_UNLABELED_MATCH", "") != "1":
            raise RuntimeError(
                "REVIEW_SOURCE=collected 는 MATCH_MODE=label 과 함께 쓸 수 없습니다. "
                "실 리뷰에는 정답 라벨이 없어 모든 주장이 '근거 없음'으로 끝납니다 "
                "(에러가 나지 않아 더 위험합니다). MATCH_MODE=llm 으로 도세요. "
                "라벨 없이 도는 것을 알고도 원하면 ALLOW_UNLABELED_MATCH=1.")
        from .collected import CollectedReviews
        return CollectedReviews(threshold=getattr(pack, "review_risk_threshold", 0.20))
    raise RuntimeError(
        f"모르는 리뷰 소스입니다: {REVIEW_SOURCE!r}. "
        f"쓸 수 있는 것: 'synthetic' · 'collected'. 새로 붙이려면 app/reviews/ 에 "
        f"ReviewSource(base.py) 구현을 두고 app/reviews/__init__.py 에 등록하세요."
    )
