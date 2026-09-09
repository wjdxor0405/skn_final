"""
클렌징 — 받아 온 리뷰를 대조에 쓸 수 있는 모양으로 만든다.

[문장으로 쪼개지 않는다]
처음에 리뷰를 문장 단위로 쪼개려 했다. 짧을수록 "이 주장에 닿는가" 판정이
선명해지기 때문이다. **그런데 그러면 표본 수가 부풀어 판정이 바뀐다** —
`verify.py` 의 `MIN_RELEVANT`·`REFUTE_RATIO` 는 리뷰 **건수**에 대한 값이고,
목업의 `리뷰 214` 도 건수다. 한 리뷰를 다섯 문장으로 쪼개면 같은 사람의 한
의견이 다섯 표본이 된다. 그래서 **리뷰 1건 = Review 1건**을 지키고, 대신
지나치게 긴 것만 자른다.

[개인정보는 두 겹으로 막는다]
수집기가 IP 를 아예 안 받아 적고 닉네임은 해시만 남긴다. 여기서는 **본문 안에**
들어간 것을 지운다 — 전화번호·이메일·주소·주문번호를 리뷰에 적는 사람이 있다.

[가장 무서운 것은 다른 제품의 리뷰다]
가격비교 사이트의 한 상품 페이지에는 용량·색상이 다른 옵션의 리뷰가 함께
붙는다. `990 PRO 2TB` 페이지에 `1TB 샀는데…` 가 섞이면, 그 리뷰로 2TB 의
쓰기 속도 주장을 반증하게 된다. **판정이 조용히 거짓이 되는 유일한 자리**라
용량·모델 토큰이 어긋나는 것은 표시해서 뺀다.

[조작 확률을 여기서 매기지 않는다]
신호(`risk_signals`)만 붙이고 점수는 비워 둔다. `Review.risk` 의 기본값이
`None` 인 이유가 그것이다 — 0.0 으로 채우면 "조작 확률 20% 이상을 걸렀다"는
화면의 약속이 조용히 무력해진다. 신호를 점수로 바꾸는 것은 `risk.py` 의 일이다.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter

# 이보다 짧으면 대조에 쓸 내용이 없다. 실측에서 "굿", "좋아요" 가 이 아래다.
MIN_LENGTH = 10
# 이보다 길면 자른다. 대조 비용이 길이에 비례하고, 뒤쪽은 대개 다른 이야기다.
MAX_LENGTH = 1200
# 근사 중복으로 볼 3-gram 자카드 유사도.
NEAR_DUP_RATIO = 0.85

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))

# 본문에 섞여 들어오는 개인정보. 지우지 않고 **무엇을 지웠는지 남기며** 바꾼다.
PII_PATTERNS = [
    (re.compile(r"\b01[016-9][-. ]?\d{3,4}[-. ]?\d{4}\b"), "[전화]"),
    (re.compile(r"\b0\d{1,2}[-. ]\d{3,4}[-. ]\d{4}\b"), "[전화]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[메일]"),
    (re.compile(r"\b\d{6}[-]\d{7}\b"), "[주민]"),
    (re.compile(r"\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}\b"), "[카드]"),
    (re.compile(r"https?://\S+"), "[링크]"),
    (re.compile(r"\b\d{10,14}\b"), "[번호]"),          # 송장·주문번호 자리
]

# 내용이 없는 상투구. 이것만으로 된 리뷰는 뺀다.
BOILERPLATE = re.compile(
    r"^(?:[.!~ㅎㅋ\s]*|굿|good|좋아요|좋습니다|만족|만족합니다|잘\s?샀|배송\s?빠[르릅]|"
    r"빠른\s?배송|감사합니다|추천합니다|가성비\s?굿)[.!~ㅎㅋ\s]*$", re.I)


def normalize(text: str) -> str:
    """
    유니코드·공백·반복문자를 고른다. **뜻을 바꾸지 않는 것만 한다.**

    NFKC 가 아니라 NFC 다. NFKC 는 한글 호환 자모(`ㅋ` U+314B)를 조합용 자모
    (U+110F)로 바꿔 버린다 — 글자는 비슷해 보이는데 `[ㄱ-ㅎㅏ-ㅣ]` 로 걸리지
    않아서, "ㅋㅋㅋ 만 있는 리뷰를 버린다"는 규칙이 조용히 무력해진다.
    검사(`tests/test_review_pipeline.py`)가 이 자리를 잡았다.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = text.translate(_ZERO_WIDTH)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"(.)\1{3,}", r"\1\1\1", text)     # ㅋㅋㅋㅋㅋ → ㅋㅋㅋ
    return re.sub(r"\s+", " ", text).strip()


def mask_pii(text: str) -> tuple[str, list[str]]:
    """본문 안의 개인정보를 가린다. 무엇을 가렸는지 함께 돌려준다."""
    found: list[str] = []
    for pattern, tag in PII_PATTERNS:
        if pattern.search(text):
            found.append(tag)
            text = pattern.sub(tag, text)
    return text, found


def is_empty_of_content(text: str) -> bool:
    if len(text) < MIN_LENGTH:
        return True
    if BOILERPLATE.match(text):
        return True
    # 자모만 있는 것 (ㅇㅇ, ㅋㅋ, ㄱㅅ)
    return not re.search(r"[가-힣a-zA-Z0-9]", re.sub(r"[ㄱ-ㅎㅏ-ㅣ]", "", text))


def shingles(text: str, n: int = 3) -> set[str]:
    s = re.sub(r"\s", "", text)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))} or {s}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# 용량·크기처럼 **옵션마다 다른 수치**. 리뷰가 품목과 다른 값을 말하면 다른
# 옵션의 리뷰일 수 있다.
_OPTION_TOKENS = re.compile(r"\b(\d+)\s?(TB|GB|W|mm|Hz)\b", re.I)


def option_mismatch(text: str, part_name: str) -> str:
    """
    리뷰가 품목과 다른 옵션을 말하는가. **확실할 때만 표시한다.**

    품목 이름에 수치가 있고(`990 PRO 2TB`), 리뷰가 같은 단위의 **다른** 수치만
    말할 때다. 품목 이름에 수치가 없으면 판단하지 않는다 — 넘겨짚어 빼면
    멀쩡한 표본이 사라진다.
    """
    mine = {(v.upper(), u.upper()) for v, u in _OPTION_TOKENS.findall(part_name)}
    if not mine:
        return ""
    units = {u for _, u in mine}
    theirs = {(v.upper(), u.upper()) for v, u in _OPTION_TOKENS.findall(text)
              if u.upper() in units}
    if theirs and not (theirs & mine):
        return f"품목은 {sorted(mine)} 인데 리뷰는 {sorted(theirs)} 를 말합니다"
    return ""


def risk_signals(row: dict, by_author: Counter, by_day: Counter) -> list[str]:
    """
    조작으로 의심할 만한 **신호**. 점수가 아니다.

    하나로 단정하지 않고 여러 개를 붙여 두는 이유는 기획안 §7 과 같다 —
    개별 리뷰의 진위를 맞히는 것이 목적이 아니라 분포를 보여주는 것이 목적이다.
    """
    out: list[str] = []
    if row.get("author_hash") and by_author[row["author_hash"]] >= 3:
        out.append("같은 작성자가 이 상품에 3건 이상")
    if row.get("rating") is not None and row["rating"] >= 5 and len(row["text"]) < 25:
        out.append("최고 평점인데 본문이 매우 짧음")
    day = (row.get("date") or "")[:11]
    if day and by_day[day] >= 10:
        out.append("같은 날짜에 몰림")
    return out


def clean_part(doc: dict) -> dict:
    """
    한 품목의 원문 묶음을 클렌징한다. **버린 것을 세어서 함께 돌려준다** —
    몇 건을 왜 뺐는지 말할 수 없으면 남은 건수도 믿을 수 없다.
    """
    rows = doc.get("reviews", [])
    part_name = doc.get("product_title") or doc.get("name") or ""
    by_author = Counter(r.get("author_hash") for r in rows if r.get("author_hash"))
    by_day = Counter((r.get("date") or "")[:11] for r in rows if r.get("date"))

    kept: list[dict] = []
    seen_exact: set[str] = set()
    seen_shingles: list[set[str]] = []
    dropped = Counter()
    pii_hits = Counter()

    for row in rows:
        text = normalize(row.get("text", ""))
        if not text:
            dropped["빈 본문"] += 1
            continue
        text, found = mask_pii(text)
        for tag in found:
            pii_hits[tag] += 1
        if is_empty_of_content(text):
            dropped["내용 없음"] += 1
            continue
        if len(text) > MAX_LENGTH:
            text = text[:MAX_LENGTH] + "…"
            dropped["잘림(버린 것 아님)"] += 1

        digest = hashlib.sha1(re.sub(r"\s", "", text).encode()).hexdigest()
        if digest in seen_exact:
            dropped["완전 중복"] += 1
            continue
        sh = shingles(text)
        if any(jaccard(sh, prev) >= NEAR_DUP_RATIO for prev in seen_shingles):
            dropped["근사 중복"] += 1
            continue
        seen_exact.add(digest)
        seen_shingles.append(sh)

        mismatch = option_mismatch(text, part_name)
        if mismatch:
            dropped["다른 옵션의 리뷰"] += 1
            continue

        kept.append({
            "review_id": f"{doc['source']}-{row.get('review_id') or digest[:10]}",
            "part_code": doc["part_code"],
            # 엔진의 `Evidence.samples` 가 소스별로 쪼개지는 dict 라 종류를 남긴다.
            # 다나와의 "질문" 은 목업의 QA 칸에 해당한다.
            "kind": "QA" if row.get("kind") in ("질문", "답변") else "리뷰",
            "text": text,
            "source_url": doc.get("product_url", ""),
            # **점수는 여기서 매기지 않는다.** None 은 "안 쟀다"이지 "깨끗하다"가 아니다.
            "risk": None,
            "risk_signals": risk_signals({**row, "text": text}, by_author, by_day),
            "rating": row.get("rating"),
            "date": row.get("date", ""),
        })

    return {
        "part_code": doc["part_code"],
        "name": doc.get("name", ""),
        "type": doc.get("type", ""),
        "source": doc["source"],
        "product_id": doc.get("product_id", ""),
        "product_title": doc.get("product_title", ""),
        "product_url": doc.get("product_url", ""),
        "collected_at": doc.get("collected_at", ""),
        "reviews": kept,
        "cleaning": {
            "in": len(rows),
            "out": len(kept),
            "dropped": dict(dropped),
            "pii_masked": dict(pii_hits),
            "with_risk_signal": sum(1 for r in kept if r["risk_signals"]),
        },
    }
