"""
납품 가능성 검증 — "재고 10대인데 100대 가능하다"를 룰로 막는 자리

`P0_확정안_v3` §2가 지목한 그 검증이다. 문서의 표현대로
**LLM 없이 P0에 넣을 수 있는 유일한 검증**이고, 여기 쓰이는 숫자는 전부 카탈로그
스냅샷에서 온다 — 재고(inventoryLevel)·최소주문량(moq)·공장 리드타임 전부 API 원본이다.

영업 담당자가 고객에게 답하려면 "왜 안 되는지"가 필요하다 — 그게 `reasons` / `materials` 다.
"""

from __future__ import annotations

import math
import os

from . import snapshot_catalog

# 부족분을 조달할 때 협상에 넘길 상한가 = 적격 공급처 최저가 × (1 + 이 값)
PROCURE_CAP_MARGIN = float(os.getenv("PROCURE_CAP_MARGIN", "0.05"))


def _vendor_offers(code: str, qty: float) -> list[dict]:
    """
    조달 후보. 유통사 카탈로그에서 오고, 수량구간이 있어서 조달 수량에 맞는 단가가 들어온다.
    supply_qty 는 그 판매자가 실제로 댈 수 있는 수량이다 — 이것 때문에
    "그만큼 댈 수 있는 판매자가 없습니다"라는 판정이 나올 수 있다.
    """
    return [{"vendor": s.seller_id, "price": s.offer_price,
             "lead_time_days": s.lead_time_days, "min_qty": s.min_qty,
             "supply_qty": s.qty}
            for s in snapshot_catalog.sellers_for(code, max(1, int(qty)))]


def _product(product_code: str) -> dict:
    """
    품목과 '우리' 재고.

    스냅샷에는 우리 재고가 없다 — 외부 유통사의 공급 데이터뿐이다. 그래서
    `on_hand` 는 항상 0이고, 요청 수량 전량을 조달로 채운다. 상수를 그대로 두지
    않고 필드로 남기는 이유는, 실제 재고 출처가 생기면 여기 한 곳만 바뀌기 때문이다.
    """
    item = next((i for i in snapshot_catalog.list_items()
                 if i["code"] == product_code), None)
    if item is None:
        raise ValueError(f"스냅샷에 없는 품목입니다: {product_code!r}")
    return {"id": None, "code": item["code"], "name": item["name"],
            "spec": item["spec"], "on_hand": 0.0}


def _material_plan(code: str, required: float, on_hand: float) -> dict:
    """품목 1종의 부족분과 조달 조건. 조달할 게 없으면 candidates는 빈 목록."""
    shortage = max(0.0, required - on_hand)
    plan = {
        "code": code,
        "required": required,
        "on_hand": on_hand,
        "shortage": shortage,
        "procure_qty": 0.0,
        "overbuy": 0.0,
        "candidates": [],
        "best_lead_days": 0,
        "best_price": None,
        "blocked": False,
        "note": "",
        # 한 판매자가 댈 수 있는 최대 수량. 조달할 게 없으면 None.
        "supply_cap": None,
    }
    if shortage <= 0:
        return plan

    offers = _vendor_offers(code, shortage)
    if not offers:
        plan["blocked"] = True
        plan["note"] = "스냅샷에 이 품목의 판매자가 없습니다."
        return plan

    # 부족분이 최소주문량보다 적어도 조달은 된다 — 초과 발주하면 그만이다.
    # 그래서 실제 발주량은 '부족분'과 '가장 낮은 MOQ' 중 큰 값이고,
    # 이 수량이면 적어도 한 곳은 협상 스크리닝을 통과한다.
    caps = [o["supply_qty"] for o in offers
            if o["supply_qty"] is not None and o["supply_qty"] >= o["min_qty"]]
    if caps:
        plan["supply_cap"] = float(max(caps))

    min_moq = min(o["min_qty"] for o in offers)
    procure_qty = max(shortage, float(min_moq))
    # 최소주문량을 넘겨야 하고, 그 수량을 실제로 댈 수 있어야 한다.
    # 재고를 안 보면 협상 스크리닝이 전원 탈락시킬 물량을 "가능"이라고 답하게 된다 —
    # 이 모듈이 막으려는 바로 그 답이다.
    qualified = [o for o in offers
                 if procure_qty >= o["min_qty"]
                 and (o["supply_qty"] is None or o["supply_qty"] >= procure_qty)]
    if not qualified:
        plan["procure_qty"] = procure_qty
        plan["overbuy"] = procure_qty - shortage
        plan["blocked"] = True
        best = max((o["supply_qty"] for o in offers if o["supply_qty"] is not None),
                   default=None)
        note = f"{procure_qty:,.0f}개를 댈 수 있는 판매자가 없습니다"
        if best is not None:
            note += f" (한 곳의 최대 보유량 {best:,.0f}개)"
        plan["note"] = note + "."
        return plan

    plan["procure_qty"] = procure_qty
    plan["overbuy"] = procure_qty - shortage
    plan["candidates"] = qualified
    plan["best_lead_days"] = min(o["lead_time_days"] for o in qualified)
    plan["best_price"] = min(o["price"] for o in qualified)
    if plan["overbuy"] > 0:
        plan["note"] = (
            f"최소주문량 때문에 {procure_qty:,.0f}개를 발주해야 합니다 "
            f"({plan['overbuy']:,.0f}개 초과 구매)")
    return plan


def check(product_code: str, qty: float, due_days: int) -> dict:
    """
    "이 수량을 이 납기 안에 댈 수 있는가"를 판정하고 근거를 함께 돌려준다.

    소요일은 조달 납기 하나다. 우리가 만드는 게 아니라 유통사에서 사 오는 구조라
    제조 리드타임·생산일이 없다.
    """
    product = _product(product_code)
    on_hand = product["on_hand"]
    shortfall = max(0.0, qty - on_hand)
    materials = []
    if shortfall > 0:
        materials.append(_material_plan(product_code, qty, on_hand))

    procurement_days = max([m["best_lead_days"] for m in materials], default=0)
    blocked = [m for m in materials if m["blocked"]]
    total_days = procurement_days

    feasible = (not blocked) and total_days <= due_days

    # 납기 안에 댈 수 있는 최대 수량 — 안 된다고만 하지 않고 대안을 준다.
    # 조달 납기는 요청 수량 기준으로 구한 값을 그대로 쓴다(수량이 줄면 MOQ 때문에
    # 조달이 오히려 막힐 수 있어서, 이 값은 낙관적인 상한이다).
    max_feasible_qty = qty if procurement_days <= due_days else on_hand
    # 다만 아무도 그만큼 못 대면 거기까지다. 이걸 빼면 "댈 수 있는 판매자가
    # 없습니다" 라고 해놓고 "최대 요청 수량까지 가능합니다" 라고 답하게 된다.
    caps = [m["supply_cap"] for m in materials if m["supply_cap"] is not None]
    if caps:
        max_feasible_qty = min(max_feasible_qty, on_hand + min(caps))

    reasons = []
    reasons.append(f"재고 {on_hand:,.0f}개 → {shortfall:,.0f}개를 조달해야 합니다.")
    for m in materials:
        if m["blocked"]:
            reasons.append(f"[{m['code']}] {m['note']}")
        elif m["shortage"] > 0:
            extra = f" — {m['note']}" if m["note"] else ""
            reasons.append(
                f"[{m['code']}] 소요 {m['required']:,.0f} / 재고 {m['on_hand']:,.0f} → "
                f"{m['shortage']:,.0f}개 부족, 최단 조달 {m['best_lead_days']}일{extra}")
        else:
            reasons.append(
                f"[{m['code']}] 소요 {m['required']:,.0f} / 재고 {m['on_hand']:,.0f} → 충분")
    if shortfall > 0:
        reasons.append(f"소요일 = 조달 {procurement_days}일 (요구 납기 {due_days}일)")
    if not feasible:
        reasons.append(
            "→ 이 납기 안에는 한 개도 댈 수 없습니다 (조달 납기만으로 납기를 다 씁니다)."
            if max_feasible_qty <= 0
            else f"→ 납기 내 최대 {max_feasible_qty:,.0f}개까지 가능합니다.")

    return {
        "product": product,
        "requested_qty": qty,
        "due_days": due_days,
        "on_hand": on_hand,
        "shortfall": shortfall,
        "procurement_days": procurement_days,
        "total_days": total_days,
        "feasible": feasible,
        "max_feasible_qty": max_feasible_qty,
        "materials": materials,
        "reasons": reasons,
    }


def procurement_requests(result: dict) -> list[dict]:
    """
    검증 결과에서 '부족분 조달 요청' 목록을 뽑는다.
    이걸 그대로 협상(run_negotiation)에 넘기면 검증에서 협상까지 이어진다.
    """
    out = []
    for m in result["materials"]:
        if m["shortage"] <= 0 or m["blocked"]:
            continue
        cap = int(round(m["best_price"] * (1 + PROCURE_CAP_MARGIN)))
        out.append({
            "item": m["code"],
            "qty": int(math.ceil(m["procure_qty"])),
            "cap_price": cap,
            "max_lead_time_days": result["due_days"],
        })
    return out
