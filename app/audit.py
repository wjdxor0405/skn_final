"""
감사자 (Auditor)
LLM은 실수하거나(할루시네이션) 고의로 유리하게 판단할 수 있다.
그래서 LLM의 출력을 그대로 믿지 않고, 여기서 규칙(floor_price/cap_price)과
대조해서 어긴 경우에만 개입·보정한다. 규칙을 안 어겼으면 아무 것도 안 하고
그대로 통과시킨다 — 감사자가 매번 작동하는 게 아니라 "필요할 때만" 작동한다.
"""

from __future__ import annotations


def audit_seller_offer(price: int, floor_price: int) -> tuple[int, str | None]:
    """
    셀러 LLM이 최저수용가 밑으로 가격을 부르면(할루시네이션·실수) 최저수용가로 보정.
    반환: (보정된 가격, 개입 사유 — 개입 없었으면 None)
    """
    if price < floor_price:
        return floor_price, f"감사자 개입: LLM 제시가({price}원)가 최저수용가({floor_price}원) 미만이라 최저수용가로 보정"
    return price, None


def audit_buyer_accept(accept: bool, price: int, cap_price: int) -> tuple[bool, str | None]:
    """
    바이어 LLM이 상한가 초과 가격인데 실수로 ACCEPT 판단을 내리면 REJECT로 정정.
    반환: (보정된 수락여부, 개입 사유 — 개입 없었으면 None)
    """
    if accept and price > cap_price:
        return False, f"감사자 개입: LLM이 승인 판단을 냈으나 가격({price}원)이 상한가({cap_price}원) 초과라 거절로 정정"
    return accept, None
