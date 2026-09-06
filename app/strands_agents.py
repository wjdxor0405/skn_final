"""
Strands Agents SDK 기반 셀러/바이어 에이전트 — 해커톤 필수 요건.

대회 규정이 *"Build a new AI agent with **Strands Agents**"* 이고 심사 기준 1번이
그 사용도 자체를 묻는다(`docs/해커톤_요건_대조.md` §1). `llm_agents.py` 는 OpenAI
SDK 를 직접 호출해서 그 기준을 채우지 못한다.

`agents.py`/`llm_agents.py` 와 **정확히 같은 인터페이스**(`SellerAgentPort` /
`BuyerAgentPort`)를 구현한다. `negotiate.py` 는 `NEGOTIATOR_MODE` 만 보고,
라운드 진행·로그 적재·낙찰 로직은 한 줄도 안 바뀐다.

프롬프트는 `agent_prompts.py` 에서 온다 — OpenAI 어댑터와 같은 문장을 쓴다.
정보 은닉 규약(셀러는 바이어 상한가를 못 본다)이 그 문장 안에 들어 있어서,
어댑터마다 프롬프트를 들고 있으면 한쪽만 고쳐질 때 규약이 조용히 깨진다.

**모델 바꾸기.** `_model()` 한 곳만 고치면 된다. Bedrock 으로 가려면
`OpenAIModel(...)` 을 `BedrockModel(model_id=..., region_name=...)` 으로 바꾸는
것이 전부다 — 나머지(`Agent`, `structured_output`, 프롬프트, 스키마)는 그대로다.
해커톤 3단계(가점)가 이 한 줄이다.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from .schemas import SellerRegister, BuyerRequest
from .agent_prompts import seller_prompt, buyer_prompt

# 사용할 모델. .env 의 OPENAI_MODEL 로 덮어쓸 수 있다(기본값: gpt-4o-mini).
# 협상 판단은 비교적 단순한 추론이라 저가 모델로 충분.
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


# ── 구조화 출력 스키마 ──────────────────────────────────────────────────────
# llm_agents.py 의 SELLER_SCHEMA / BUYER_SCHEMA(JSON Schema dict)와 1:1 대응이다.
# Strands 는 pydantic 모델을 받아 스키마를 스스로 만든다.
class SellerOffer(BaseModel):
    """셀러가 이번 라운드에 내놓는 것."""

    price: int = Field(description="이번 라운드에 제시할 단가")
    message: str = Field(description="협상 상대에게 보낼 짧은 메시지(한국어, 1문장)")


class BuyerDecision(BaseModel):
    """바이어가 제시가를 보고 내리는 판단."""

    accept: bool = Field(description="이 제시가를 수락할지 여부")
    message: str = Field(description="협상 상대에게 보낼 짧은 메시지(한국어, 1문장)")


def _model(model_id: str | None = None):
    """
    모델 프로바이더. 여기 한 곳만 바꾸면 Bedrock·Anthropic·Ollama 로 간다.

    임포트를 함수 안에 두는 이유는 `rule` 모드(기본값)가 이 패키지 없이도 돌아야
    하기 때문이다 — `negotiate.py` 가 필요할 때만 이 파일을 임포트한다.
    """
    from strands.models.openai import OpenAIModel

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY 환경변수가 설정되어 있지 않습니다. "
            "Strands 를 쓰려면(협상·리포트·어시스턴트) 이 키가 필요합니다."
        )
    return OpenAIModel(client_args={"api_key": api_key}, model_id=model_id or MODEL)


def _agent(system_prompt: str):
    """
    라운드마다 새 Agent 를 만든다 — 대화 이력을 남기지 않기 위해서다.

    Strands 의 `Agent` 는 기본적으로 메시지 이력을 누적하는데, 협상에서 그러면
    셀러 에이전트가 **자기가 이전에 본 것을 계속 들고 다닌다**. 라운드 간에
    무엇을 기억할지는 `negotiate.py` 가 인자(`round_no`, `last_reject_price`)로
    정하는 것이고, 그게 정보 은닉의 경계이기도 하다. 이력을 SDK 에 맡기면
    그 경계가 흐려진다.
    """
    from strands import Agent

    return Agent(model=_model(), system_prompt=system_prompt)


SELLER_SYSTEM = (
    "당신은 B2B 조달 마켓플레이스의 판매자 측 협상 에이전트입니다. "
    "주어진 사실만 근거로 판단하고, 요청된 형식으로만 답하세요."
)
BUYER_SYSTEM = (
    "당신은 B2B 조달 마켓플레이스의 구매자 측 협상 에이전트입니다. "
    "주어진 사실만 근거로 판단하고, 요청된 형식으로만 답하세요."
)


class StrandsSellerAgent:
    """`SellerAgentPort` 구현. 반환 계약은 규칙 기반과 동일한 `(price, message)`."""

    def decide(
        self, offer: SellerRegister, round_no: int, last_reject_price: int | None
    ) -> tuple[int, str]:
        result = _agent(SELLER_SYSTEM).structured_output(
            SellerOffer, seller_prompt(offer, round_no, last_reject_price)
        )
        return int(result.price), str(result.message)


class StrandsBuyerAgent:
    """`BuyerAgentPort` 구현. 반환 계약은 규칙 기반과 동일한 `(accept, message)`."""

    def decide(
        self, request: BuyerRequest, offer_price: int, seller_message: str
    ) -> tuple[bool, str]:
        result = _agent(BUYER_SYSTEM).structured_output(
            BuyerDecision, buyer_prompt(request, offer_price, seller_message)
        )
        return bool(result.accept), str(result.message)
