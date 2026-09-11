"""
리뷰 수집기 — `data/parts.json` 의 품목에 붙는 리뷰를 모아 `data/reviews_raw/` 에 둔다.

    .venv/bin/python scripts/collect_reviews.py probe            # 어디에 얼마나 있는지만 잰다
    .venv/bin/python scripts/collect_reviews.py collect --top 50 # 상위 N개 품목의 리뷰를 받는다

[원문은 커밋하지 않는다]
`data/reviews_raw/` 는 `.gitignore` 로 막혀 있다. 2026-09-08 방침(리뷰 원문을 그대로
저장·노출하지 않는다)에 대한 이 저장소의 타협점이다 — **로컬에는 남기되 저장소로는
나가지 않는다.** 커밋되는 것은 `data/reviews_snapshot.json` 의 파생 지표뿐이고,
그건 `scripts/build_review_snapshot.py` 가 만든다.

[개인정보는 애초에 안 받아 적는다]
다나와 의견에는 닉네임과 부분 마스킹된 IP(`112.xxx.56.193`)가 함께 온다. 지우는
단계를 뒤에 두면 그 사이 파일에는 남아 있다. 그래서 **파싱하는 자리에서 버린다** —
IP 는 통째로 버리고, 닉네임은 소금친 해시만 남긴다. 해시를 남기는 이유는 하나다:
같은 사람이 같은 상품에 여러 건을 쓴 것이 조작 신호라서, 조작 확률을 매기려면
"같은 사람인가"만 알면 되고 "누구인가"는 필요 없다.

[두 가지를 두려워하며 짰다]
1. **엉뚱한 상품에 붙기.** `Core Ultra 7 265K` 를 검색하면 1위가 그 CPU 를 넣은
   **완제품 PC** 다(실측 2026-09-09). 그대로 쓰면 완제품 리뷰로 CPU 주장을 판정한다.
   그래서 모델 토큰 일치율·제외어·카테고리 코드 세 가지로 거른다
2. **상대 서버에 부담 주기.** 요청 간 간격을 두고, 연속 실패 3회면 그 자리에서
   멈춘다. 재시도가 재시도를 부르는 것이 회의에서 나온 걱정(계정 단위 차단)이라,
   백오프가 아니라 **중단**이 기본이다
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARTS = ROOT / "data" / "parts.json"
RAW_DIR = ROOT / "data" / "reviews_raw"
PROBE_OUT = ROOT / "data" / "review_probe.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 닉네임 해시용 소금. 저장소에 그대로 두는 이유는, 이 해시의 목적이 비밀 유지가
# 아니라 **같은 작성자를 알아보는 것**이기 때문이다. 소금을 바꾸면 이전에 모은
# 것과 대조가 안 되므로 고정한다.
NICK_SALT = "skn_final/reviews/2026-09"

# 검색 결과에서 이것이 제목에 있으면 그 품목이 아니다. 완제품·중고·번들이
# 부품 검색 상위에 섞인다.
EXCLUDE_WORDS = ("조립pc", "완제품", "다나와표준pc", "중고", "리퍼", "렌탈", "대여",
                 "세트상품", "번들", "케이블", "브라켓", "받침대", "거치대")

# 영문 제품명으로는 국내 검색이 안 잡히는 것들. 검색이 비면 이 표로 한 번 더 친다.
QUERY_ALIAS = {
    "GeForce": "지포스", "Radeon": "라데온", "Ryzen": "라이젠",
    "Core Ultra": "코어 울트라", "Core i9": "코어i9", "Core i7": "코어i7",
    "Core i5": "코어i5", "Core i3": "코어i3",
}


# ── 예의 ────────────────────────────────────────────────────────────────────
class Fetcher:
    """
    요청을 내는 유일한 자리. 간격·robots·연속 실패를 여기서만 본다.

    [robots 를 우리 UA 로 직접 읽는다]
    `urllib.robotparser` 의 `read()` 는 파이썬 기본 UA 로 가져오는데, 다나와는
    거기에 403 을 준다. 그러면 `RobotFileParser` 가 **전부 금지**로 해석해서
    (403 은 disallow_all) 막히지 않은 주소까지 막힌 것으로 보인다. 실제로
    처음 실사가 요청 0회로 끝난 이유가 이것이었다. 그래서 본문을 직접 받아
    `parse()` 에 넣는다.

    [Crawl-delay 를 지킨다]
    `search.danawa.com` 은 `Crawl-delay: 10` 을 적어 뒀다(2026-09-09 확인).
    호스트마다 다르므로 간격도 호스트별로 따로 센다. 검색 332건이면 55분이라
    이 값은 실사를 오래 걸리게 만드는 주범인데, 적어 둔 값을 우리가 깎을
    이유가 없다.

    **연속 실패에는 백오프가 아니라 중단으로 대응한다.** 실패가 이어지는 것은
    대개 차단됐다는 뜻이고, 그때 재시도를 계속하면 차단이 넓어진다.
    """

    def __init__(self, delay: float = 1.5, max_consecutive_failures: int = 3) -> None:
        self.delay = delay
        self.max_fail = max_consecutive_failures
        self.fails = 0
        self.requests = 0
        self._robots: dict[str, tuple[object, float] | None] = {}
        self._last: dict[str, float] = {}

    def _rules(self, url: str):
        """(robots 파서, 이 호스트에 지킬 간격). 못 읽으면 (None, delay)."""
        host = urllib.parse.urlsplit(url)._replace(path="", query="", fragment="").geturl()
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        wait = self.delay
        try:
            req = urllib.request.Request(host + "/robots.txt", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("utf-8", "replace")
            rp.parse(body.splitlines())
            declared = rp.crawl_delay(UA) or rp.crawl_delay("*")
            if declared:
                wait = max(wait, float(declared))
        except Exception as exc:
            print(f"  ! robots.txt 를 못 읽었습니다({host}): {exc} — 이 호스트는 건너뜁니다")
            self._robots[host] = None
            return None
        self._robots[host] = (rp, wait)
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        """
        **못 읽으면 안 간다.** 모르는 상태에서 가져오는 것과 금지된 것을 구분할
        방법이 없고, 둘 중 잘못 고르면 상대 서버 쪽에 피해가 간다.
        """
        rules = self._rules(url)
        if rules is None:
            return False
        rp, _ = rules
        return rp.can_fetch(UA, url)

    def get(self, url: str, referer: str = "", ajax: bool = False) -> str:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt 가 막는 주소입니다: {url}")
        host = urllib.parse.urlsplit(url).netloc
        wait = self._rules(url)[1]
        gap = wait - (time.monotonic() - self._last.get(host, 0.0))
        if gap > 0:
            time.sleep(gap + random.uniform(0, 0.4))
        headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9",
                   "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            self.fails = 0
            self.requests += 1
            self._last[host] = time.monotonic()
            return raw.decode("utf-8", "replace")
        except Exception:
            self.fails += 1
            self._last[host] = time.monotonic()
            if self.fails >= self.max_fail:
                raise SystemExit(
                    f"연속 {self.fails}회 실패해 멈춥니다. 차단됐을 수 있으니 "
                    f"재시도 대신 시간을 두고 다시 도세요. (총 요청 {self.requests}회)")
            raise


# ── 파싱 도우미 ──────────────────────────────────────────────────────────────
def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def nick_hash(nickname: str) -> str:
    if not nickname:
        return ""
    return hashlib.sha256((NICK_SALT + nickname).encode()).hexdigest()[:16]


# 다나와 분류 코드. **2026-09-09 에 부품군마다 검색해서 눈으로 확인한 값이다.**
# 이 표가 완제품 PC(112756 · 11316681)를 걸러 내는 핵심이다 — 이름만 보면
# `영웅컴퓨터 영웅 RL3461F i5-13400F RTX5060Ti` 가 `Core i5-13400F` 와 100%
# 일치한다. 실제로 첫 실사에서 이게 통과했고, CPU 리뷰 자리에 완제품 PC 리뷰가
# 들어올 뻔했다.
TYPE_CATE: dict[str, set[str]] = {
    "cpu":       {"113973", "113990"},   # 인텔 / AMD 가 다른 코드다
    "gpu":       {"112753"},
    "mainboard": {"112751"},
    "ram":       {"112752"},
    "ssd":       {"112760"},
    "psu":       {"112777"},
    "case":      {"112775"},
    "cooler":    {"11236855"},
}

# 모델명 뒤에 붙어 **다른 제품이 되는** 꼬리표. 이걸 안 보면 `RTX 5070` 이
# `RTX 5070 Ti` 에 붙는다.
SUFFIXES = ("XTX", "X3D", "SUPER", "TI", "XT", "KF", "KS", "PRO", "EVO", "PLUS", "MAX", "SE")

# 제목에 브랜드가 한국어로만 나오는 경우가 많다.
BRAND_ALIAS = {
    "intel": ("인텔", "intel"), "amd": ("amd", "라이젠", "라데온"),
    "nvidia": ("지포스", "nvidia", "rtx", "gtx"),
    "Samsung": ("삼성", "samsung"), "SK hynix": ("sk하이닉스", "skhynix", "하이닉스"),
    "Western Digital": ("wd", "웨스턴디지털"), "Seagate": ("씨게이트", "seagate"),
    "Micronics": ("마이크로닉스", "micronics"), "Cooler Master": ("쿨러마스터", "coolermaster"),
    "Lian Li": ("리안리", "lianli"), "Fractal Design": ("프랙탈", "fractal"),
    "Corsair": ("커세어", "corsair"), "Antec": ("안텍", "antec"),
    "Zalman": ("잘만", "zalman"), "ABKO": ("앱코", "abko"), "Montech": ("몬텍", "montech"),
    "be quiet!": ("비콰이엇", "bequiet"), "Noctua": ("녹투아", "noctua"),
    "Thermalright": ("써멀라이트", "thermalright"), "DAVEN": ("데이븐", "daven"),
    "Phanteks": ("판테크", "판텍스", "phanteks"), "HYTE": ("하이트", "hyte"),
    "Seasonic": ("시소닉", "seasonic"), "FSP": ("fsp", "에프에스피"),
    "Deepcool": ("딥쿨", "deepcool"), "NZXT": ("nzxt", "엔지엑스티"),
    "Crucial": ("마이크론", "크루셜", "crucial"), "Kingston": ("킹스톤", "kingston"),
    "TeamGroup": ("팀그룹", "teamgroup", "t-force", "tforce"),
    "G.SKILL": ("지스킬", "gskill", "g.skill"), "KLEVV": ("klevv", "클레브"),
    "ADATA": ("adata", "에이데이타", "xpg"), "Lexar": ("렉사", "lexar"),
    "SuperFlower": ("슈퍼플라워", "superflower"), "Enermax": ("에너맥스", "enermax"),
    "Thermaltake": ("써멀테이크", "thermaltake"), "3RSYS": ("3rsys", "쓰리알"),
    "darkflash": ("다크플래쉬", "darkflash"), "JONSBO": ("조스보", "jonsbo"),
    "ARCTIC": ("arctic", "아틱"), "PCCOOLER": ("pccooler", "피씨쿨러"),
    "ID-COOLING": ("idcooling", "아이디쿨링"), "GAMDIAS": ("gamdias", "감디아스"),
    "MAXELITE": ("맥스엘리트", "maxelite"), "Micronics": ("마이크로닉스", "micronics"),
}


def norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


def model_key(name: str) -> tuple[str, str]:
    """
    이름에서 **모델 핵심 번호와 꼬리표**를 뽑는다.

        GeForce RTX 5070 Ti   → ("5070", "TI")
        Core i5-13400F        → ("13400", "F")
        Ryzen 7 9800X3D       → ("9800", "X3D")
        B650M Pro RS          → ("B650", "M")
        990 PRO               → ("990", "PRO")

    세 자리 이상 숫자를 핵심으로 본다. 그 앞의 알파벳(`B650` 의 `B`)은 붙이고,
    뒤에 붙거나 뒤 낱말로 떨어져 있는 꼬리표는 따로 든다 — **꼬리표가 다르면
    다른 제품**이라, 이걸 안 보면 5070 리뷰가 5070 Ti 에 붙는다.
    """
    upper = name.upper()
    matches = list(re.finditer(r"([A-Z]{0,2})(\d{3,5})([A-Z0-9]{0,4})", upper))
    if not matches:
        return "", ""
    m = max(matches, key=lambda x: len(x.group(2)))
    core = (m.group(1) + m.group(2)).lower()
    tail = m.group(3)
    suffix = ""
    for s in SUFFIXES:
        if tail.startswith(s):
            suffix = s
            break
    if not suffix and tail and tail.isalpha() and len(tail) <= 2:
        suffix = tail                      # 13400`F`, B650`M`
    if not suffix:                         # 낱말로 떨어져 있는 꼬리표 (RTX 5070 `Ti`)
        rest = upper[m.end():]
        for word in re.split(r"[^A-Z0-9]+", rest):
            if word in SUFFIXES:
                suffix = word
                break
    return core, suffix.lower()


# 제목이 한국어라 낱말이 번역돼 있다. `Core Ultra 5 250K Plus` 의 제목은
# `인텔 코어 울트라 5 시리즈2 250K Plus` 다.
WORD_ALIAS = {
    "core": ("코어",), "ultra": ("울트라",), "geforce": ("지포스",),
    "radeon": ("라데온",), "ryzen": ("라이젠",), "titan": ("타이탄",),
    "threadripper": ("스레드리퍼",), "plus": ("plus", "플러스"),
}


def word_in_title(word: str, title_norm: str) -> bool:
    if word in title_norm:
        return True
    return any(norm(a) in title_norm for a in WORD_ALIAS.get(word, ()))


def distinctive_words(name: str) -> list[str]:
    """제품을 가리키는 낱말. 네 글자 이상만 — `pro`·`rgb` 는 어디에나 있다."""
    return [w.lower() for w in re.findall(r"[A-Za-z]{4,}", name)
            if w.lower() not in ("argb", "gaming", "series", "edition", "white", "black")]


def title_matches(part_name: str, title: str) -> tuple[bool, str]:
    """
    제목이 그 제품인가. **아니라고 판단한 이유를 함께 돌려준다** — 나중에
    실사 결과를 사람이 훑을 때 이유 없이 빠진 것이 제일 곤란하다.

    이름이 낱말로 서는 제품(`Peerless Assassin 120`)과 번호로 서는 제품
    (`RTX 5070 Ti`)을 다르게 본다. 낱말이 둘 이상 다 맞으면 뒤에 붙은 변형
    표기(`… 120 SE`)는 같은 제품 계열로 받아들인다 — 여기서까지 엄격하면
    쿨러·케이스가 통째로 빠진다. 번호로 서는 제품은 반대로 꼬리표 하나에
    다른 제품이 되므로 엄격하게 본다.
    """
    core, suffix = model_key(part_name)
    t = norm(title)
    words = distinctive_words(part_name)
    if len(words) >= 2:
        # 낱말이 이름의 정체인 제품이다. 하나라도 없으면 다른 제품이다
        # (`Peerless Assassin 120` 과 `Assassin X 120 Refined` 는 다르다).
        missing = [w for w in words if not word_in_title(w, t)]
        if missing:
            return False, f"낱말 불일치({','.join(missing[:2])})"
        if not core or core in t:
            return True, ""
    if not core:
        return (norm(part_name)[:12] in t, "이름 대조")
    if core not in t:
        return False, f"모델 번호 {core} 없음"
    pos = t.index(core) + len(core)
    after = t[pos:pos + 6]
    if suffix:
        if not after.startswith(norm(suffix)):
            return False, f"꼬리표 {suffix} 없음"
    else:
        for s in SUFFIXES:                 # 우리에겐 없는 꼬리표가 제목에 붙어 있다
            if after.startswith(s.lower()):
                return False, f"제목에 다른 꼬리표({s})가 붙어 있음"
    return True, ""


def brand_ok(brand: str, title: str) -> bool:
    """
    브랜드가 맞는가. **모르는 것과 틀린 것을 가른다.**

    별칭표에 없는 브랜드는 널렸고(70종이 표기만 달리해 들어 있다), 못 찾았다는
    이유로 빼면 멀쩡한 후보가 사라진다. 그래서 별칭을 찾으면 통과,
    **못 찾았는데 제목에 다른 브랜드가 또렷이 있으면** 그때만 거부한다.
    """
    t = norm(title)
    aliases = BRAND_ALIAS.get(brand, (brand,))
    if any(norm(a) and norm(a) in t for a in aliases):
        return True
    for other, others in BRAND_ALIAS.items():
        if other == brand:
            continue
        if any(norm(a) and norm(a) in t for a in others):
            return False
    return True


# ── 소스 ────────────────────────────────────────────────────────────────────
class DanawaSource:
    """
    다나와. **두 종류를 따로 가져온다** — 다나와 상품의견(의견·질문·후기)과
    쇼핑몰 상품리뷰다. 엔진의 `Evidence.samples` 가 소스별로 쪼개지는 dict 라
    (`{"리뷰": 214, "QA": 31}`) 여기서 섞어 버리면 그 칸을 못 채운다.
    """

    name = "danawa"
    SEARCH = "https://search.danawa.com/dsearch.php?query={q}"
    OPINION = ("https://prod.danawa.com/info/dpg/ajax/productOpinion.ajax.php"
               "?prodCode={pcode}&page={page}&limit={limit}&score=0&sortType=&usefullScore=Y")
    MALL = ("https://prod.danawa.com/info/dpg/ajax/companyProductReview.ajax.php"
            "?prodCode={pcode}&page={page}&limit={limit}&score=0&sortType=NEW&usefullScore=Y")

    def __init__(self, fetcher: Fetcher) -> None:
        self.f = fetcher

    def product_url(self, pcode: str) -> str:
        return f"https://prod.danawa.com/info/?pcode={pcode}"

    # 검색 ------------------------------------------------------------------
    def search(self, query: str) -> list[dict]:
        html = self.f.get(self.SEARCH.format(q=urllib.parse.quote(query)))
        out: list[dict] = []
        for block in re.split(r'(?=<div class="prod_main_info">)', html)[1:]:
            m = re.search(r'<p class="prod_name">\s*<a\s+href="([^"]*pcode=(\d+)[^"]*)"[^>]*>(.*?)</a>',
                          block, re.S)
            if not m:
                continue
            # 분류 코드는 상품 링크의 `cate=` 에 있다. 페이지 안의
            # `hidden_cate_sub_c1` 은 **대표 상품 하나에만** 붙어 있어서(2026-09-09
            # 확인) 후보별로 쓸 수 없다 — 이걸 쓰면 2위 이하가 전부 분류 미상이 된다.
            cate = re.search(r"[?&]cate=(\d+)", unescape(m.group(1)))
            out.append({"product_id": m.group(2), "title": text_of(m.group(3)),
                        "cate": cate.group(1) if cate else ""})
        return out

    # 리뷰 ------------------------------------------------------------------
    def opinions(self, pcode: str, page: int, limit: int) -> list[dict]:
        html = self.f.get(self.OPINION.format(pcode=pcode, page=page, limit=limit),
                          referer=self.product_url(pcode), ajax=True)
        out: list[dict] = []
        for li in re.split(r'(?=<li class="cmt_item")', html)[1:]:
            rid = (re.search(r'productOpinion-list-self-(\d+)', li) or [None, ""])[1] \
                if re.search(r'productOpinion-list-self-(\d+)', li) else ""
            rid = re.search(r'productOpinion-list-self-(\d+)', li)
            rid = rid.group(1) if rid else ""
            # 본문은 hidden input 에 HTML 없이 그대로 들어 있다.
            body = re.search(r'productOpinion-content-text-\d+"\s+type="hidden"\s+value="(.*?)">',
                             li, re.S)
            if not body:
                continue
            kind = re.search(r'class="lb_qs[^"]*"[^>]*>(.*?)</span>', li)
            nick = re.search(r'productOpinion-nickname-\d+"\s*>(.*?)</strong>', li, re.S)
            date = re.search(r'<span class="date">(.*?)</span>', li, re.S)
            out.append({
                "review_id": rid,
                "kind": text_of(kind.group(1)) if kind else "의견",
                "text": unescape(body.group(1)).strip(),
                "date": text_of(date.group(1)) if date else "",
                # IP 는 받아 적지 않는다. 닉네임은 해시만 남긴다.
                "author_hash": nick_hash(text_of(nick.group(1)) if nick else ""),
                "rating": None,
                "mall": "",
            })
        return out

    def mall_reviews(self, pcode: str, page: int, limit: int) -> list[dict]:
        html = self.f.get(self.MALL.format(pcode=pcode, page=page, limit=limit),
                          referer=self.product_url(pcode), ajax=True)
        out: list[dict] = []
        for li in re.split(r'(?=<li class="danawa-prodBlog-companyReview-clazz-more")', html)[1:]:
            title = re.search(r'<p class="tit">(.*?)</p>', li, re.S)
            body = re.search(r'<div class="atc">(.*?)</div>', li, re.S)
            if not body:
                continue
            star = re.search(r'<span class="star_mask"[^>]*>(\d+)점</span>', li)
            mall = re.search(r'<img[^>]+alt="([^"]+)"', li)
            date = re.search(r'<span class="date">(.*?)</span>', li, re.S)
            nick = re.search(r'<span class="name">(.*?)</span>', li, re.S)
            rid = re.search(r'companyReview-button-side-(\d+)', li)
            joined = " ".join(x for x in [text_of(title.group(1)) if title else "",
                                          text_of(body.group(1))] if x)
            out.append({
                "review_id": rid.group(1) if rid else "",
                "kind": "몰리뷰",
                "text": joined,
                "date": text_of(date.group(1)) if date else "",
                "author_hash": nick_hash(text_of(nick.group(1)) if nick else ""),
                "rating": int(star.group(1)) / 20 if star else None,   # 100점 → 5점
                "mall": text_of(mall.group(1)) if mall else "",
            })
        return out

    def counts(self, pcode: str) -> dict:
        """탭에 적힌 총 건수. 한 페이지만 받아서 읽는다 — 수집 전에 규모를 아는 값."""
        html = self.f.get(self.OPINION.format(pcode=pcode, page=1, limit=1),
                          referer=self.product_url(pcode), ajax=True)
        t = text_of(html)
        # **천 단위 쉼표가 붙는다**(`다나와 상품의견 1,886`). `\d+` 로 읽으면
        # 1,000건 넘는 인기 상품이 전부 0건으로 잡혀서 — 리뷰가 가장 많은
        # 상품만 골라 빠진다. 실사 첫판에서 9800X3D 가 이렇게 0건이 됐다.
        num = lambda x: int(x.replace(",", "")) if x else 0
        m = re.search(r"다나와 상품의견\s*([\d,]+)?\s*쇼핑몰 상품리뷰\s*([\d,]+)?", t)
        d = re.search(r"전체보기\s*([\d,]+)\s*의견\s*([\d,]+)\s*질문/답변\s*([\d,]+)\s*후기\s*([\d,]+)", t)
        return {
            "opinion_total": num(m.group(1)) if m else 0,
            "mall_total": num(m.group(2)) if m else 0,
            "opinion": num(d.group(2)) if d else 0,
            "qa": num(d.group(3)) if d else 0,
            "review": num(d.group(4)) if d else 0,
        }


# 새 소스는 여기 한 줄. `app/reviews/__init__.py` 의 `_SOURCES` 와 같은 규칙이다.
SOURCES = {"danawa": DanawaSource}


# ── 매칭 ────────────────────────────────────────────────────────────────────
def queries_for(part: dict) -> list[str]:
    """
    검색어. **국내 표기를 먼저 친다.**

    제목이 한국어라 `Core Ultra 7 265K` 로 치면 완제품 PC 만 올라온다. 실사
    첫판에서 인텔 울트라 계열이 통째로 빠진 이유가 이것이었다. 검색 한 번이
    10초(Crawl-delay)라 순서가 곧 시간이다.
    """
    name, brand = part["name"], part["brand"]
    alias = name
    for en, ko in QUERY_ALIAS.items():
        if en.lower() in name.lower():
            alias = re.sub(re.escape(en), ko, alias, flags=re.I)
    prefix = {"intel": "인텔", "amd": "AMD", "nvidia": ""}.get(brand.lower(), brand)
    first = f"{prefix} {alias}".strip()
    out = [first]
    for q in (f"{prefix} {name}".strip(), name):
        if q not in out:
            out.append(q)
    return out


def pick(part: dict, candidates: list[dict]) -> list[dict]:
    """
    후보 중 그 품목인 것만. **네 겹으로 거른다.**

    1. 분류 코드 — 완제품 PC 를 여기서 잘라 낸다. 가장 강한 필터다
    2. 제외어 — 중고·번들·액세서리
    3. 모델 번호와 꼬리표 — `5070` 이 `5070 Ti` 에 붙는 것을 막는다
    4. 브랜드 — 제목이 한국어라 별칭표를 본다

    거른 것도 버리지 않고 `rejected` 를 달아 남긴다. 몇 개가 왜 빠졌는지
    보이지 않으면 매칭이 조용히 틀려도 알 수가 없다.
    """
    allowed_cate = TYPE_CATE.get(part["type"])
    out = []
    for c in candidates:
        c = dict(c)
        low = c["title"].lower().replace(" ", "")
        ok, why = title_matches(part["name"], c["title"])
        if allowed_cate and c.get("cate") and c["cate"] not in allowed_cate:
            c["rejected"] = f"분류 코드 다름({c['cate']})"
        elif any(w in low for w in EXCLUDE_WORDS):
            c["rejected"] = "제외어"
        elif not ok:
            c["rejected"] = why
        elif not brand_ok(part["brand"], c["title"]):
            c["rejected"] = f"브랜드 불일치({part['brand']})"
        out.append(c)
    return out


def probe(args) -> None:
    parts = json.loads(PARTS.read_text())["parts"]
    if args.types:
        want = set(args.types.split(","))
        parts = [p for p in parts if p["type"] in want]
    if args.limit:
        parts = parts[: args.limit]

    fetcher = Fetcher(delay=args.delay)
    src = SOURCES[args.source](fetcher)
    done = {}
    if PROBE_OUT.exists() and not args.restart:
        done = {r["part_code"]: r for r in json.loads(PROBE_OUT.read_text())["results"]}
    if args.retry_missed:
        # 매칭에 실패한 것만 다시 잰다. 매칭 규칙을 고친 뒤 쓰는 자리다 —
        # 전부 다시 도는 것은 Crawl-delay 때문에 몇 시간이 든다.
        done = {k: v for k, v in done.items() if v.get("total")}

    results = list(done.values())
    todo = [p for p in parts if p["part_code"] not in done]
    print(f"실사 대상 {len(todo)}개 (이미 잰 것 {len(done)}개) · 소스 {src.name}")
    try:
        for i, part in enumerate(todo, 1):
            rec = {"part_code": part["part_code"], "type": part["type"],
                   "name": part["name"], "brand": part["brand"], "candidates": []}
            try:
                cands: list[dict] = []
                for q in queries_for(part):
                    cands = src.search(q)[: args.candidates]
                    scored = pick(part, cands)
                    if any("rejected" not in c for c in scored):
                        rec["query"] = q
                        rec["candidates"] = scored
                        break
                    rec["query"] = q
                    rec["candidates"] = scored
                best = next((c for c in rec["candidates"] if "rejected" not in c), None)
                if best:
                    best.update(src.counts(best["product_id"]))
                    rec["total"] = best["opinion_total"] + best["mall_total"]
            except SystemExit:
                raise
            except Exception as exc:                       # 한 품목 실패로 전체를 버리지 않는다
                rec["error"] = f"{type(exc).__name__}: {exc}"
            results.append(rec)
            if i % 10 == 0 or i == len(todo):
                PROBE_OUT.write_text(json.dumps({"source": src.name, "results": results},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
                got = sum(1 for r in results if r.get("total"))
                print(f"  {i}/{len(todo)}  매칭 {got}개  요청 {fetcher.requests}회")
    finally:
        PROBE_OUT.write_text(json.dumps({"source": src.name, "results": results},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
    summarize_probe(results)


def summarize_probe(results: list[dict]) -> None:
    matched = [r for r in results if r.get("total")]
    print(f"\n실사 결과 — 품목 {len(results)}개 중 매칭 {len(matched)}개")
    if not matched:
        return
    totals = sorted((r["total"] for r in matched), reverse=True)
    print(f"  리뷰 합계 중앙값 {totals[len(totals) // 2]}건 · 최대 {totals[0]}건 · "
          f"100건 이상 {sum(1 for t in totals if t >= 100)}개")
    by = Counter(r["type"] for r in matched)
    print("  부품군별 매칭:", dict(by))
    # 카테고리 코드가 부품군 안에서 갈리면 엉뚱한 상품이 섞였다는 신호다.
    for t in sorted({r["type"] for r in matched}):
        cates = Counter(next(c for c in r["candidates"] if "rejected" not in c)["cate"]
                        for r in matched if r["type"] == t)
        if len(cates) > 1:
            print(f"    ! {t}: 카테고리 코드가 {len(cates)}종 — {dict(cates)}")


def collect(args) -> None:
    probe_doc = json.loads(PROBE_OUT.read_text())
    results = [r for r in probe_doc["results"] if r.get("total")]

    # 분류 코드는 `pick()` 이 이미 `TYPE_CATE` 허용 목록으로 걸렀다. 여기서
    # **최빈값**으로 한 번 더 거르던 것을 걷어냈다 — 한 부품군에 코드가 여럿인
    # 경우(cpu 는 인텔 113973 · AMD 113990)에 소수 쪽이 통째로 빠진다.
    # 실제로 인텔 CPU 18품목이 이 필터에 걸려 수집 대상에서 사라졌다.
    kept = [(r, next(x for x in r["candidates"] if "rejected" not in x)) for r in results]

    kept.sort(key=lambda rb: rb[0]["total"], reverse=True)
    if args.balanced:
        types = {r["type"] for r, _ in kept} or {"?"}
        per = max(1, args.top // len(types))
        picked, seen = [], Counter()
        for r, b in kept:
            if seen[r["type"]] < per:
                picked.append((r, b))
                seen[r["type"]] += 1
        kept = picked
    kept = kept[: args.top]
    print(f"수집 대상 {len(kept)}개 품목 (리뷰 합계 {sum(r['total'] for r, _ in kept)}건 예상)")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(delay=args.delay)
    src = SOURCES[probe_doc["source"]](fetcher)
    for i, (r, best) in enumerate(kept, 1):
        out = RAW_DIR / f"{r['part_code']}.json"
        if out.exists() and not args.overwrite:
            continue
        pcode = best["product_id"]
        rows: list[dict] = []
        try:
            for page in range(1, args.pages + 1):
                got = src.opinions(pcode, page, args.limit)
                rows += got
                if len(got) < args.limit:
                    break
            for page in range(1, args.pages + 1):
                got = src.mall_reviews(pcode, page, args.limit)
                rows += got
                if len(got) < args.limit:
                    break
        except SystemExit:
            raise
        except Exception as exc:
            print(f"  ! {r['part_code']} 수집 실패: {type(exc).__name__}: {exc}")
            continue
        doc = {
            "part_code": r["part_code"], "name": r["name"], "type": r["type"],
            "source": src.name, "product_id": pcode, "product_title": best["title"],
            "product_url": src.product_url(pcode), "match_score": best.get("score"),
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "counts_reported": {k: best.get(k) for k in
                                ("opinion_total", "mall_total", "opinion", "qa", "review")},
            "reviews": rows,
        }
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {i}/{len(kept)} {r['part_code']} ← {len(rows)}건 ({best['title'][:40]})")
    print(f"끝. 요청 {fetcher.requests}회 · {RAW_DIR} (커밋되지 않는 자리)")


def main() -> None:
    ap = argparse.ArgumentParser(description="리뷰 수집 — 실사(probe)와 수집(collect)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="품목마다 리뷰가 어디에 몇 건 있는지만 잰다")
    p.add_argument("--source", default="danawa", choices=list(SOURCES))
    p.add_argument("--types", default="", help="쉼표로 부품군 제한 (예: gpu,cpu)")
    p.add_argument("--limit", type=int, default=0, help="품목 수 제한")
    p.add_argument("--candidates", type=int, default=8)
    p.add_argument("--delay", type=float, default=1.5)
    p.add_argument("--restart", action="store_true", help="이전 실사 결과를 버리고 처음부터")
    p.add_argument("--retry-missed", action="store_true", help="매칭 실패한 것만 다시")
    p.set_defaults(func=probe)

    c = sub.add_parser("collect", help="실사 결과에서 상위 N개 품목의 리뷰를 받는다")
    c.add_argument("--top", type=int, default=50)
    c.add_argument("--pages", type=int, default=3)
    c.add_argument("--limit", type=int, default=100, help="한 페이지 건수")
    c.add_argument("--delay", type=float, default=1.5)
    c.add_argument("--balanced", action="store_true", help="부품군을 고르게 뽑는다")
    c.add_argument("--overwrite", action="store_true")
    c.set_defaults(func=collect)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
