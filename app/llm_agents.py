"""
OpenAI API 기반 셀러/바이어 에이전트.
agents.py의 RuleBasedSellerAgent/RuleBasedBuyerAgent와 정확히 같은 인터페이스
(SellerAgentPort/BuyerAgentPort)를 구현해서, negotiate.py는 이 파일이
있는지조차 몰라도 되게 만든다 — NEGOTIATOR_MODE 환경변수로만 전환된다.

프롬프트는 `agent_prompts.py` 에 있다 — Strands 어댑터(`strands_agents.py`)와
같은 문장을 써야 정보 은닉 규약이 한쪽만 고쳐지는 일이 없다.
"""

from __future__ import annotations
import json
import os
from openai import OpenAI

from .schemas import SellerRegister, BuyerRequest
from .agent_prompts import seller_prompt, buyer_prompt

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
        prompt = seller_prompt(offer, round_no, last_reject_price)

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
        prompt = buyer_prompt(request, offer_price, seller_message)

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
