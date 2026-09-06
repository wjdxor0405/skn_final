#!/usr/bin/env python3
"""
협상 어댑터(Strands/OpenAI) 회귀 검증.

pytest 없이 그대로 실행된다:

    python tests/test_agents_and_summary.py

**모델을 실제로 부르지 않는다.** API 키가 없어도 돌아야 하고, 검증하려는 것도
모델 품질이 아니라 우리 코드다 — 프롬프트가 지켜야 할 규약, 가드레일이 무엇을
막는지, 실패했을 때 리포트가 그래도 나오는지.

그래서 **여기서 확인되지 않는 것**을 분명히 해 둔다: Strands SDK 호출 자체와
모델이 실제로 스키마를 지키는지는 키가 있어야 확인된다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent_prompts import seller_prompt, buyer_prompt          # noqa: E402
from app.schemas import SellerRegister, BuyerRequest              # noqa: E402


OFFER = SellerRegister(
    seller_id="한빛테크", item="MCU-A", qty=1000, offer_price=5000,
    floor_price=4200, spec="", lead_time_days=3, min_qty=0,
)
REQUEST = BuyerRequest(item="MCU-A", qty=500, cap_price=4800, spec="", max_lead_time_days=30)


def check_prompt_contract() -> None:
    """
    셀러 프롬프트에 바이어 상한가가 절대 들어가면 안 된다.

    이 규약은 프롬프트 **문장** 안에 있어서 타입도 린터도 안 잡아 준다. 어댑터가
    둘(OpenAI/Strands)이라 각자 프롬프트를 들고 있으면 한쪽만 고쳐질 때 조용히
    깨지는데, agent_prompts.py 한 곳으로 모았으므로 이 검사 하나면 양쪽이 덮인다.
    """
    sp = seller_prompt(OFFER, round_no=2, last_reject_price=4900)
    assert str(REQUEST.cap_price) not in sp, f"셀러 프롬프트에 상한가가 들어갔다:\n{sp}"
    assert "예산" not in sp or "알 수 없" in sp, sp
    assert str(OFFER.floor_price) in sp, "셀러는 자기 최저 수용가를 알아야 판단한다"
    assert "4900" in sp, "직전 거절 이력이 프롬프트에 반영돼야 한다"

    # 바이어 쪽은 자기 예산을 알아야 하고, 셀러의 최저 수용가는 몰라야 한다
    bp = buyer_prompt(REQUEST, offer_price=4700, seller_message="조정했습니다")
    assert str(REQUEST.cap_price) in bp
    assert str(OFFER.floor_price) not in bp, f"바이어 프롬프트에 셀러 최저가가 들어갔다:\n{bp}"

    # 두 어댑터가 같은 문장을 쓰는지 (임포트 경로가 갈라지면 여기서 잡힌다)
    from app import llm_agents, strands_agents
    assert llm_agents.seller_prompt is seller_prompt
    assert strands_agents.seller_prompt is seller_prompt
    assert llm_agents.buyer_prompt is buyer_prompt
    assert strands_agents.buyer_prompt is buyer_prompt
    print("  프롬프트 규약 OK — 셀러에게 상한가가 안 가고, 두 어댑터가 같은 문장을 쓴다")


def check_mode_switch() -> None:
    """NEGOTIATOR_MODE 로 어댑터가 갈리고, 기본값은 키 없이 돈다."""
    import os
    from app.negotiate import _build_agents

    saved = os.environ.get("NEGOTIATOR_MODE")
    try:
        os.environ.pop("NEGOTIATOR_MODE", None)
        s, b = _build_agents()
        assert type(s).__name__ == "RuleBasedSellerAgent", type(s)

        os.environ["NEGOTIATOR_MODE"] = "strands"
        os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")
        s, b = _build_agents()
        assert type(s).__name__ == "StrandsSellerAgent", type(s)
        assert type(b).__name__ == "StrandsBuyerAgent", type(b)

        os.environ["NEGOTIATOR_MODE"] = "llm"
        s, b = _build_agents()
        assert type(s).__name__ == "OpenAISellerAgent", type(s)
    finally:
        os.environ.pop("NEGOTIATOR_MODE", None)
        if saved is not None:
            os.environ["NEGOTIATOR_MODE"] = saved
    print("  모드 전환 OK — rule(기본) / strands / llm")


CHECKS = {
    "prompt_contract": check_prompt_contract,
    "mode_switch": check_mode_switch,
}


def test_prompt_contract(): check_prompt_contract()
def test_mode_switch(): check_mode_switch()


if __name__ == "__main__":
    for name, fn in CHECKS.items():
        print(f"[{name}]")
        fn()
    print("\n전부 통과")
