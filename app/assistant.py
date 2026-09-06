"""
조달 어시스턴트 — 도구를 직접 부르는 Strands 에이전트 (이식 2단계).

1단계(`strands_agents.py`)는 협상 판단 한 번을 SDK 로 감싼 것이라 **어댑터
교체**에 가깝다. 여기서는 에이전트가 스스로 무엇을 확인할지 정하고 도구를
여러 번 부른다 — 심사 기준이 요구하는 *"non-trivial"* 이 이 차이다.

    "STM32 500개를 60일 안에 받을 수 있나? 어디가 제일 싸지?"
      → list_catalog_items()   품목 코드를 확인하고
      → check_delivery(...)    납기 안에 되는지 룰로 판정한 뒤
      → find_sellers(...)      후보와 조건을 받아
      → 근거를 붙여 답한다

**판정은 여전히 룰이 한다.** 도구는 기존 함수를 감싸기만 하고(`agent_tools.py`),
모델이 정하는 것은 *무엇을 물어볼지*와 *답을 어떻게 쓸지*다. "재고 10대인데
100대 가능"이 모델 쪽에서 다시 생길 자리를 만들지 않는 것이 이 경계의 목적이다.

협상 라운드와는 분리되어 있다 — 협상 에이전트는 도구를 받지 않는다(이유는
`agent_tools.py` 상단).
"""

from __future__ import annotations

import os

from .agent_tools import TOOLS

# 어시스턴트 전용 모델. 지정하지 않으면 협상 쪽과 같은 모델을 쓴다.
MODEL = os.environ.get("ASSISTANT_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

ASSISTANT_SYSTEM = (
    "당신은 B2B 조달 담당자를 돕는 어시스턴트입니다. 전자부품 유통 카탈로그를 "
    "도구로 조회할 수 있습니다.\n"
    "- **도구로 확인한 사실만** 말하세요. 단가·재고·납기를 추측하거나 기억으로 "
    "답하지 마세요\n"
    "- 품목 코드가 불확실하면 먼저 list_catalog_items 로 확인하세요\n"
    "- 납기·수량을 물으면 check_delivery 로 판정하세요. 불가하면 그 도구가 주는 "
    "**납기 내 최대 가능 수량**을 함께 제안하세요\n"
    "- 가격을 물으면 find_sellers 로 후보를 받아 비교하세요. 수량이 바뀌면 "
    "가격구간이 바뀌므로 반드시 요청 수량으로 조회하세요\n"
    "- 판매자의 최저 수용가는 알 수 없습니다. 물어보면 협상 대상이라고 답하세요\n"
    "- 한국어로, 근거(수치와 출처 도구)를 붙여 간결하게 답하세요"
)


def ask(question: str) -> dict:
    """
    질문 하나를 처리하고 답과 **무슨 도구를 몇 번 불렀는지**를 함께 돌려준다.

    도구 호출 내역을 같이 내보내는 이유는 화면에서 보이기 위해서다. 답만 보면
    모델이 지어낸 것과 룰이 판정한 것을 구분할 수 없는데, 이 프로젝트가
    보여주려는 것이 정확히 그 구분이다.

    매 요청마다 새 Agent 를 만든다 — 대화 이력을 서버가 들고 있지 않다.
    (멘토가 "챗봇이면 대화 로그를 DB에 저장해야 한다"고 했다. 그건 별도 작업이고,
     지금은 이력이 없다는 사실을 여기 적어 둔다.)
    """
    from strands import Agent

    from .strands_agents import _model

    agent = Agent(model=_model(MODEL), tools=TOOLS, system_prompt=ASSISTANT_SYSTEM)
    result = agent(question)

    tools_used = [
        {"tool": name, "calls": m.call_count, "errors": m.error_count}
        for name, m in sorted(result.metrics.tool_metrics.items())
    ]
    return {
        "answer": str(result).strip(),
        "tools_used": tools_used,
        "cycles": result.metrics.cycle_count,
    }
