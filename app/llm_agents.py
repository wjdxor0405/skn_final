"""
OpenAI API 기반 셀러/바이어 에이전트.
agents.py의 RuleBasedSellerAgent/RuleBasedBuyerAgent와 정확히 같은 인터페이스
(SellerAgentPort/BuyerAgentPort)를 구현해서, negotiate.py는 이 파일이
있는지조차 몰라도 되게 만든다 — NEGOTIATOR_MODE 환경변수로만 전환된다.

**셀러 프롬프트에 바이어 상한가를 넣지 않는다** — 알면 거기 붙여 부르므로 협상이
성립하지 않는다(schemas.py 상단 규약). 바이어 프롬프트에는 자기 예산이 들어간다.
LLM 출력은 audit.py를 거쳐 규칙(floor/cap) 위반 여부를 검증하는데, 검증 대상은
**가격 판단뿐이고 message 본문은 검사하지 않는다** — 바이어 LLM이 자유 문장에
자기 예산 액수를 적어버리는 것은 아직 막지 못한다(README "정보 은닉" 절).
"""

from __future__ import annotations
import json
import os
from openai import OpenAI

from .schemas import SellerRegister, BuyerRequest

_client: OpenAI | None = None
# 사용할 OpenAI 모델. .env의 OPENAI_MODEL로 덮어쓸 수 있다(기본값: gpt-4o-mini).
# 협상 판단은 비교적 단순한 추론이라 저가 모델로 충분.
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY 환경변수가 설정되어 있지 않습니다. "
                "NEGOTIATOR_MODE=llm으로 쓰려면 이 키가 필요합니다."
            )
        _client = OpenAI(api_key=api_key)
    return _client


SELLER_SCHEMA = {
    "type": "object",
    "properties": {
        "price": {"type": "integer", "description": "이번 라운드에 제시할 단가"},
        "message": {"type": "string", "description": "협상 상대에게 보낼 짧은 메시지(한국어, 1문장)"},
    },
    "required": ["price", "message"],
    "additionalProperties": False,
}

BUYER_SCHEMA = {
    "type": "object",
    "properties": {
        "accept": {"type": "boolean", "description": "이 제시가를 수락할지 여부"},
        "message": {"type": "string", "description": "협상 상대에게 보낼 짧은 메시지(한국어, 1문장)"},
    },
    "required": ["accept", "message"],
    "additionalProperties": False,
}


class OpenAISellerAgent:
    def decide(
        self, offer: SellerRegister, round_no: int, last_reject_price: int | None
    ) -> tuple[int, str]:
        history = ""
        if last_reject_price is not None:
            history = f"이전 라운드에서 {last_reject_price}원을 제시했으나 바이어가 거절했습니다.\n"

        prompt = (
            f"당신은 B2B 거래에서 {offer.item}를 판매하는 협상 에이전트입니다.\n"
            f"품목: {offer.item} / 수량: {offer.qty}\n"
            f"당신의 원래 제시가: {offer.offer_price}원\n"
            f"당신의 최저 수용가(이 밑으로는 절대 팔면 안 됨): {offer.floor_price}원\n"
            f"현재 라운드: {round_no}\n"
            f"{history}\n"
            "바이어의 예산은 알 수 없습니다. 거절당한 적이 있으면 조금씩 양보하되,\n"
            "최저 수용가 밑으로는 절대 내려가지 마세요.\n"
            "협상 상대에게 보낼 짧은 메시지도 함께 작성하세요."
        )

        resp = _get_client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "seller_offer", "schema": SELLER_SCHEMA, "strict": True},
            },
        )
        data = json.loads(resp.choices[0].message.content)
        return int(data["price"]), str(data["message"])


class OpenAIBuyerAgent:
    def decide(self, request: BuyerRequest, offer_price: int, seller_message: str) -> tuple[bool, str]:
        prompt = (
            f"당신은 B2B 거래에서 {request.item}를 구매하는 협상 에이전트입니다.\n"
            f"품목: {request.item} / 수량: {request.qty}\n"
            f"당신의 예산 상한(이 이상은 절대 승인하면 안 됨): {request.cap_price}원\n"
            f"셀러의 제안: {offer_price}원\n"
            f'셀러 메시지: "{seller_message}"\n\n'
            "예산 상한 이내이면 수락(accept: true)하고, 초과하면 거절(accept: false)하세요.\n"
            "협상 상대에게 보낼 짧은 메시지도 함께 작성하세요."
        )

        resp = _get_client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "buyer_decision", "schema": BUYER_SCHEMA, "strict": True},
            },
        )
        data = json.loads(resp.choices[0].message.content)
        return bool(data["accept"]), str(data["message"])
