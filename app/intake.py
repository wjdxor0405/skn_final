"""
고객 주문 접수 — 표준계약 범위인지 먼저 가르고, 그에 따라 다르게 처리한다

    발주 접수 ──▶ 표준계약 범위 내인가?
                    │
                    ├─ 예    기존 조건 그대로 자동 발주 (협상 0라운드)
                    └─ 아니오 AI 에이전트가 협상

이 분기가 이 프로젝트에서 "AI 에이전트가 왜 필요한가"에 답하는 자리다. 에이전트는
평소에는 나서지 않는다 — 이례적인 발주에만 붙는다. 반대로 말하면, 에이전트가 항상
돌아야 하는 설계였다면 그 자체가 과한 구조라는 지적을 피할 수 없다.

기준값(고객사별 분기 표준수량·허용배수)은 Odoo의 ir.config_parameter 에 있다.
`odoo_client.standard_contract()` 참고.
"""

from __future__ import annotations

from .feasibility import check as feasibility_check
from .feasibility import procurement_requests
from .negotiate import run_negotiation, run_standard_order
from .odoo_client import odoo
from .schemas import BuyerRequest
from .store import CentralStore


def classify(customer_ref: str, product_code: str, qty: float) -> dict:
    """
    이 발주가 표준계약 범위인지 판정한다. 반환 키: kind / reason / contract

    kind 는 STANDARD 또는 EXCEPTION. 표준계약이 아예 없는 고객(신규 거래처)은
    비교 기준이 없으므로 EXCEPTION 으로 본다 — 처음 거래하는 상대와 조건을
    다투는 것이 오히려 정상이다.
    """
    contract = odoo.standard_contract(customer_ref, product_code)
    if contract is None:
        return {
            "kind": "EXCEPTION",
            "reason": "표준계약이 없는 고객입니다 (신규 거래처). 기준이 없으므로 협상합니다.",
            "contract": None,
        }

    limit = contract["period_qty"] * contract["tolerance"]
    if qty <= limit:
        return {
            "kind": "STANDARD",
            "reason": (f"표준계약 {contract['period_qty']:,.0f}대 × 허용 {contract['tolerance']}"
                       f" = {limit:,.0f}대 이내의 {qty:,.0f}대 요청입니다. "
                       f"기존 조건으로 자동 처리합니다."),
            "contract": contract,
        }
    return {
        "kind": "EXCEPTION",
        "reason": (f"표준계약 {contract['period_qty']:,.0f}대 × 허용 {contract['tolerance']}"
                   f" = {limit:,.0f}대를 넘는 {qty:,.0f}대 요청입니다. 협상이 필요합니다."),
        "contract": contract,
    }


def _incumbent_offer(store: CentralStore, request: BuyerRequest) -> dict | None:
    """
    표준 경로에서 쓸 '기존 조건'을 찾는다. 반환 키: vendor / price / why

    ① 직전에 확정된 발주서의 공급처를 우선한다 — 계속 거래해 온 곳이다
    ② 그 공급처가 지금 조건(최소주문량·납기)을 못 맞추면 조건을 맞추는 최저가로 대체한다

    ②로 넘어갔다는 사실 자체가 근거로 남아야 하므로 why 에 적어 돌려준다.
    """
    eligible = [
        s for s in store.sellers_for(request.item)
        if request.qty >= (s.min_qty or 0)
        and s.lead_time_days <= request.max_lead_time_days
    ]
    if not eligible:
        return None

    last = odoo.last_purchase_for(request.item)
    if last:
        for s in eligible:
            if s.seller_id == last["vendor"]:
                return {"vendor": s.seller_id, "price": s.offer_price,
                        "why": "직전 발주와 같은 공급처의 현재 표준 단가"}

    cheapest = min(eligible, key=lambda s: s.offer_price)
    why = ("직전 발주 이력이 없어 조건을 만족하는 최저가 공급처를 적용"
           if not last else
           f"직전 공급처({last['vendor']})가 현재 조건을 못 맞춰 최저가 공급처로 대체")
    return {"vendor": cheapest.seller_id, "price": cheapest.offer_price, "why": why}


def run_intake(store: CentralStore, customer_ref: str, product_code: str,
               qty: float, due_days: int) -> dict:
    """
    고객 주문 하나를 끝까지 처리한다.

    납품 가능성 검증 → 부족 자재 산출 → (표준이면 즉시 발주 / 예외면 협상) → 발주서 초안.
    표준 경로여도 기존 조건으로 살 수 없으면 협상으로 되돌린다. 그 사실도 근거에 남긴다.
    """
    verdict = classify(customer_ref, product_code, qty)
    feasibility = feasibility_check(product_code, qty, due_days)

    deals = []
    for raw in procurement_requests(feasibility):
        request = BuyerRequest(**raw)
        route = verdict["kind"]

        if route == "STANDARD":
            pick = _incumbent_offer(store, request)
            if pick:
                note = f"표준계약 범위 내 자동 처리 — {pick['why']}"
                summary = run_standard_order(
                    store, request, pick["vendor"], pick["price"], note)
                deals.append({**summary, "route": "STANDARD", "route_note": note,
                              **_po_fields(store, summary["txid"])})
                continue
            route = "EXCEPTION_FALLBACK"   # 기존 조건으로는 못 산다 → 협상으로

        summary = run_negotiation(store, request)
        note = ("표준 범위지만 기존 조건으로 조달할 수 없어 협상으로 처리"
                if route == "EXCEPTION_FALLBACK" else "예외 발주 — 협상으로 처리")
        deals.append({**summary, "route": route, "route_note": note,
                      **_po_fields(store, summary["txid"])})

    return {
        "customer_ref": customer_ref,
        "verdict": verdict,
        "feasible": feasibility["feasible"],
        "reasons": feasibility["reasons"],
        "deals": deals,
    }


def _po_fields(store: CentralStore, txid: str) -> dict:
    deal = store.get_deal(txid) or {}
    return {k: deal.get(k) for k in ("po_name", "po_url", "po_error")}
