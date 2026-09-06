"""
Strands `@tool` — 이미 있는 룰 기반 함수를 에이전트가 직접 부를 수 있게 노출한다.

해커톤 심사 기준 1번이 *"a working, **non-trivial** implementation"* 을 묻는다.
어댑터만 바꾼 1단계(`strands_agents.py`)로는 낮게 받는다는 판단이
`docs/해커톤_요건_대조.md` §3 에 적혀 있고, 그 §3 이 지목한 함수가 여기 있다.

**새 로직이 없다.** 세 도구 전부 기존 함수를 감싸기만 한다 — 판정은 여전히 룰이
하고, 모델은 *무엇을 물어볼지*만 정한다. 그래야 "재고 10대인데 100대 가능"이
모델 쪽에서 다시 생길 자리가 없다.

    list_catalog_items()                    store.list_items()
    find_sellers(item, qty)                 store.sellers_for()      ← floor_price 제거
    check_delivery(item, qty, due_days)     feasibility.check()

[왜 협상 에이전트에게는 도구를 주지 않는가]
`negotiate.py` 의 셀러/바이어 에이전트는 이 도구들을 **받지 않는다.** 정보 은닉이
깨지기 때문이다:

- 셀러가 `find_sellers` 를 부르면 경쟁 셀러의 제시가를 본다 — 거기 맞춰 부르면 그만
- 바이어가 부르면 셀러의 `floor_price` 를 본다 — 최저 수용가를 알면 협상이 아니다

두 번째는 도구를 주지 않더라도 막아야 해서, `find_sellers` 자체가 `floor_price`
를 **아예 반환하지 않는다**(`_public_offer`). 이 어시스턴트는 바이어를 돕는
쪽이고, 협상 라운드 밖에서만 돈다.
"""

from __future__ import annotations

from strands import tool

from . import feasibility
from .store import store


def _public_offer(s) -> dict:
    """
    판매자 1건에서 **밖에 보여도 되는 것만** 골라낸다.

    `SellerRegister` 에는 `floor_price`(최저 수용가)가 들어 있다. 어떤 공개
    데이터에도 없는 값이고(그래서 우리가 합성한다), 상대가 알면 거기까지 깎으면
    그만이라 협상이 성립하지 않는다. 도구가 dict 로 바꿔 내보내는 이 자리가
    **모델에게 나가는 유일한 경로**이므로 여기서 잘라낸다.
    """
    return {
        "seller_id": s.seller_id,
        "offer_price": s.offer_price,
        "available_qty": s.qty,
        "lead_time_days": s.lead_time_days,
        "min_order_qty": s.min_qty,
        "spec": s.spec,
    }


@tool
def list_catalog_items() -> list[dict]:
    """
    거래 가능한 품목 목록을 돌려준다.

    사용자가 말한 품목명이 카탈로그에 실제로 있는지 확인할 때 먼저 부른다.
    """
    return [
        {"code": i["code"], "name": i["name"], "spec": i["spec"]}
        for i in store.list_items()
    ]


@tool
def find_sellers(item: str, qty: int) -> list[dict]:
    """
    한 품목에 대해 조건을 댈 수 있는 판매자와 그 조건을 돌려준다.

    수량에 맞는 가격구간이 적용되므로 `qty` 가 바뀌면 단가와 순위가 바뀐다.
    최저 수용가는 돌려주지 않는다 — 협상 대상이 되는 값이다.

    Args:
        item: 품목 코드 (예: "STM32F103C8T6"). `list_catalog_items` 로 확인할 것
        qty: 요청 수량. 가격구간 선택에 쓰인다
    """
    return [_public_offer(s) for s in store.sellers_for(item, qty)]


@tool
def check_delivery(item: str, qty: int, due_days: int) -> dict:
    """
    "이 수량을 이 납기 안에 댈 수 있는가"를 룰로 판정하고 근거를 함께 돌려준다.

    가능/불가 판정과 그 이유, 그리고 불가일 때 **납기 내 최대 가능 수량**까지
    나온다. 수량을 줄여 다시 제안할 때 이 값을 쓴다.

    Args:
        item: 품목 코드
        qty: 요청 수량
        due_days: 요구 납기(일)
    """
    try:
        r = feasibility.check(item, qty, due_days)
    except ValueError as e:
        return {"error": str(e)}
    return {
        "feasible": r["feasible"],
        "requested_qty": r["requested_qty"],
        "max_feasible_qty": r["max_feasible_qty"],
        "procurement_days": r["procurement_days"],
        "reasons": r["reasons"],
    }


TOOLS = [list_catalog_items, find_sellers, check_delivery]
