"""결과 화면 에이전트 — 모델·DB 없이 검증할 수 있는 부분.

슬롯 찾기, 도구 인자 검사, 근거 질문 선조회 트리거, 프롬프트에 구성표·이유가 실리는지, Strands 도구 등록.
실제 모델 응답과 DB 경로는 docs/결과화면_에이전트_strands.md 의 실측 기록.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from src.agent import result_agent as ra


def _result() -> dict:
    def item(slot, name, price, reason="ready", **kw):
        d = {"item_id": str(uuid4()), "slot": slot, "slot_label": slot, "price": price, "qty": 1,
             "selected": True, "alternatives_count": 3, "budget_share": 0.2,
             "product": {"product_key": name.lower().replace(" ", "-"), "name": name, "spec_summary": "성능 티어 6"},
             "reason": {"status": reason, "text": "1순위, 밸런스" if reason == "ready" else None}}
        d.update(kw)
        return d
    return {
        "run_id": str(uuid4()), "category": "computer", "conditions_summary": "게임 · 가성비",
        "budget_max": 1_500_000,
        "items": [item("CPU", "Intel Core Ultra 7 265K", 255_000), item("GPU", "AMD Radeon RX 7600", 525_000),
                  item("케이스", "NR200P", 69_000, reason="pending", selected=False)],
        "totals": {"selected_price": 780_000, "selected_units": 2, "budget_remaining": 720_000, "over_budget": False},
        "verification": {"status": "ready", "confidence": 94, "issues": [{"axis": "power", "text": "ok (근사)"}]},
    }


def _session() -> ra.ResultSession:
    return ra.ResultSession(conn=None, revision_id=uuid4(), result=_result())


def test_item_lookup_by_slot_case_insensitive():
    s = _session()
    assert s.item("gpu")["product"]["name"] == "AMD Radeon RX 7600"
    assert s.item("케이스")["selected"] is False
    assert s.item("모니터") is None


def test_set_item_validates_before_touching_db():
    s = _session()          # conn=None — DB 에 닿으면 AttributeError 로 터진다
    assert s.set_item("GPU", qty="0").startswith("오류")
    assert s.set_item("GPU", qty="abc").startswith("오류")
    assert s.set_item("GPU").startswith("오류")          # 바꿀 값 없음
    assert s.set_item("모니터", qty="2").startswith("오류")
    assert s.swap("GPU", "not-a-uuid").startswith("오류")
    assert s.swap("모니터", str(uuid4())).startswith("오류")
    assert len(s.trace) == 6 and not s.changed


def test_prefetch_triggers_only_on_why_questions(monkeypatch):
    s = _session()
    monkeypatch.setattr(ra.ResultSession, "explain", lambda self, slot: f"EXPLAIN[{slot}]")
    assert ra._prefetch_explanations(s, "그래픽카드 더 싼 걸로") == ""
    assert ra._prefetch_explanations(s, "왜 이 CPU야?") == "EXPLAIN[CPU]"
    assert ra._prefetch_explanations(s, "그래픽카드 리뷰 어때?") == "EXPLAIN[GPU]"    # 동의어 → 슬롯
    out = ra._prefetch_explanations(s, "이 구성 괜찮아?")                                # 슬롯 없음 → 담긴 것 전부
    assert out == "EXPLAIN[CPU]\n\nEXPLAIN[GPU]"                                            # 빼둔 케이스는 제외


def test_system_prompt_carries_table_reasons_budget_and_language():
    r = _result()
    p = ra.system_prompt(r, "왜 이 CPU야?", [], prefetched="PRE")
    assert "- CPU: Intel Core Ultra 7 265K · 255,000원 × 1" in p
    assert "추천 이유: 1순위, 밸런스" in p
    assert "케이스: NR200P · 69,000원 × 1 (빼둠)" in p
    assert "예산 상한: 1,500,000원 · 총액: 780,000원 · 잔여: 720,000원" in p
    assert "[power] ok (근사)" in p
    assert "미리 조회한 근거" in p and "\nPRE" in p
    assert "답변 언어: 한국어 존댓말." in p and "마크다운" in p.splitlines()[-1]
    assert "미리 조회한 근거" not in ra.system_prompt(r, "swap the gpu", [])


def test_strands_registers_change_and_question_tools():
    tools = ra.make_tools(_session())
    assert [t.tool_name for t in tools] == [
        "list_alternatives", "swap", "set_qty", "remove_or_restore", "explain",
        "preview_swap", "upgrade_options", "savings_options", "check_build", "game_check",
        "search_unavailable_part"]
    swap_spec = next(t for t in tools if t.tool_name == "swap").tool_spec
    assert set(swap_spec["inputSchema"]["json"]["required"]) == {"slot", "candidate_id"}


# ── DB 미보유 부품 실시간 검색 — 동의 확인은 코드가 정한다 ──────────────────────────
def test_search_confirmed_requires_both_marker_and_affirmation():
    marker_reply = f"RTX 6090은 저희 DB에 없는 상품으로 확인됩니다. {ra._SEARCH_PERMISSION_MARKER}"
    assert ra._search_confirmed([("RTX 6090으로 바꿔줘", marker_reply)], "응") is True
    assert ra._search_confirmed([("RTX 6090으로 바꿔줘", marker_reply)], "아니 됐어") is False
    assert ra._search_confirmed([("아무 말", "그냥 평범한 답변")], "응") is False  # 마커 없으면 동의로 안 침
    assert ra._search_confirmed([], "응") is False


def test_search_unavailable_part_found_in_catalog_tells_agent_not_missing(monkeypatch):
    from src.dto import Candidate

    s = _session()
    found = Candidate(product_key="k", slot="GPU", name="NVIDIA GeForce RTX 5090")
    monkeypatch.setattr("src.repo.catalog_repo.load_candidates_by_slot_from_db", lambda conn: {"GPU": [found]})
    monkeypatch.setattr("src.engine.owned_parts._match_catalog", lambda text, pool: pool)

    out = s.search_unavailable_part("GPU", "RTX 5090")
    assert "실제로 카탈로그에 있습니다" in out
    assert "RTX 5090" in out or "5090" in out


def test_search_unavailable_part_not_confirmed_asks_permission_without_calling_lookup(monkeypatch):
    def boom(*_a, **_kw):
        raise AssertionError("동의 전인데 실시간 검색이 호출됨")

    s = _session()
    assert s.search_confirmed is False
    monkeypatch.setattr("src.repo.catalog_repo.load_candidates_by_slot_from_db", lambda conn: {"GPU": []})
    monkeypatch.setattr("src.engine.owned_parts._match_catalog", lambda text, pool: [])
    monkeypatch.setattr("src.services.live_spec_lookup.lookup", boom)
    monkeypatch.setattr("src.services.live_spec_lookup.available", lambda: True)

    out = s.search_unavailable_part("GPU", "RTX 6090")
    assert ra._SEARCH_PERMISSION_MARKER in out
    assert "DB에 없는 상품" in out


def test_search_unavailable_part_confirmed_calls_lookup_and_adds_disclaimer(monkeypatch):
    from src.services.live_spec_lookup import LiveSpecLookupResult

    s = _session()
    s.search_confirmed = True
    monkeypatch.setattr("src.repo.catalog_repo.load_candidates_by_slot_from_db", lambda conn: {"GPU": []})
    monkeypatch.setattr("src.engine.owned_parts._match_catalog", lambda text, pool: [])
    monkeypatch.setattr("src.services.live_spec_lookup.available", lambda: True)
    monkeypatch.setattr("src.services.live_spec_lookup.lookup", lambda conn, text, **kw: LiveSpecLookupResult.model_validate(
        {"relevant": True, "supported_fields": {"interface": "PCIe 5.0"}, "source_url": "https://example.com/rtx6090"}))

    out = s.search_unavailable_part("GPU", "RTX 6090")
    assert "PCIe 5.0" in out
    assert "example.com/rtx6090" in out
    assert "카탈로그 정식 등재 값이 아니" in out


def test_unavailable_under_mock_mode(monkeypatch):
    monkeypatch.setattr(ra, "MOCK_MODE", True)
    monkeypatch.setattr(ra, "RESULT_AGENT", True)
    assert ra.available() is False


# ── 수치 가드 ──────────────────────────────────────────────────────────────
def test_numbers_normalise_commas_percent_and_product_names():
    assert ra._numbers("총액 1,457,000원 · 예산 비중 18% · RTX 5090 · DDR4-3600 · 1TB · 신뢰도 94점") == {
        "1457000", "18", "5090", "4", "3600", "1", "94"}


def test_reply_within_accepts_input_numbers_and_rejects_invented():
    prompt = "- GPU: RX 7600 · 525,000원 × 1\n예산 상한: 1,500,000원 · 총액: 1,457,000원 · 잔여: 43,000원"
    ok, out = ra._reply_within("총액은 1457000원이고 예산이 43,000원 남았습니다.", [prompt])
    assert ok and not out
    ok, out = ra._reply_within("이 GPU는 약 620,000원 정도의 성능입니다.", [prompt])
    assert not ok and out == {"620000"}
    ok, _ = ra._reply_within("교체했습니다 (+84,000원).", [prompt, "swap → … (+84,000원)"])   # 도구 결과의 숫자도 허용
    assert ok


def test_guarded_reply_prefers_changes_then_prefetched_then_template():
    s = _session()
    s.changed = True
    s.trace = ["prefetch:explain('CPU') → …", "list_alternatives('CPU') → CPU 현재: X\n1. candidate_id=abc",
               "set_item('저장장치', selected='', qty='2') → 저장장치: selected=True qty=2 · 총액 780,000원 · 예산 잔여 720,000원"]
    out = ra._guarded_reply(s, "")
    assert out.startswith("적용된 변경: 저장장치: selected=True qty=2") and "총액 780,000원" in out
    assert "candidate_id" not in out          # 후보 목록 조회는 '적용된 변경'이 아니다
    s2 = _session()
    assert ra._guarded_reply(s2, "CPU X 255,000원\n저장된 추천 이유: …") == "CPU X 255,000원 저장된 추천 이유: …"
    assert ra._guarded_reply(_session(), "").startswith("구성표 기준으로만 답할 수 있어요")



# ── 묻는 말 (2026-10-02) ───────────────────────────────────────────────────
_ASK = ("CPU 더 성능 좋은 걸로 바꿔도 문제없을까?", "램 32기가로 늘려도 돼?", "그래픽카드 올려도 괜찮아?",
        "그래픽카드 더 좋은 걸로 바꾸면 어때?", "파워 이걸로 바꾸면 될까?", "CPU 바꿔도 되나요", "CPU 바꿀까?",
        "그래픽카드 한 단계 올리면 파워는 괜찮아?",
        # 실제 말투 (2026-10-02 — 예전 정규식은 이 7개 중 1개만 잡았다)
        "글카 갈아타도됨?", "cpu 7600x로 가면 괜찮음?", "램 32로 ㄱㄱ?", "그래픽 올리는거 어케생각함",
        "파워 바꾸는거 ㄱㅊ?", "씨퓨 업글 해도 무방?", "SSD 1테라 더 달아도 됨")
_CHANGE = ("CPU 한 단계 좋은 걸로 바꿔줘", "그래픽카드도 바꿔줘", "SSD 2개로", "SSD 2개로 해줘", "쿨러 빼줘",
           "그래픽카드 더 싼 걸로 바꿔줄래?", "응 그걸로 바꿔줘", "ㅇㅇ", "응", "네", "7600X로", "그걸로 ㄱㄱ",
           "예산 200만원으로 늘려줘", "그래픽카드 더 저렴한 걸로", "글카 4060ti로 바꿔주세요", "글카 한단계 위로 바꿔주셈",
           # 10/3 리허설 대본 (P1-5 여러 턴 회귀 — 앞 턴 제안을 받는 말, 방향만 말하는 교체)
           "응 그렇게 바꿔줘", "그걸로 바꿔줘", "램이 너무 비싼데 좀 더 싼 걸로 바꿔줘", "CPU를 7600X로 바꿔줘")
_OTHER = ("돈 남았는데 바꿀 거 추천해 줄 수 있나?", "왜 이 그래픽카드 골랐어?", "이 구성 괜찮아?", "파워 용량 충분해?",
          "이 글카 ㄱㅊ?", "쿨러 꼭 사야 돼?", "인텔이랑 AMD 차이가 뭐야?", "남는돈으로 머 올리지",
          # 10/3 리허설 대본 — "그대로 두고"·거절·가정 질문은 구성표를 바꾸지 않는다
          "남은 예산 얼마야?", "그래픽카드 MSI 제품 맞아?", "CPU를 한 단계 낮추면 얼마나 아껴?", "ㄴㄴ",
          "그럼 CPU는 그대로 두고, 전체에서 10만원 정도 줄일 수 있어?",
          "아니 CPU는 그대로 두고, 대신 전체에서 10만원 정도 줄일 수 있어?",
          # 잘못된 전제 (P1-5) — 없던 약속을 근거로 바꾸게 하지 않는다
          "아까 쿨러 빼 준다고 했잖아, 왜 아직 있어?")


@pytest.mark.parametrize("text", _ASK)
def test_classify_ask(text):
    assert ra.classify_intent(text) == "ask"


@pytest.mark.parametrize("text", _CHANGE)
def test_classify_change(text):
    assert ra.classify_intent(text) == "change"


@pytest.mark.parametrize("text", _OTHER)
def test_classify_other_does_not_open_write_tools(text):
    """바꾸라는 말이 아니면(묻는 말이든 다른 말이든) 바꾸는 도구가 닫힌다 — 허용 목록."""
    assert ra.classify_intent(text) == "other"


@pytest.mark.parametrize("text", [
    "예산 조금 넘어도 괜찮아, 그걸로 바꿔줘",                       # 실패 6 (10/3 리허설)
    "예산 조금 넘어도 괜찮아, 그래픽카드 한 단계 좋은 걸로 바꿔줘",
    "비싸도 상관없어. 4070으로 바꿔줘",
])
def test_permission_plus_imperative_is_a_change_request(text):
    """허락('넘어도 괜찮아')이 묻는 말로 먼저 잡혀 바꾸는 도구가 닫히고 되묻던 것 — 명령이 든 절이 따로 있으면 바꾼다."""
    assert ra.classify_intent(text) == "change"


@pytest.mark.parametrize("text", ["바꿔줘도 괜찮아?", "예산 조금 넘어도 괜찮아?", "그래픽카드 바꿔줘도 돼?"])
def test_imperative_inside_a_question_stays_a_question(text):
    assert ra.classify_intent(text) != "change"


def test_read_only_turn_refuses_writes_without_touching_db():
    """"램 32기가로 늘려도 돼?"에 모델이 set_qty 를 불러 수량을 2로 바꾸던 문제 — 묻는 말이면 코드가 막는다."""
    s = _session()
    s.read_only = True                        # conn=None — DB 에 닿으면 터진다
    assert s.set_item("RAM", qty="2").startswith("거절")
    assert s.swap("GPU", str(uuid4())).startswith("거절")
    assert not s.changed and len(s.outputs) == 2


def test_whatif_question_skips_why_prefetch(monkeypatch):
    s = _session()
    monkeypatch.setattr(ra.ResultSession, "explain", lambda self, slot: f"EXPLAIN[{slot}]")
    assert ra._prefetch_explanations(s, "그래픽카드 올려도 괜찮아?") == ""


def test_guard_sources_are_full_tool_outputs_not_trimmed_trace():
    """trace 는 160자로 잘린다 — 가드가 trace 를 보면 긴 후보 목록의 뒤쪽 가격을 '지어낸 숫자'로 버렸다(실측)."""
    s = _session()
    long_out = "후보:\n" + "\n".join(f"{i}. 제품{i} · {100_000 + i * 1_111:,}원" for i in range(1, 20))
    s._record("list_alternatives('CPU')", long_out)
    assert "119,998" not in s.trace[0] and "119,998" in s.outputs[0]
    ok, _ = ra._reply_within("제품18은 119,998원입니다.", [*s.outputs])
    assert ok


def test_guarded_reply_falls_back_to_question_tool_output_without_ids():
    s = _session()
    s._record("upgrade_options(extra='', new_budget='')",
              "- CPU: 지금 X\n    · 한 단계 위: Y 279,000원 (추가 57,250원) · candidate_id=514d34f7-438a-49b5-b902-5c30a7021058")
    out = ra._guarded_reply(s, "")
    assert "한 단계 위: Y 279,000원 (추가 57,250원)" in out and "candidate_id" not in out


def test_reply_within_accepts_fraction_as_percent():
    ok, _ = ra._reply_within("230W가 675W(750W의 90%) 이내입니다.", ["230W ≤ 파워 750W × 0.9 = 675W"])
    assert ok


def test_prefetch_skips_words_that_point_at_no_part():
    """"오늘 날씨 어때?"의 '어때'로 부품 8개 근거를 다 조회하던 것 — 부품도 구성 전체도 가리키지 않으면 조회하지 않는다."""
    s = _session()
    assert ra._prefetch_explanations(s, "오늘 날씨 어때?") == ""


def test_highest_price_word_is_not_evaluative():
    assert ra.evaluative_words("각 슬롯의 최고가 후보로 바꿨습니다") == []
    assert ra.evaluative_words("최고의 선택입니다") == ["최고"]


def test_evaluative_words_from_the_users_own_message_are_not_flagged():
    """잘못된 전제("RTX 4090이니까 4K 최고 옵션도 되지?")를 정정하는 답은 사용자 말을 옮긴다 — 막으면 정정이 사라진다."""
    reply = "현재 그래픽카드는 RTX 4090이 아니라 RX 7600이라 4K 최고 옵션은 확인되지 않습니다."
    assert ra.evaluative_words(reply, "그래픽카드가 RTX 4090이니까 4K 최고 옵션도 되지?") == []
    assert ra.evaluative_words(reply + " 압도적인 구성입니다.", "4K 최고 옵션도 되지?") == ["압도적"]


def test_search_unavailable_part_busy_lookup_is_a_sentence_not_an_exception(monkeypatch):
    from src.errors import ServiceUnavailable

    def busy(conn, text, **kw):
        raise ServiceUnavailable("지금 검색 요청이 몰려 있어요. 잠시 후 다시 시도해 주세요.", code="live_part_lookup_busy")

    s = _session()
    s.search_confirmed = True
    monkeypatch.setattr("src.repo.catalog_repo.load_candidates_by_slot_from_db", lambda conn: {"GPU": []})
    monkeypatch.setattr("src.engine.owned_parts._match_catalog", lambda text, pool: [])
    monkeypatch.setattr("src.services.live_spec_lookup.available", lambda: True)
    monkeypatch.setattr("src.services.live_spec_lookup.lookup", busy)

    out = s.search_unavailable_part("GPU", "RTX 6090")
    assert "몰려 있어요" in out and "가져오지 못했습니다" in out


@pytest.mark.parametrize("text", ["가격에 맞게 예산 줄여줘", "우선순위를 성능으로 바꿔줘", "예산 200만원으로 늘려줘",
                                  "용도를 영상 편집으로 바꿔줘"])
def test_condition_change_request_gets_the_conditions_screen_notice(text):
    """결과 채팅은 조건을 바꾸지 않는다 — 실패 5("예산 줄여줘"가 부품 절약으로 오독)와 팀원 관찰("우선순위를 성능으로
    바꿔줘"가 반영된 것처럼 읽힘). 모델이 안내를 빠뜨려도 코드가 '조건 바꾸기'를 붙인다."""
    reply = ra.with_condition_notice(text, "부품 후보는 다음과 같습니다.")
    assert reply.startswith("부품 후보는") and reply.endswith(ra.CONDITION_NOTICE)
    assert ra.with_condition_notice(text, reply) == reply                       # 이미 있으면 다시 붙이지 않는다


@pytest.mark.parametrize("text", ["남은 예산으로 할 만한 업그레이드 있어?", "예산 조금 넘어도 괜찮아, 그걸로 바꿔줘",
                                  "예산 안에서 그래픽카드 바꿔줘", "예산 맞춰서 그래픽카드 올려줘", "남은 예산 얼마야?",
                                  "예산 초과 안 되게 램 바꿔줘", "CPU를 한 단계 낮추면 얼마나 아껴?"])
def test_part_requests_that_mention_the_budget_get_no_notice(text):
    assert ra.with_condition_notice(text, "답") == "답"
