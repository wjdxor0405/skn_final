"""
L2 — 에이전트 판단 로직
규칙 기반(RuleBased*)과 LLM 기반(llm_agents.py)이 같은 인터페이스(SellerAgentPort/
BuyerAgentPort)를 만족하도록 만들어서, negotiate.py는 어느 쪽을 쓰는지 몰라도 되게 한다.
"""

from __future__ import annotations
from typing import Protocol
from .schemas import SellerRegister, BuyerRequest


class SellerAgentPort(Protocol):
    def decide(
        self, offer: SellerRegister, round_no: int, last_reject_price: int | None
    ) -> tuple[int, str]:
        """
        (제시가, 협상 메시지)를 반환.

        **바이어의 상한가를 인자로 받지 않는다.** 상대의 지불의사 최대치를 알면
        거기 붙여 부르면 그만이라 협상이 성립하지 않는다. 셀러가 아는 것은
        자기 제시가·최저수용가와, 자기가 제시했다가 거절당한 이력뿐이다.
        """
        ...


class BuyerAgentPort(Protocol):
    def decide(self, request: BuyerRequest, offer_price: int, seller_message: str) -> tuple[bool, str]:
        """(수락 여부, 협상 메시지)를 반환."""
        ...


def _seller_decide(offer: SellerRegister, round_no: int, buyer_last_reject_price: int | None) -> int:
    """
    셀러 규칙:
    - 1라운드: 등록된 제시가 그대로 제시
    - 거절당하면: 제시가와 최저수용가의 중간값으로 낮춰서 재제시 (최저수용가 밑으로는 절대 안 내려감)
    """
    if round_no == 1 or buyer_last_reject_price is None:
        return offer.offer_price
    candidate = (offer.offer_price + offer.floor_price) // 2
    return max(candidate, offer.floor_price)


def _buyer_decide(request: BuyerRequest, offer_price: int) -> bool:
    """바이어 규칙: 제시가 <= 상한가 이면 ACCEPT 가능."""
    return offer_price <= request.cap_price


class RuleBasedSellerAgent:
    """LLM 없이 if 조건문으로만 응답하는 1차 구현. 배관 검증용."""

    def decide(
        self, offer: SellerRegister, round_no: int, last_reject_price: int | None
    ) -> tuple[int, str]:
        price = _seller_decide(offer, round_no, last_reject_price)
        if round_no == 1:
            message = f"{price}원에 제안합니다."
        else:
            message = f"{price}원으로 조정하여 다시 제안합니다."
        return price, message


class RuleBasedBuyerAgent:
    def decide(self, request: BuyerRequest, offer_price: int, seller_message: str) -> tuple[bool, str]:
        accept = _buyer_decide(request, offer_price)
        if accept:
            message = "제안을 수락합니다."
        else:
            # 상한가 액수는 밝히지 않는다 — 이 메시지는 셀러에게 가는 것이고,
            # 정확한 상한을 알려주면 다음 제안이 거기 붙는다
            message = "제시가가 예산을 초과하여 거절합니다."
        return accept, message
