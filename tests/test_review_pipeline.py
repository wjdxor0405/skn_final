#!/usr/bin/env python3
"""
리뷰 수집·클렌징 회귀 검증 — 조용히 거짓이 되는 자리를 고정한다.

    python tests/test_review_pipeline.py

**네트워크를 쓰지 않는다.** 수집기에서 검사하는 것은 요청을 내는 부분이 아니라
*어느 상품에 붙일지 정하는 부분*이다. 실사(2026-09-09)에서 드러난 실패가 전부
거기서 났다 — `Core i5-13400F` 검색 1위가 그 CPU 를 넣은 **완제품 PC** 였다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.reviews.clean import (  # noqa: E402
    clean_part, is_empty_of_content, mask_pii, normalize, option_mismatch,
)
from collect_reviews import model_key, pick  # noqa: E402


def _cand(title: str, cate: str) -> dict:
    return {"title": title, "cate": cate, "product_id": "1"}


def _accepted(name: str, brand: str, ptype: str, title: str, cate: str) -> bool:
    part = {"name": name, "brand": brand, "type": ptype}
    return "rejected" not in pick(part, [_cand(title, cate)])[0]


def check_prebuilt_pc_is_rejected() -> None:
    """
    **가장 중요한 검사다.** 완제품 PC 제목에는 CPU 모델명이 그대로 들어 있어서
    이름만 보면 100% 일치한다. 이게 통과하면 완제품 리뷰로 CPU 주장을 판정한다.
    """
    assert not _accepted("Core i5-13400F", "intel", "cpu",
                         "영웅컴퓨터 영웅 RL3461F i5-13400F RTX5060Ti", "112756")
    assert not _accepted("Core i5-13400F", "intel", "cpu",
                         "다나와표준PC 게임용 230615", "11316681")
    assert _accepted("Core i5-13400F", "intel", "cpu",
                     "인텔 코어 i5-13세대 13400F (랩터레이크)", "113973")
    print("  ✓ 완제품 PC 는 분류 코드에서 걸리고 진짜 CPU 는 통과한다")


def check_suffix_makes_a_different_product() -> None:
    """`RTX 5070` 과 `RTX 5070 Ti` 는 다른 제품이다. 꼬리표 하나가 가른다."""
    assert not _accepted("GeForce RTX 5070", "nvidia", "gpu",
                         "MSI 지포스 RTX 5070 Ti 게이밍 트리오 OC D7 16GB", "112753")
    assert _accepted("GeForce RTX 5070 Ti", "nvidia", "gpu",
                     "MSI 지포스 RTX 5070 Ti 게이밍 트리오 OC D7 16GB", "112753")
    assert not _accepted("990 PRO", "Samsung", "ssd", "삼성 전자 990 EVO M.2 NVMe", "112760")
    assert not _accepted("B650M Pro RS", "ASRock", "mainboard",
                         "ASRock B650 Pro RS 대원씨티에스", "112751")
    print("  ✓ 꼬리표(Ti·PRO·M)가 다르면 다른 제품으로 본다")


def check_word_named_products() -> None:
    """번호가 아니라 낱말로 서는 제품은 낱말을 다 봐야 한다."""
    assert _accepted("Peerless Assassin 120", "Thermalright", "cooler",
                     "Thermalright Peerless Assassin 120 SE 서린", "11236855")
    assert not _accepted("Peerless Assassin 120", "Thermalright", "cooler",
                         "Thermalright Assassin X 120 Refined SE ARGB 서린", "11236855")
    print("  ✓ 낱말로 서는 제품은 낱말이 하나만 빠져도 뺀다")


def check_model_key() -> None:
    assert model_key("GeForce RTX 5070 Ti") == ("5070", "ti")
    assert model_key("Core i5-13400F") == ("13400", "f")
    assert model_key("Ryzen 7 9800X3D") == ("9800", "x3d")
    assert model_key("B650M Pro RS") == ("b650", "m")
    print("  ✓ 이름에서 모델 번호와 꼬리표를 갈라낸다")


def check_pii_never_survives() -> None:
    """본문에 적힌 개인정보는 남지 않는다. 지운 흔적은 남는다."""
    text, found = mask_pii("문의는 010-1234-5678 이나 a.b@mail.com 으로요 https://x.co/1")
    assert "1234" not in text and "@mail.com" not in text and "http" not in text, text
    assert set(found) >= {"[전화]", "[메일]", "[링크]"}
    print("  ✓ 전화·메일·링크가 본문에서 사라진다")


def check_boilerplate_is_dropped() -> None:
    assert is_empty_of_content("좋아요")
    assert is_empty_of_content("ㅋㅋㅋ")
    assert is_empty_of_content("배송 빠름")
    assert not is_empty_of_content("풀로드에서 78도까지 올라가서 서멀 다시 발랐습니다")
    print("  ✓ 내용 없는 상투구는 표본에서 빠진다")


def check_other_option_reviews_are_dropped() -> None:
    """
    같은 상품 페이지에 붙은 **다른 용량**의 리뷰를 뺀다. 이것이 섞이면
    2TB 의 쓰기 속도 주장을 1TB 리뷰로 반증하게 된다.
    """
    assert option_mismatch("1TB 짜리 샀는데 속도가 안 나옵니다", "삼성전자 990 PRO 2TB")
    assert not option_mismatch("2TB 인데 잘 나옵니다", "삼성전자 990 PRO 2TB")
    assert not option_mismatch("속도가 안 나옵니다", "삼성전자 990 PRO 2TB")
    print("  ✓ 다른 옵션(용량)의 리뷰는 표본에서 뺀다")


def check_cleaning_counts_what_it_dropped() -> None:
    """버린 건수를 세지 않으면 남은 건수도 믿을 수 없다."""
    doc = {
        "part_code": "ssd-x", "source": "danawa", "product_title": "삼성 990 PRO 2TB",
        "product_url": "https://example/1",
        "reviews": [
            {"review_id": "1", "kind": "의견", "text": "발열이 심해 히트싱크를 달았습니다", "date": "2026.01.01.", "author_hash": "a"},
            {"review_id": "2", "kind": "의견", "text": "발열이 심해 히트싱크를 달았습니다", "date": "2026.01.01.", "author_hash": "a"},
            {"review_id": "3", "kind": "질문", "text": "좋아요", "date": "2026.01.01.", "author_hash": "b"},
            {"review_id": "4", "kind": "의견", "text": "1TB 모델은 속도가 안 나옵니다", "date": "2026.01.01.", "author_hash": "c"},
        ],
    }
    out = clean_part(doc)
    assert out["cleaning"]["in"] == 4 and out["cleaning"]["out"] == 1, out["cleaning"]
    assert out["cleaning"]["dropped"] == {"완전 중복": 1, "내용 없음": 1, "다른 옵션의 리뷰": 1}
    print("  ✓ 클렌징이 몇 건을 왜 버렸는지 남긴다")


def check_kind_maps_to_engine_buckets() -> None:
    """다나와의 '질문' 은 엔진·목업의 QA 칸이다. 섞으면 표본을 못 쪼갠다."""
    doc = {"part_code": "p", "source": "danawa", "product_title": "x", "product_url": "",
           "reviews": [
               {"review_id": "1", "kind": "질문", "text": "소음이 60도에서 커지나요 정말", "author_hash": "a"},
               {"review_id": "2", "kind": "후기", "text": "소음이 생각보다 조용합니다 만족", "author_hash": "b"}]}
    kinds = {r["kind"] for r in clean_part(doc)["reviews"]}
    assert kinds == {"QA", "리뷰"}, kinds
    print("  ✓ 의견/질문/후기가 엔진의 리뷰·QA 칸으로 간다")


def check_collected_source_never_quotes_and_never_scores() -> None:
    """
    실소스는 **원문을 인용하지 않고**(2026-09-08 방침), **조작 확률을 0 으로
    채우지 않는다**. 둘 다 조용히 어겨지면 화면의 약속이 거짓이 된다.
    """
    import json
    import tempfile

    from app.reviews.collected import CollectedReviews

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "gpu-x.json").write_text(json.dumps({
            "reviews": [{"review_id": "r1", "kind": "리뷰", "text": "78도까지 오릅니다",
                         "source_url": "https://example/1", "risk": None}]}), encoding="utf-8")
        src = CollectedReviews(directory=Path(tmp))
        assert src.may_quote is False
        reviews = src.fetch("gpu-x")
        assert len(reviews) == 1 and reviews[0].risk is None
        assert reviews[0].bears_on == [] and reviews[0].contradicts == []
        assert src.unscored_count() == 1
        assert sum(src.risk_distribution().values()) == 0
        assert src.fetch("없는-품목") == []
    print("  ✓ 실소스는 인용하지 않고 조작 확률을 지어내지 않는다")


def check_missing_data_speaks_up() -> None:
    """
    데이터가 없을 때 조용히 빈 결과를 내면 '근거 없음' 판정으로 둔갑한다 —
    데이터가 없는 것과 반증이 없는 것이 구분되지 않는다.
    """
    from app.reviews.collected import CollectedReviews
    try:
        CollectedReviews(directory=Path("/tmp/없는디렉터리-리뷰"))
    except RuntimeError as e:
        assert "collect_reviews.py" in str(e), e
        print("  ✓ 리뷰 데이터가 없으면 무엇을 해야 하는지 말하며 멈춘다")
        return
    raise AssertionError("데이터가 없는데도 소스가 만들어졌습니다")


def check_label_matcher_is_refused_for_real_reviews() -> None:
    """
    실 리뷰 + `MATCH_MODE=label` 은 **에러 없이 전부 "근거 없음"** 이 된다.
    화면에는 "300건을 봤는데 근거가 없다"로 나오지만 실은 대조를 안 한 것이다.
    """
    import os

    from app.reviews import build_source

    saved = dict(os.environ)
    try:
        os.environ["REVIEW_SOURCE"] = "collected"
        os.environ["MATCH_MODE"] = "label"
        import app.reviews as reviews_pkg
        reviews_pkg.REVIEW_SOURCE = "collected"
        try:
            build_source(object())
        except RuntimeError as e:
            assert "MATCH_MODE=llm" in str(e), e
            print("  ✓ 라벨 대조기로 실 리뷰를 읽는 조합을 막는다")
            return
        raise AssertionError("막히지 않았습니다")
    finally:
        os.environ.clear()
        os.environ.update(saved)
        import app.reviews as reviews_pkg
        reviews_pkg.REVIEW_SOURCE = os.environ.get("REVIEW_SOURCE", "synthetic").lower()


def check_normalize_keeps_meaning() -> None:
    assert normalize("발열이   심해요<br />정말") == "발열이 심해요 정말"
    assert normalize("좋아요ㅋㅋㅋㅋㅋㅋ") == "좋아요ㅋㅋㅋ"
    print("  ✓ 정규화가 뜻을 바꾸지 않는다")


def main() -> int:
    checks = [
        check_prebuilt_pc_is_rejected,
        check_suffix_makes_a_different_product,
        check_word_named_products,
        check_model_key,
        check_pii_never_survives,
        check_boilerplate_is_dropped,
        check_other_option_reviews_are_dropped,
        check_cleaning_counts_what_it_dropped,
        check_kind_maps_to_engine_buckets,
        check_collected_source_never_quotes_and_never_scores,
        check_missing_data_speaks_up,
        check_label_matcher_is_refused_for_real_reviews,
        check_normalize_keeps_meaning,
    ]
    failed = 0
    print("리뷰 수집·클렌징 검증")
    for fn in checks:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print("전부 통과" if not failed else f"{failed}건 실패")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
