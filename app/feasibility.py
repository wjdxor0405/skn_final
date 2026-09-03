"""
납품 가능성 검증 — "재고 10대인데 100대 가능하다"를 룰로 막는 자리

`P0_확정안_v3` §2가 지목한 그 검증이다. 문서의 표현대로
**LLM 없이 P0에 넣을 수 있는 유일한 검증**이고, 여기 쓰이는 숫자는 전부 Odoo에서 온다:

    완제품 재고        product.product.qty_available
    자재 소요량        mrp.bom.line.product_qty
    자재 재고          product.product.qty_available
    자재 조달 납기     product.supplierinfo.delay  (최소주문량 반영)
    제조 리드타임      mrp.bom.produce_delay

Odoo에 없어서 설정으로 두는 값은 **일일 생산 캐파** 하나뿐이다(아래 주석 참고).

영업 담당자가 고객에게 답하려면 "왜 안 되는지"가 필요하다 — 그게 `reasons` / `materials` 다.
"""

from __future__ import annotations

import math
import os

from .odoo_client import odoo

# Odoo에는 "하루에 몇 개 만들 수 있는가"에 해당하는 단순한 필드가 없다.
# (mrp.workcenter + 라우팅으로 표현하지만 데모 범위를 크게 벗어난다)
# 도메인에 따라 크게 달라지는 값이다. 기본값은 GPU 서버랙 조립 기준 일 10대(주 50대).
CAPACITY_PER_DAY = float(os.getenv("PRODUCTION_CAPACITY_PER_DAY", "10"))

# 부족분을 조달할 때 협상에 넘길 상한가 = 적격 공급처 최저가 × (1 + 이 값)
PROCURE_CAP_MARGIN = float(os.getenv("PROCURE_CAP_MARGIN", "0.05"))


def _material_plan(code: str, required: float, on_hand: float) -> dict:
    """자재 1종의 부족분과 조달 조건. 조달할 게 없으면 candidates는 빈 목록."""
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
    }
    if shortage <= 0:
        return plan

    offers = odoo.vendor_offers(code)
    if not offers:
        plan["blocked"] = True
        plan["note"] = "Odoo에 등록된 공급처가 없습니다."
        return plan

    # 부족분이 최소주문량보다 적어도 조달은 된다 — 초과 발주하면 그만이다.
    # 그래서 실제 발주량은 '부족분'과 '가장 낮은 MOQ' 중 큰 값이고,
    # 이 수량이면 적어도 한 곳은 협상 스크리닝을 통과한다.
    min_moq = min(o["min_qty"] for o in offers)
    procure_qty = max(shortage, float(min_moq))
    qualified = [o for o in offers if procure_qty >= o["min_qty"]]

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

    조달과 생산은 **직렬**로 본다(자재가 와야 만들기 시작). 실제로는 일부 겹치지만,
    영업이 고객에게 약속하는 자리라 보수적으로 잡는 편이 맞다.
    """
    product = odoo.product(product_code)
    if product is None:
        raise ValueError(f"Odoo에 없는 품목입니다: {product_code!r}")

    on_hand = product["on_hand"]
    shortfall = max(0.0, qty - on_hand)

    bom = odoo.bom_for(product_code)
    is_manufactured = bom is not None
    materials = []

    if is_manufactured:
        # 만들어 파는 품목 — 자재를 조달해서 생산한다.
        if shortfall > 0:
            for line in bom["lines"]:
                materials.append(_material_plan(
                    line["code"], line["qty_per"] * shortfall, line["on_hand"]))
        mfg_lead_days = bom["produce_delay"]
        production_days = math.ceil(shortfall / CAPACITY_PER_DAY) if shortfall > 0 else 0
    else:
        # BOM이 없는 품목 = 사다 파는(또는 사서 쓰는) 품목. 생산 공정이 없으므로
        # 부족분을 그 품목 자체로 조달한다. 생산일·제조 리드타임은 0.
        if shortfall > 0:
            materials.append(_material_plan(product_code, qty, on_hand))
        mfg_lead_days = 0
        production_days = 0

    procurement_days = max([m["best_lead_days"] for m in materials], default=0)
    blocked = [m for m in materials if m["blocked"]]
    total_days = procurement_days + mfg_lead_days + production_days

    feasible = (not blocked) and total_days <= due_days

    # 납기 안에 댈 수 있는 최대 수량 — 안 된다고만 하지 않고 대안을 준다.
    # 조달 납기는 요청 수량 기준으로 구한 값을 그대로 쓴다(수량이 줄면 MOQ 때문에
    # 조달이 오히려 막힐 수 있어서, 이 값은 낙관적인 상한이다).
    if is_manufactured:
        spare_days = due_days - procurement_days - mfg_lead_days
        max_feasible_qty = on_hand + max(0.0, spare_days) * CAPACITY_PER_DAY
    else:
        # 조달만 하면 되므로, 납기가 조달 납기를 넘기면 요청 수량 전부 가능하고
        # 못 넘기면 지금 있는 재고까지만 가능하다.
        max_feasible_qty = qty if procurement_days <= due_days else on_hand

    reasons = []
    reasons.append(
        f"완제품 재고 {on_hand:,.0f}개 → {shortfall:,.0f}개를 새로 만들어야 합니다."
        if is_manufactured
        else f"재고 {on_hand:,.0f}개 → {shortfall:,.0f}개를 조달해야 합니다 (BOM이 없는 구매 품목).")
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
        reasons.append(
            f"소요일 = 조달 {procurement_days}일 + 제조 리드타임 {mfg_lead_days}일 + "
            f"생산 {production_days}일(일 {CAPACITY_PER_DAY:,.0f}개) = {total_days}일 "
            f"(요구 납기 {due_days}일)"
            if is_manufactured
            else f"소요일 = 조달 {procurement_days}일 (요구 납기 {due_days}일)")
    if not feasible:
        reasons.append(
            "→ 이 납기 안에는 한 개도 댈 수 없습니다 (조달·제조 리드타임만으로 납기를 다 씁니다)."
            if max_feasible_qty <= 0
            else f"→ 납기 내 최대 {max_feasible_qty:,.0f}개까지 가능합니다.")

    return {
        "product": product,
        "requested_qty": qty,
        "due_days": due_days,
        "on_hand": on_hand,
        "is_manufactured": is_manufactured,
        "shortfall": shortfall,
        "capacity_per_day": CAPACITY_PER_DAY,
        "procurement_days": procurement_days,
        "manufacturing_lead_days": mfg_lead_days,
        "production_days": production_days,
        "total_days": total_days,
        "feasible": feasible,
        "max_feasible_qty": max_feasible_qty,
        "materials": materials,
        "reasons": reasons,
    }


def procurement_requests(result: dict) -> list[dict]:
    """
    검증 결과에서 '부족분 조달 요청' 목록을 뽑는다.
    이걸 그대로 협상(run_negotiation)에 넘기면 3단계의 발주서 생성까지 이어진다.
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
