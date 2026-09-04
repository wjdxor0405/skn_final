#!/usr/bin/env python3
"""
Nexar(Octopart) supply 데이터 → API 응답 원본 스냅샷.

심사위원이 API 키 없이 재현할 수 있도록, 라이브 조회 결과를 JSON으로 떠서
저장소에 커밋하는 것이 목적이다. 앱은 CATALOG_SOURCE=snapshot 에서 이 파일을 읽는다.

[이 스크립트는 해석하지 않는다]
받은 응답을 그대로 적는다. 통화 환산·수량구간 선택·floor_price 합성·누락값 정규화는
전부 app/snapshot_catalog.py 가 "읽는 시점에" 한다. 이 파일에는 합성값이 하나도 없다.

이렇게 나눈 이유는 비대칭이다. 무료 플랜의 파트 한도는 조회한 '파트 수'로 깎여서
원본은 사실상 다시 못 뜬다. 반면 변환은 몇 밀리초면 다시 돈다. 되돌릴 수 없는 쪽을
손상 없이 남기고, 언제든 다시 할 수 있는 쪽을 뒤로 미룬다. 나중에 스키마가 바뀌거나
환산 가정이 바뀌어도 재조회가 필요 없다.

사용:
    export NEXAR_CLIENT_ID=...
    export NEXAR_CLIENT_SECRET=...

    # 1단계: 스키마 필드명 확인 (파트 한도 차감 없음)
    python scripts/nexar_snapshot.py --introspect SupPartOffer
    python scripts/nexar_snapshot.py --introspect SupPart

    # 2단계: 스모크 테스트 (파트 1개만 소모)
    python scripts/nexar_snapshot.py --smoke

    # 3단계: 실제 스냅샷 생성
    python scripts/nexar_snapshot.py --mpn-file mpns.txt --out data/nexar_snapshot.json

주의: 한도는 조회한 '파트 수'로 차감된다. 필드를 더 받는 것은 차감되지 않는다.
나중에 필요할 것 같은 필드는 --introspect 로 존재를 확인한 뒤 EXTRA_*_FIELDS 에 넣어
이번 조회에 함께 받아두는 편이 싸다 — 원본을 보존해도 '안 물어본 필드'는 없다.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

# 키는 .env 에서 읽는다 (프로젝트 공통 규약 — env.example 참고).
# 셸에서 export 해도 되지만, 도구가 호출마다 새 셸을 띄우면 남지 않는다.
# requests 없이 도는 인터프리터로도 --print-query 는 돌아야 해서 soft import.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN_URL = "https://identity.nexar.com/connect/token"
GRAPHQL_URL = "https://api.nexar.com/graphql"
TOKEN_CACHE = Path(".nexar_token.json")


# --------------------------------------------------------------------------
# 인증
def _requests():
    """
    requests 는 이 스크립트에서만 쓴다 — 앱은 스냅샷 파일만 읽으므로 필요 없다.
    프로젝트 의존성(uv로 생성된 requirements.txt)을 건드리지 않으려고 여기서 늦게 부른다.
    덕분에 --print-query 같은 오프라인 모드는 설치 없이도 돈다.
    """
    try:
        import requests
    except ModuleNotFoundError:
        sys.exit("이 명령은 requests 가 필요합니다:  pip install requests")
    return requests


# --------------------------------------------------------------------------
def get_token() -> str:
    """토큰을 발급받아 로컬에 캐시한다. 유효기간 24시간."""
    if TOKEN_CACHE.exists():
        cached = json.loads(TOKEN_CACHE.read_text())
        if cached.get("expires_at", 0) > time.time() + 300:
            return cached["access_token"]

    cid = os.environ.get("NEXAR_CLIENT_ID")
    secret = os.environ.get("NEXAR_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit(
            "NEXAR_CLIENT_ID / NEXAR_CLIENT_SECRET 가 없습니다.\n"
            "저장소 루트의 .env 에 아래 두 줄을 넣으세요 (.env 는 gitignore 됩니다):\n"
            "  NEXAR_CLIENT_ID=...\n"
            "  NEXAR_CLIENT_SECRET=..."
        )

    resp = _requests().post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": secret,
            "scope": "supply.domain",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    TOKEN_CACHE.write_text(
        json.dumps(
            {
                "access_token": payload["access_token"],
                "expires_at": time.time() + payload.get("expires_in", 86400),
            }
        )
    )
    TOKEN_CACHE.chmod(0o600)
    return payload["access_token"]


def gql(query: str, variables: dict | None = None) -> dict:
    # 토큰을 먼저 확보한다. 인자 안에서 부르면 _requests() 가 먼저 평가돼서
    # 정작 설정이 안 된 키 대신 라이브러리 안내가 뜬다.
    token = get_token()
    resp = _requests().post(
        GRAPHQL_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        # GraphQL은 필드명이 하나만 틀려도 쿼리 전체가 실패한다.
        # --introspect로 실제 필드명을 확인할 것.
        sys.exit("GraphQL 오류:\n" + json.dumps(body["errors"], indent=2, ensure_ascii=False))
    return body["data"]


# --------------------------------------------------------------------------
# 1단계: 필드명 확인
# --------------------------------------------------------------------------
INTROSPECT = """
query($name: String!) {
  __type(name: $name) {
    name
    fields { name description type { name kind ofType { name kind } } }
  }
}
"""


def introspect(type_name: str) -> None:
    data = gql(INTROSPECT, {"name": type_name})
    t = data.get("__type")
    if not t:
        sys.exit(f"'{type_name}' 타입을 찾을 수 없습니다. Voyager에서 정확한 이름을 확인하세요:\n"
                 "  https://api.nexar.com/ui/voyager")
    print(f"=== {t['name']}")
    for f in t["fields"]:
        ft = f["type"]
        tname = ft.get("name") or (ft.get("ofType") or {}).get("name") or ft["kind"]
        print(f"  {f['name']:24s} {tname}")


# --------------------------------------------------------------------------
# 2·3단계: 조회
# --------------------------------------------------------------------------
SMOKE = """
query { supSearchMpn(q: "LM358", limit: 3) { hits results { part { mpn manufacturer { name } } } } }
"""

# --introspect SupPartOffer -> SupOffer 로 확인한 실제 이름. 스냅샷의 provenance 에
# 기록되어 로더가 이 이름을 따라간다(app/snapshot_catalog.py).
MOQ_FIELD = "moq"

# 한도는 조회한 '파트 수'로만 깎이고 필드 수로는 깎이지 않는다. 원본을 보존해도
# '안 물어본 필드'는 없으므로, 나중에 쓸 법한 것은 이번 한 번에 같이 받는다.
# 아래는 전부 --introspect 로 존재를 확인한 이름이다. 확인하지 않은 이름을 넣으면
# 쿼리 전체가 실패한다.
EXTRA_PART_FIELDS: list[str] = [
    "id",
    "name",
    "genericMpn",
    "octopartUrl",                 # 출처 링크 — 실데이터임을 화면에서 보일 때
    "estimatedFactoryLeadDays",
    "totalAvail",                  # 전 유통사 재고 합
    "avgAvail",
    "akaMpns",                     # 대체 표기 MPN
    "category { name path }",
    "bestDatasheet { name url }",  # 사양 근거 문서
    # 시장가 기준선. 협상에서 "이 값이 적정한가"의 근거가 된다.
    "medianPrice1000 { quantity price currency convertedPrice convertedCurrency }",
    # 사양 매칭(negotiate.py 의 _spec_matches)을 shortDescription 문자열 대신
    # 구조화된 값으로 할 여지를 남긴다.
    "specs { attribute { name shortname group } value displayValue units }",
]

EXTRA_SELLER_FIELDS: list[str] = [
    "country",
    "isBroker",                    # 브로커는 정품 유통과 신뢰도가 다르다
    "isRfq",                       # 견적 필요 여부
]

EXTRA_OFFER_FIELDS: list[str] = [
    "id",
    "sku",                         # 유통사 품번 — 발주 단계에서 필요
    "updated",                     # 데이터 신선도
    "eligibleRegion",
    "onOrderQuantity",             # 입고 예정 수량
    "factoryPackQuantity",
    "orderMultiple",               # 주문 배수 제약 — MOQ 와 별개로 실재하는 조건
    "multipackQuantity",
    "isCustomPricing",             # 견적 전용이면 표시가가 의미를 잃는다
]

# supMultiMatch 는 currency 인자를 받는다. 넘기면 각 가격에 convertedPrice/
# conversionRate 가 붙는다 — 환산을 우리가 가정하지 않고 API 값을 쓸 수 있다.
# country 는 기본적으로 넘기지 않는다. 판매자 구성을 좁힐 수 있는데, 이 프로젝트는
# 한 품목에 판매자가 2곳 이상 붙어야 협상 후보가 생기기 때문이다.
MULTI_MATCH = """
query($queries: [SupPartMatchQuery!]!%(argdecl)s) {
  supMultiMatch(queries: $queries%(argpass)s) {
    reference
    hits
    parts {
      mpn
      shortDescription
      manufacturer { name }
%(part_extra)s
      sellers {
        company { id name homepageUrl isVerified }
        isAuthorized
%(seller_extra)s
        offers {
          clickUrl
          inventoryLevel
          factoryLeadDays
          packaging
%(moq)s
%(offer_extra)s
          prices {
            quantity
            price
            currency
            convertedPrice
            convertedCurrency
            conversionRate
          }
        }
      }
    }
  }
}
"""


def _block(fields: list[str], indent: int) -> str:
    """필드 목록을 쿼리 본문에 끼울 들여쓴 블록으로. 비어 있으면 빈 줄도 남기지 않는다."""
    return "\n".join(" " * indent + f for f in fields if f)


def build_query(currency: str | None = None, country: str | None = None) -> str:
    """
    비워둔 확장 슬롯이 빈 줄로 남지 않게 지운다. 이 쿼리는 스냅샷의 provenance 에
    그대로 저장되므로, 나중에 읽는 사람이 보기 좋아야 한다.
    """
    decl, passed = "", ""
    if currency:
        decl += ", $currency: String"
        passed += ", currency: $currency"
    if country:
        decl += ", $country: String"
        passed += ", country: $country"

    filled = MULTI_MATCH % {
        "argdecl": decl,
        "argpass": passed,
        "part_extra": _block(EXTRA_PART_FIELDS, 6),
        "seller_extra": _block(EXTRA_SELLER_FIELDS, 8),
        "moq": _block([MOQ_FIELD], 10),
        "offer_extra": _block(EXTRA_OFFER_FIELDS, 10),
    }
    return "\n".join(line for line in filled.splitlines() if line.strip()) + "\n"


def fetch(mpns: list[str], currency: str | None = None,
          country: str | None = None) -> tuple[str, dict, dict]:
    """(보낸 쿼리, 보낸 변수, 받은 응답 원본)을 그대로 돌려준다."""
    query = build_query(currency, country)
    variables: dict = {"queries": [{"mpn": m, "limit": 1, "reference": m} for m in mpns]}
    if currency:
        variables["currency"] = currency
    if country:
        variables["country"] = country
    return query, variables, gql(query, variables)


# --------------------------------------------------------------------------
# 요약 — 원본을 읽기만 한다. 파일에는 쓰지 않는다.
# --------------------------------------------------------------------------
def summarize(response: dict) -> None:
    """
    조회 직후 MPN 목록의 품질을 눈으로 확인하기 위한 것.
    여기서 계산한 값은 어디에도 저장하지 않는다 — 해석은 로더의 몫이다.
    """
    sellers_by_item: dict[str, set[str]] = {}
    counts = Counter()

    for match in response.get("supMultiMatch") or []:
        for part in match.get("parts") or []:
            mpn = part.get("mpn")
            counts["parts"] += 1
            for seller in part.get("sellers") or []:
                company = (seller.get("company") or {}).get("name")
                for offer in seller.get("offers") or []:
                    counts["offers"] += 1
                    if any(p.get("price") is not None for p in offer.get("prices") or []):
                        counts["priced"] += 1
                        sellers_by_item.setdefault(mpn, set()).add(company)

    print(f"파트 {counts['parts']}개 / 오퍼 {counts['offers']}건 "
          f"(가격 있는 오퍼 {counts['priced']}건) / 품목 {len(sellers_by_item)}개")

    thin = sorted(i for i, s in sellers_by_item.items() if len(s) < 2)
    if thin:
        print(f"경고: 판매자가 1곳뿐인 품목 {len(thin)}개 — 협상 후보가 안 생깁니다: {thin[:5]}")


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--introspect", metavar="TYPE", help="스키마 필드명 출력 (예: SupPartOffer)")
    ap.add_argument("--smoke", action="store_true", help="문서 예제 쿼리로 연결 확인")
    ap.add_argument("--mpn-file", help="MPN 목록 파일 (한 줄에 하나, # 주석 허용)")
    ap.add_argument("--out", default="data/nexar_snapshot.json")
    ap.add_argument("--print-query", action="store_true",
                    help="조회 없이 보낼 쿼리만 출력 (한도 차감 없음)")
    ap.add_argument("--currency", default="KRW",
                    help="가격에 붙일 환산 통화. 빈 문자열이면 환산을 요청하지 않는다")
    ap.add_argument("--country", default="",
                    help="사용자 국가. 판매자 구성을 좁힐 수 있어 기본은 미지정")
    args = ap.parse_args()

    if args.print_query:
        print(build_query(args.currency, args.country))
        return

    if args.introspect:
        introspect(args.introspect)
        return

    if args.smoke:
        print(json.dumps(gql(SMOKE), indent=2, ensure_ascii=False))
        return

    if not args.mpn_file:
        ap.error("--mpn-file 이 필요합니다 (또는 --introspect / --smoke / --print-query)")

    lines = Path(args.mpn_file).read_text(encoding="utf-8").splitlines()
    mpns = list(dict.fromkeys(  # 순서 보존 중복 제거 — 한도 낭비 방지
        s.split("#")[0].strip() for s in lines if s.split("#")[0].strip()
    ))
    print(f"조회 대상 {len(mpns)}개 MPN (중복 제거 후)")

    query, variables, response = fetch(mpns, args.currency, args.country)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                # 쿼리 전문을 같이 남긴다. GraphQL 응답은 보낸 쿼리에 대한 투영이라,
                # 쿼리를 모르면 '없는 필드'인지 '안 물어본 필드'인지 구분할 수 없다.
                "provenance": {
                    "endpoint": GRAPHQL_URL,
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "query": query,
                    "variables": variables,
                    "moq_field": MOQ_FIELD,
                    "note": "API 응답 원본. 합성값 없음. 해석은 app/snapshot_catalog.py 가 한다.",
                },
                "response": response,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"{out} 저장 — 응답 원본 그대로")
    summarize(response)


if __name__ == "__main__":
    main()
