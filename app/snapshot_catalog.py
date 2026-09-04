"""
CATALOG_SOURCE=snapshot — Nexar(Octopart) 스냅샷을 셀러 카탈로그로 읽는다.

[왜 이 파일이 따로 있나]
data/nexar_snapshot.json 은 API 응답 원본이다. 무료 플랜의 파트 한도가 조회한
'파트 수'로 깎이기 때문에 그 파일은 사실상 다시 못 뜬다. 그래서 거기엔 아무 해석도
굽지 않았다 — 통화 환산, 수량구간 선택, floor_price 합성, 누락값 정규화는 전부
여기서, 읽는 시점에 일어난다. 가정이 바뀌면 이 파일만 고치고 서버를 다시 띄우면 된다.
재조회는 필요 없다.

[합성값은 floor_price 하나뿐]
셀러의 '최저 수용가'는 어떤 공개 데이터에도 없다 — 공개되면 협상이 성립하지 않는
값이기 때문이다. 관측된 단가에 SNAPSHOT_FLOOR_RATIO 를 곱해 만든다.
단가·재고·납기·MOQ는 전부 API 원본이다.

[정수 스키마에 맞추는 일]
SellerRegister.offer_price/floor_price 는 int 다(schemas.py). 전자부품 단가는
대부분 1달러 미만의 실수라서 그대로는 검증을 통과하지 못한다. SNAPSHOT_FX 의 고정
환율로 원화 정수로 환산한다. 환율이 없는 통화나 1원 미만으로 뭉개지는 행은 버리고,
몇 건을 왜 버렸는지 diagnostics() 로 남긴다 — 조용히 사라지면 안 되는 값들이다.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .schemas import SellerRegister

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "data/nexar_snapshot.json")
# store.py 의 ODOO_FLOOR_RATIO 와 같은 관례 — 제시가의 이 비율을 최저 수용가로 가정한다.
SNAPSHOT_FLOOR_RATIO = float(os.getenv("SNAPSHOT_FLOOR_RATIO", "0.90"))
# 스키마가 정수라서 단가를 이 통화로 맞춘다.
SNAPSHOT_CURRENCY = os.getenv("SNAPSHOT_CURRENCY", "KRW").upper()
# 폴백 환율. "USD=1400,EUR=1520" 형식.
#
# 조회할 때 currency 를 넘겼으면 응답에 convertedPrice/conversionRate 가 들어 있다.
# 그건 API가 계산한 실제 환율이므로 우선 쓰고, 이 표는 환산값이 없는 행에만 쓴다.
# 어느 쪽도 안 되면 그 행은 버린다 — 틀린 환율로 넣는 것보다 낫다.
SNAPSHOT_FX = os.getenv("SNAPSHOT_FX", "USD=1400")


class SnapshotUnavailable(RuntimeError):
    """스냅샷 파일이 없거나 형식이 아닌 경우. API 계층에서 그대로 보여준다."""


# ── 원본 읽기 ────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _raw() -> dict:
    """프로세스당 1회만 읽는다. 스냅샷은 실행 중에 바뀌지 않는다."""
    path = Path(SNAPSHOT_PATH)
    if not path.is_absolute():
        path = _ROOT / path
    if not path.exists():
        raise SnapshotUnavailable(
            f"스냅샷이 없습니다: {path}\n"
            "scripts/nexar_snapshot.py 로 생성하거나 SNAPSHOT_PATH 를 확인하세요."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _matches(raw: dict) -> list[dict]:
    # provenance 로 감싼 형식이 정본이지만, 응답만 저장한 파일도 읽을 수 있게 둔다.
    response = raw.get("response", raw)
    return response.get("supMultiMatch") or []


def _moq_key(raw: dict) -> str:
    """
    MOQ 필드명은 조회할 때 정해졌다. 스냅샷이 자기가 뭘 물어봤는지 알고 있으므로
    로더가 이름을 따로 짐작하지 않는다 — provenance 를 남긴 이유가 이것이다.
    """
    return (raw.get("provenance") or {}).get("moq_field") or ""


@lru_cache(maxsize=1)
def _fx() -> dict[str, float]:
    table: dict[str, float] = {}
    for entry in SNAPSHOT_FX.split(","):
        code, _, rate = entry.strip().partition("=")
        if code.strip() and rate.strip():
            table[code.strip().upper()] = float(rate)
    return table


# ── 변환 ────────────────────────────────────────────────────────────────────
def _price_breaks(offer: dict) -> list[dict]:
    """수량 구간을 오름차순으로. 가격이 빠진 구간은 뺀다."""
    breaks = []
    for p in offer.get("prices") or []:
        price, quantity = p.get("price"), p.get("quantity")
        if price is None or quantity is None:
            continue
        breaks.append({
            "quantity": int(quantity),
            "price": float(price),
            "currency": (p.get("currency") or "").upper(),
            # 조회 시 currency 를 넘겼을 때만 채워진다.
            "converted_price": p.get("convertedPrice"),
            "converted_currency": (p.get("convertedCurrency") or "").upper(),
        })
    return sorted(breaks, key=lambda b: b["quantity"])


def pick_break(breaks: list[dict], qty: int | None) -> dict:
    """
    요청 수량에 해당하는 수량 구간. qty 를 모르면 최소수량 구간(=가장 비싼 단가)을 쓴다.
    자격이 없는 볼륨 할인가를 먼저 부르지 않는 쪽이 보수적이라 그렇게 잡았다.
    """
    if qty is None:
        return breaks[0]
    applicable = [b for b in breaks if b["quantity"] <= qty]
    return applicable[-1] if applicable else breaks[0]


def _unit_price(brk: dict) -> tuple[int | None, str]:
    """
    구간 단가를 목표 통화의 정수로. 출처를 함께 돌려준다 — 어느 값이 API 환산이고
    어느 값이 우리 가정인지 구분되지 않으면 스냅샷의 신뢰 경계가 흐려진다.
    """
    if brk["converted_currency"] == SNAPSHOT_CURRENCY and brk["converted_price"] is not None:
        return int(round(float(brk["converted_price"]))), "api"
    if brk["currency"] == SNAPSHOT_CURRENCY:
        return int(round(brk["price"])), "원본"
    rate = _fx().get(brk["currency"])
    if rate is None:
        return None, "환율없음"
    return int(round(brk["price"] * rate)), "폴백환율"


def _build(qty: int | None) -> tuple[list[SellerRegister], Counter, Counter]:
    raw = _raw()
    moq_key = _moq_key(raw)
    dropped: Counter = Counter()
    best: dict[tuple[str, str], SellerRegister] = {}
    # 카탈로그에 실제로 남은 행의 단가 출처만 센다. 중복·0원으로 버려진 것까지
    # 세면 "이 카탈로그의 단가 중 몇 개가 API 값인가"라는 질문에 답하지 못한다.
    origins: dict[tuple[str, str], str] = {}

    for match in _matches(raw):
        for part in match.get("parts") or []:
            item = part.get("mpn")
            if not item:
                dropped["mpn 없음"] += 1
                continue
            spec = part.get("shortDescription") or ""
            for seller in part.get("sellers") or []:
                company = (seller.get("company") or {}).get("name")
                if not company:
                    dropped["판매자명 없음"] += 1
                    continue
                for offer in seller.get("offers") or []:
                    breaks = _price_breaks(offer)
                    if not breaks:
                        dropped["가격 없는 오퍼"] += 1
                        continue
                    brk = pick_break(breaks, qty)
                    unit, origin = _unit_price(brk)
                    if unit is None:
                        dropped[f"환율 미정의({brk['currency'] or '통화없음'})"] += 1
                        continue
                    if unit < 1:
                        # 1원 미만으로 뭉개지면 협상 자체가 성립하지 않는다.
                        dropped["환산 단가 0원"] += 1
                        continue

                    moq = offer.get(moq_key) if moq_key else None
                    row = SellerRegister(
                        seller_id=company,
                        item=item,
                        # 재고는 원본에 있다(Odoo 모드와 달리 가정할 필요가 없다).
                        # 값이 없으면 0 — 스크리닝에서 걸러지는 편이 부풀리는 것보다 낫다.
                        qty=int(offer.get("inventoryLevel") or 0),
                        offer_price=unit,
                        # 유일한 합성값. 0원이 되면 협상 하한이 무너지므로 최소 1원.
                        floor_price=max(1, int(round(unit * SNAPSHOT_FLOOR_RATIO))),
                        spec=spec,
                        lead_time_days=int(offer.get("factoryLeadDays") or 0),
                        # 이 단가를 받으려면 해당 수량 구간 이상을 사야 한다.
                        # MOQ와 구간 수량 중 큰 쪽이 실제 최소주문량이다.
                        # (MOQ_FIELD 미확인이면 moq 는 None — 여기서 0으로 정규화된다.
                        #  스키마가 int 라서 None 을 그대로 넘기면 검증에서 터진다.)
                        min_qty=max(int(moq or 0), brk["quantity"]),
                    )
                    # 같은 판매자가 같은 품목에 여러 오퍼를 낸다(포장 단위 차이 등).
                    # 협상 상대가 중복으로 등장하면 로그가 읽히지 않으므로 최저가만 남긴다.
                    key = (company, item)
                    if key in best:
                        dropped["같은 판매자의 중복 오퍼"] += 1
                        if best[key].offer_price <= row.offer_price:
                            continue
                    best[key] = row
                    origins[key] = origin

    rows = sorted(best.values(), key=lambda s: (s.item, s.offer_price, s.seller_id))
    sources = Counter(origins.values())
    return rows, dropped, sources


@lru_cache(maxsize=8)
def _catalog_cached(qty: int | None) -> tuple[SellerRegister, ...]:
    rows, dropped, sources = _build(qty)
    log.info("스냅샷 카탈로그(qty=%s): %d행 · 단가출처 %s · 제외 %s",
             qty, len(rows), dict(sources), dict(dropped))
    return tuple(rows)


# ── store.py 가 쓰는 표면 ────────────────────────────────────────────────────
def sellers_for(item: str, qty: int | None = None) -> list[SellerRegister]:
    return [s for s in _catalog_cached(qty) if s.item == item]


def list_sellers(qty: int | None = None) -> list[SellerRegister]:
    return list(_catalog_cached(qty))


def list_items() -> list[dict]:
    """화면 드롭다운용. on_hand 는 '우리' 재고를 뜻하는데 스냅샷에는 없으므로 0."""
    out: dict[str, dict] = {}
    for s in _catalog_cached(None):
        out.setdefault(s.item, {"code": s.item, "name": s.item,
                                "spec": s.spec, "on_hand": 0.0})
    return sorted(out.values(), key=lambda d: d["code"])


def diagnostics(qty: int | None = None) -> dict:
    """무엇이 몇 건 빠졌는지. 스냅샷 품질을 눈으로 볼 때 쓴다."""
    rows, dropped, sources = _build(qty)
    items = {s.item for s in rows}
    return {
        "path": SNAPSHOT_PATH,
        "fetched_at": (_raw().get("provenance") or {}).get("fetched_at"),
        "moq_field": _moq_key(_raw()) or "(미확인 — MOQ 없이 동작)",
        "rows": len(rows),
        "sellers": len({s.seller_id for s in rows}),
        "items": len(items),
        "currency": SNAPSHOT_CURRENCY,
        # 어느 단가가 API 환산이고 어느 것이 우리 폴백 환율인지.
        "price_sources": dict(sources),
        "dropped": dict(dropped),
        # 판매자가 1곳뿐인 품목은 협상 후보가 안 생긴다.
        "single_seller_items": sorted(
            i for i in items if sum(1 for s in rows if s.item == i) < 2
        ),
    }
