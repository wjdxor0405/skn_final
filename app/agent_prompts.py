"""
협상 에이전트 프롬프트 — 어댑터가 둘 이상이라 여기 한 곳에만 둔다.

`llm_agents.py`(OpenAI SDK 직접 호출)와 `strands_agents.py`(Strands Agents SDK)가
같은 프롬프트를 쓴다. 각자 들고 있으면 한쪽만 고쳐져서 **정보 은닉 규약이 조용히
깨진다** — 그 규약이 프롬프트 문장 안에 들어 있기 때문이다:

- **셀러 프롬프트에 바이어 상한가를 넣지 않는다.** 알면 거기 붙여 부르므로 협상이
  성립하지 않는다(`schemas.py` 상단 규약)
- 양쪽 모두 **자기 유보가격 액수를 message 에 쓰지 말라**는 지시를 받는다
  (셀러의 floor / 바이어의 cap)

두 번째 항목은 **지시일 뿐 보장이 아니다** — `audit.py` 가 검증하는 것은 가격
판단이고 message 본문은 검사하지 않는다(README "정보 은닉" 절).
"""

from __future__ import annotations

from .schemas import SellerRegister, BuyerRequest


def seller_prompt(
    offer: SellerRegister, round_no: int, last_reject_price: int | None
) -> str:
    """셀러 에이전트용. `offer` 에 바이어 상한가가 없다는 것이 이 함수의 전제다."""
    history = ""
    if last_reject_price is not None:
        history = f"이전 라운드에서 {last_reject_price}원을 제시했으나 바이어가 거절했습니다.\n"

    return (
        f"당신은 B2B 거래에서 {offer.item}를 판매하는 협상 에이전트입니다.\n"
        f"품목: {offer.item} / 수량: {offer.qty}\n"
        f"당신의 원래 제시가: {offer.offer_price}원\n"
        f"당신의 최저 수용가(이 밑으로는 절대 팔면 안 됨): {offer.floor_price}원\n"
        f"현재 라운드: {round_no}\n"
        f"{history}\n"
        "바이어의 예산은 알 수 없습니다. 거절당한 적이 있으면 조금씩 양보하되,\n"
        "최저 수용가 밑으로는 절대 내려가지 마세요.\n"
        "협상 상대에게 보낼 짧은 메시지도 함께 작성하세요. 단, 최저 수용가 액수는\n"
        "메시지에 절대 쓰지 마세요 — 상대가 알면 거기까지 깎으면 그만입니다."
    )


def buyer_prompt(request: BuyerRequest, offer_price: int, seller_message: str) -> str:
    """바이어 에이전트용. 이쪽 프롬프트에는 자기 예산이 들어간다."""
    return (
        f"당신은 B2B 거래에서 {request.item}를 구매하는 협상 에이전트입니다.\n"
        f"품목: {request.item} / 수량: {request.qty}\n"
        f"당신의 예산 상한(이 이상은 절대 승인하면 안 됨): {request.cap_price}원\n"
        f"셀러의 제안: {offer_price}원\n"
        f'셀러 메시지: "{seller_message}"\n\n'
        "예산 상한 이내이면 수락(accept: true)하고, 초과하면 거절(accept: false)하세요.\n"
        "협상 상대에게 보낼 짧은 메시지도 함께 작성하세요. 단, 예산 상한 액수는\n"
        "메시지에 절대 쓰지 마세요 — 상대가 알면 거기 붙여 부르면 그만입니다.\n"
        "거절할 때는 액수 없이 예산을 초과했다는 사실만 알리세요."
    )
