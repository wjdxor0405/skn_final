#!/usr/bin/env python3
"""
협상 어댑터(Strands/OpenAI)·설명 제너레이션·에이전트 도구 회귀 검증.

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
from app.schemas import SellerRegister, BuyerRequest, Envelope, MsgType  # noqa: E402
from app import report                                              # noqa: E402


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


# ── 설명 제너레이션 ─────────────────────────────────────────────────────────
def _log() -> list[Envelope]:
    """낙찰까지 간 최소 로그. 한 곳은 스크리닝에서, 한 곳은 가격으로 밀린다."""
    def env(t, frm, to, **payload):
        return Envelope(type=t, **{"from": frm}, to=to, txid="TX-TEST", payload=payload)

    return [
        env(MsgType.REQUEST, "buyer", "central", item="MCU-A", qty=500, cap_price=4800),
        env(MsgType.REQUEST, "central", "*", item="MCU-A", qty=500),
        env(MsgType.REJECT, "central", "머큐리", stage="screening", reason="재고부족(보유 0 < 요청 500)"),
        env(MsgType.OFFER, "한빛테크", "central", item="MCU-A", price=4500),
        env(MsgType.OFFER, "오퍼렛", "central", item="MCU-A", price=4700),
        env(MsgType.SETTLED, "central", "buyer", seller_id="한빛테크", price=4500, qty=500),
    ]


def check_facts_and_template() -> None:
    f = report._facts(_log())
    assert f["winner"] == "한빛테크" and f["price"] == 4500, f
    assert list(f["sellers"]) == ["한빛테크", "오퍼렛"], f["sellers"]
    assert f["screening_rejects"][0]["seller_id"] == "머큐리"
    assert f["cap"] == 4800, "상한가는 buyer→central 접수 메시지에서만 온다"

    text = report.TemplateSummarizer().summarize(_log())
    assert "한빛테크" in text and "4500" in text, text
    print("  사실 추출·템플릿 OK — 낙찰자·상한가·스크리닝 사유가 전부 잡힌다")


def check_verify_guard() -> None:
    """가드레일이 무엇을 막는지 — 막아야 할 것만 막고 멀쩡한 건 통과시킨다."""
    f = report._facts(_log())

    ok, _ = report._verify("한빛테크가 4,500원으로 최저가를 제시해 채택되었습니다.", f)
    assert ok, "천 단위 구분이 들어간 정상 문장을 막으면 안 된다"

    for bad, why in [
        ("", "빈 응답"),
        ("오퍼렛이 4500원으로 채택되었습니다.", "낙찰자가 틀림"),
        ("한빛테크가 3900원으로 채택되었습니다.", "낙찰가가 틀림"),
    ]:
        ok, _ = report._verify(bad, f)
        assert not ok, f"막았어야 한다({why}): {bad!r}"
    print("  가드레일 OK — 낙찰자·낙찰가가 틀리면 잡고, 천 단위 구분은 통과시킨다")


def check_llm_fallback() -> None:
    """
    설명 생성이 실패하거나 사실과 어긋나면 **템플릿과 똑같은 문장**이 나온다.

    리포트는 승인 흐름의 산출물이라 "요약을 못 만들어서 리포트가 없다"가 되면 안 된다.
    """
    expected = report.TemplateSummarizer().summarize(_log())

    s = report.LLMSummarizer()
    s._generate = lambda f: (_ for _ in ()).throw(RuntimeError("네트워크 끊김"))
    assert s.summarize(_log()) == expected, "생성 실패인데 템플릿으로 안 돌아갔다"

    s = report.LLMSummarizer()
    s._generate = lambda f: "오퍼렛이 9999원에 낙찰되었습니다."   # 사실과 어긋남
    assert s.summarize(_log()) == expected, "사실과 어긋나는데 그대로 내보냈다"

    s = report.LLMSummarizer()
    s._generate = lambda f: "규칙 엔진은 한빛테크의 4,500원을 채택했습니다. 오퍼렛은 4,700원으로 더 높았습니다."
    out = s.summarize(_log())
    assert out.startswith("규칙 엔진은"), out
    print("  폴백 OK — 실패·사실불일치면 템플릿, 정상이면 생성문이 나간다")


def check_explain_prompt() -> None:
    """설명 프롬프트에는 룰이 뽑은 사실만 들어간다 — 원문 로그가 새면 안 된다."""
    f = report._facts(_log())
    p = report._explain_prompt(f)
    assert "한빛테크" in p and "4500" in p and "머큐리" in p, p
    assert "재고부족" in p, "탈락 사유가 있어야 '왜 탈락했는지'를 설명할 수 있다"
    assert "Envelope" not in p and "txid" not in p, f"원문 로그가 프롬프트에 샜다:\n{p}"
    print("  설명 프롬프트 OK — 사실 dict 만 들어가고 원문 로그는 안 들어간다")


# ── Strands 도구 (이식 2단계) ────────────────────────────────────────────────
def check_tools_are_plain_functions() -> None:
    """
    @tool 로 감싼 뒤에도 평범한 함수로 부를 수 있어야 한다.

    그래야 도구의 판정을 **모델 없이** 검증할 수 있다. 판정은 룰이 하고 모델은
    무엇을 물어볼지만 정한다는 경계가, 이 검사가 성립한다는 사실로 보장된다.
    """
    from app.agent_tools import list_catalog_items, find_sellers, check_delivery

    items = list_catalog_items()
    assert items and "code" in items[0], items[:1]

    d = check_delivery("39-01-2040", 838293, 30)
    assert d["feasible"] is False, d
    assert d["max_feasible_qty"] == 694443, d["max_feasible_qty"]
    assert any("댈 수 있는 판매자가 없습니다" in r for r in d["reasons"]), d["reasons"]

    d = check_delivery("없는품목", 1, 10)
    assert "error" in d, "없는 품목이면 예외가 아니라 error 를 돌려줘야 도구가 계속 돈다"
    print("  도구 직접 호출 OK — 모델 없이 판정이 검증된다")


def check_tools_hide_floor_price() -> None:
    """
    도구가 셀러의 최저 수용가를 절대 내보내면 안 된다.

    `store.sellers_for()` 는 `floor_price` 를 들고 있는 `SellerRegister` 를
    돌려준다. 그걸 그대로 직렬화해 모델에 넘기면 **바이어 쪽 에이전트가 셀러의
    유보가격을 보게 된다** — 알면 거기까지 깎으면 그만이라 협상이 성립하지 않는다.
    도구가 dict 로 바꾸는 자리가 모델로 나가는 유일한 경로라 거기서 잘라낸다.
    """
    from app.agent_tools import find_sellers
    from app.store import store

    raw = store.sellers_for("STM32F103C8T6", 500)
    assert raw and raw[0].floor_price, "전제: 원본에는 floor_price 가 있다"

    rows = find_sellers("STM32F103C8T6", 500)
    assert rows, rows
    for r in rows:
        assert "floor" not in " ".join(r).lower(), f"최저 수용가가 샜다: {r}"
        assert raw[0].floor_price not in r.values(), f"최저 수용가 값이 샜다: {r}"
    assert {"seller_id", "offer_price", "available_qty"} <= set(rows[0]), rows[0]
    print("  도구 정보 은닉 OK — 최저 수용가가 모델로 안 나간다")


def check_tool_specs() -> None:
    """모델이 보는 도구 스펙(이름·인자)이 실제 시그니처와 맞는지."""
    from app.agent_tools import TOOLS

    specs = {t.tool_spec["name"]: t.tool_spec for t in TOOLS}
    assert set(specs) == {"list_catalog_items", "find_sellers", "check_delivery"}, list(specs)
    assert set(specs["find_sellers"]["inputSchema"]["json"]["properties"]) == {"item", "qty"}
    assert set(specs["check_delivery"]["inputSchema"]["json"]["properties"]) == {
        "item", "qty", "due_days"}
    for name, spec in specs.items():
        assert spec.get("description"), f"{name}: 설명이 없으면 모델이 언제 부를지 모른다"
    print("  도구 스펙 OK — 3종, 인자와 설명이 붙어 있다")


def check_negotiators_have_no_tools() -> None:
    """
    협상 에이전트는 도구를 받지 않는다 — 정보 은닉이 깨진다.

    셀러가 find_sellers 를 부르면 경쟁 셀러의 제시가를 보고, 바이어가 부르면
    카탈로그를 통째로 본다. 협상 라운드 안에서는 negotiate.py 가 넘겨주는 것만
    알아야 한다.
    """
    import inspect
    from app import strands_agents

    src = inspect.getsource(strands_agents)
    assert "agent_tools" not in src and "tools=" not in src, (
        "협상 어댑터에 도구가 들어갔다 — 정보 은닉이 깨진다")
    print("  협상 어댑터에 도구 없음 OK")


CHECKS = {
    "prompt_contract": check_prompt_contract,
    "mode_switch": check_mode_switch,
    "facts_template": check_facts_and_template,
    "verify_guard": check_verify_guard,
    "llm_fallback": check_llm_fallback,
    "explain_prompt": check_explain_prompt,
    "tools_plain": check_tools_are_plain_functions,
    "tools_hide_floor": check_tools_hide_floor_price,
    "tool_specs": check_tool_specs,
    "negotiators_no_tools": check_negotiators_have_no_tools,
}


def test_prompt_contract(): check_prompt_contract()
def test_mode_switch(): check_mode_switch()
def test_facts_template(): check_facts_and_template()
def test_verify_guard(): check_verify_guard()
def test_llm_fallback(): check_llm_fallback()
def test_explain_prompt(): check_explain_prompt()
def test_tools_plain(): check_tools_are_plain_functions()
def test_tools_hide_floor(): check_tools_hide_floor_price()
def test_tool_specs(): check_tool_specs()
def test_negotiators_no_tools(): check_negotiators_have_no_tools()


if __name__ == "__main__":
    for name, fn in CHECKS.items():
        print(f"[{name}]")
        fn()
    print("\n전부 통과")
