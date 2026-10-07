"""결과 화면 채팅 저장·복원 (CHAT-08).

`recommendation_service.handle_result_message`이 매 턴을 `identity.message`에 저장하는지, 조건 대화와
합쳐 `GET /session/{id}`가 시간순으로 복원하는지, 에이전트의 이어 말하기 맥락(`_db_history`)이 조건
대화는 빼고 이번 run 이후 턴만 보는지를 본다. tests/test_list_service.py 와 같은 방식(실 로컬
PostgreSQL, autocommit)으로 돈다 — RESULT_AGENT=0(규칙 경로)로 고정해 LLM 키 없이도 결정적으로 돈다.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from src.auth.deps import Principal
from src.config import DATABASE_URL
from src.repo.plan_repo import PlanRepo
from src.repo.user_repo import ConversationRepo, UserRepo
from src.services import auth_service, recommendation_service, session_service


class _Ctx:
    def __init__(self, connection):
        self.conn = connection
        self.created_ids: list[str] = []

    def signup(self, email: str, password: str = "abc12345") -> Principal:
        user, token = auth_service.signup(
            self.conn, Principal(user_id=None, browser_token=None),
            email=email, password=password, display_name="채팅저장테스트",
            terms_agreed=True, privacy_agreed=True, marketing_agreed=False,
        )
        self.created_ids.append(user["id"])
        import base64
        import json
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        iat = json.loads(base64.urlsafe_b64decode(padded))["iat"]
        return Principal(user_id=uuid.UUID(user["id"]), browser_token=None, session_iat=iat)

    def recommended_list(self, principal: Principal, message: str = "게임용으로 맞춰줘") -> str:
        """조건 대화 한 턴(자유 문장, 규칙 파서) → 나머지 조건 직접 채움 → 추천 실행까지 마친 list_id."""
        list_id = session_service.create_session(self.conn, principal)["list_id"]
        list_uuid = uuid.UUID(list_id)
        session_service.choose_category(self.conn, list_uuid, "computer", "build", principal)
        session_service.handle_message(self.conn, list_uuid, message, principal)
        for field, value in (("purpose", "game"), ("budget_max", 1500000), ("priority", "value")):
            session_service.patch_slot(self.conn, list_uuid, field, value, principal)
        revision_id = PlanRepo(self.conn).get_current_revision(list_uuid)["id"]
        accepted = recommendation_service.start_recommendation(self.conn, revision_id, strategy="default")
        recommendation_service.execute_recommendation(revision_id, uuid.UUID(accepted["run_id"]))
        return list_id


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("RESULT_AGENT", "0")   # 규칙 경로로 고정 — LLM 키 없이 결정적으로
    import src.config as config
    monkeypatch.setattr(config, "RESULT_AGENT", False)
    try:
        connection = psycopg.connect(DATABASE_URL, prepare_threshold=None, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("로컬 PostgreSQL(DATABASE_URL)에 연결할 수 없습니다 — db/setup_all.py로 준비하세요.")
    context = _Ctx(connection)
    try:
        yield context
    finally:
        for user_id in context.created_ids:
            try:
                UserRepo(connection).withdraw(uuid.UUID(user_id))
            except Exception:  # noqa: BLE001
                pass
        connection.close()


def _unique_email() -> str:
    return f"chat08-{uuid.uuid4().hex}@example.com"


def _conversation_id(conn, list_id: str):
    return PlanRepo(conn).get_current_revision(uuid.UUID(list_id))["conversation_id"]


# ── 저장 ──────────────────────────────────────────────────────────────────────────────

def test_a_result_chat_turn_is_saved_to_identity_message(ctx):
    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal)

    turn = recommendation_service.handle_result_message(ctx.conn, PlanRepo(ctx.conn).get_current_revision(
        uuid.UUID(list_id))["id"], "그래픽카드 더 저렴한 걸로")
    assert turn["reply"]

    rows = ConversationRepo(ctx.conn).messages(_conversation_id(ctx.conn, list_id))
    last_two = [(r["role"], r["content"]) for r in rows[-2:]]
    assert last_two == [("user", "그래픽카드 더 저렴한 걸로"), ("assistant", turn["reply"])]


def test_every_early_return_path_still_saves_the_turn(ctx):
    """규칙 경로엔 조기 반환이 여러 곳(총평 요청·모르는 슬롯·애매한 방향)이다 — 전부 저장돼야 한다."""
    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal)
    revision_id = PlanRepo(ctx.conn).get_current_revision(uuid.UUID(list_id))["id"]

    before = len(ConversationRepo(ctx.conn).messages(_conversation_id(ctx.conn, list_id)))
    recommendation_service.handle_result_message(ctx.conn, revision_id, "전체 요약해줘")
    recommendation_service.handle_result_message(ctx.conn, revision_id, "모니터 바꿔줘")          # 모르는 슬롯
    recommendation_service.handle_result_message(ctx.conn, revision_id, "그래픽카드 어때?")        # 방향 애매(질문)
    after = len(ConversationRepo(ctx.conn).messages(_conversation_id(ctx.conn, list_id)))
    assert after - before == 6   # 3턴 × (user+assistant)


# ── 복원: 조건 대화와 합쳐 시간순으로 ────────────────────────────────────────────────────

def test_get_session_restores_condition_chat_and_result_chat_together_in_order(ctx):
    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal, message="게임용으로 맞춰줘")
    revision_id = PlanRepo(ctx.conn).get_current_revision(uuid.UUID(list_id))["id"]
    recommendation_service.handle_result_message(ctx.conn, revision_id, "왜 이 CPU야?")

    state = session_service.get_session_state(ctx.conn, uuid.UUID(list_id), principal)
    texts = [(m["role"], m["text"]) for m in state["messages"]]
    assert ("user", "게임용으로 맞춰줘") in texts                 # 조건 대화
    assert ("user", "왜 이 CPU야?") in texts                      # 결과 화면 대화
    # 조건 대화가 결과 대화보다 먼저 온다(생성 순서 그대로).
    assert texts.index(("user", "게임용으로 맞춰줘")) < texts.index(("user", "왜 이 CPU야?"))


# ── 에이전트 맥락: 조건 대화는 빼고 이번 run 이후만 ──────────────────────────────────────────

def test_db_history_excludes_condition_chat_and_only_includes_this_runs_turns(ctx):
    from src.agent import result_agent

    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal, message="이건 조건 대화라 결과 채팅 맥락에 안 들어가야 한다")
    revision_id = PlanRepo(ctx.conn).get_current_revision(uuid.UUID(list_id))["id"]
    run_id = PlanRepo(ctx.conn).load_full(revision_id) and recommendation_service.get_stored_result(
        ctx.conn, revision_id)["run_id"]

    recommendation_service.handle_result_message(ctx.conn, revision_id, "첫 번째 결과 화면 질문")
    recommendation_service.handle_result_message(ctx.conn, revision_id, "두 번째 결과 화면 질문")

    pairs = result_agent._db_history(ctx.conn, revision_id, run_id)
    users = [u for u, _ in pairs]
    assert "이건 조건 대화라 결과 채팅 맥락에 안 들어가야 한다" not in users
    assert users == ["첫 번째 결과 화면 질문", "두 번째 결과 화면 질문"]


def test_db_history_keeps_only_the_most_recent_n_turns(ctx):
    from src.agent import result_agent

    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal)
    revision_id = PlanRepo(ctx.conn).get_current_revision(uuid.UUID(list_id))["id"]
    run_id = recommendation_service.get_stored_result(ctx.conn, revision_id)["run_id"]

    for i in range(result_agent._HISTORY_TURNS + 3):
        recommendation_service.handle_result_message(ctx.conn, revision_id, f"질문 {i}")

    pairs = result_agent._db_history(ctx.conn, revision_id, run_id)
    assert len(pairs) == result_agent._HISTORY_TURNS
    assert pairs[-1][0] == f"질문 {result_agent._HISTORY_TURNS + 2}"   # 가장 최근 것이 마지막


# ── 턴 기록(P1-3): 답 메시지의 metadata["turn"] ────────────────────────────────────────────

def _last_turn_log(conn, list_id: str) -> dict:
    rows = ConversationRepo(conn).messages(_conversation_id(conn, list_id))
    assistant = [r for r in rows if r["role"] == "assistant"]
    return (assistant[-1]["metadata"] or {}).get("turn")


def test_rule_path_turn_is_logged_with_path_and_latency(ctx):
    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal)
    revision_id = PlanRepo(ctx.conn).get_current_revision(uuid.UUID(list_id))["id"]

    recommendation_service.handle_result_message(ctx.conn, revision_id, "그래픽카드 더 저렴한 걸로")

    log = _last_turn_log(ctx.conn, list_id)
    assert log["path"] == "rules"
    assert isinstance(log["latency_ms"], int) and log["latency_ms"] >= 0


def test_condition_change_request_saves_the_notice_and_logs_it(ctx):
    """"우선순위를 성능으로 바꿔줘" — 결과 채팅은 조건을 안 바꾼다. 저장되는 답(대화 복원에 쓰이는 것)에도 안내가 있어야 한다."""
    from src.agent.result_agent import CONDITION_NOTICE
    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal)
    revision_id = PlanRepo(ctx.conn).get_current_revision(uuid.UUID(list_id))["id"]

    turn = recommendation_service.handle_result_message(ctx.conn, revision_id, "우선순위를 성능으로 바꿔줘")

    assert turn["reply"].endswith(CONDITION_NOTICE)
    rows = ConversationRepo(ctx.conn).messages(_conversation_id(ctx.conn, list_id))
    assert rows[-1]["content"] == turn["reply"]
    assert _last_turn_log(ctx.conn, list_id)["notice"] == "conditions"


def test_agent_turn_logs_tools_guard_tokens_and_full_outputs_but_not_to_the_screen(ctx, monkeypatch):
    from src.agent import result_agent

    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal)
    revision_id = PlanRepo(ctx.conn).get_current_revision(uuid.UUID(list_id))["id"]
    long_output = "추가 금액 안에서 올릴 수 있는 부품: " + "가" * 400   # trace(160자)보다 길다

    def fake_run_turn(conn, rev, result, text, user_id=None):
        return result_agent.TurnResult(
            reply="가드가 바꾼 코드 문장", result=result, changed=False, intent="ask",
            trace=[f"upgrade_options(extra='') → {long_output[:160]}", "check_build() → 문제 없음"],
            outputs=[long_output, "문제 없음"], guard={"numbers": ["999"], "words": []},
            usage={"input": 1200, "output": 80, "total": 1280, "calls": 2})

    monkeypatch.setattr(result_agent, "available", lambda: True)
    monkeypatch.setattr(result_agent, "run_turn", fake_run_turn)
    turn = recommendation_service.handle_result_message(ctx.conn, revision_id, "남는 돈으로 뭐 올려?")

    log = _last_turn_log(ctx.conn, list_id)
    assert log["path"] == "agent" and log["intent"] == "ask"
    assert log["tools"] == ["upgrade_options", "check_build"]
    assert log["outputs"][0] == long_output                       # 잘리지 않은 원문
    assert log["guard"] == {"numbers": ["999"], "words": []}
    assert log["tokens"] == {"input": 1200, "output": 80, "total": 1280, "calls": 2}
    assert "turn" not in turn and set(turn) == {"reply", "result"}   # API 응답에는 섞이지 않는다
    state = session_service.get_session_state(ctx.conn, uuid.UUID(list_id), principal)
    assert all("metadata" not in m and "turn" not in m for m in state["messages"])   # 복원 화면에도


def test_agent_failure_logs_the_rule_fallback_and_the_error_name(ctx, monkeypatch):
    from src.agent import result_agent

    principal = ctx.signup(_unique_email())
    list_id = ctx.recommended_list(principal)
    revision_id = PlanRepo(ctx.conn).get_current_revision(uuid.UUID(list_id))["id"]

    def broken_run_turn(*args, **kwargs):
        raise TimeoutError("모델 응답 없음")

    monkeypatch.setattr(result_agent, "available", lambda: True)
    monkeypatch.setattr(result_agent, "run_turn", broken_run_turn)
    turn = recommendation_service.handle_result_message(ctx.conn, revision_id, "그래픽카드 더 저렴한 걸로")

    assert turn["reply"]                                          # 규칙 경로가 답했다
    log = _last_turn_log(ctx.conn, list_id)
    assert log["path"] == "rules_after_agent_error" and log["error"] == "TimeoutError"


def test_usage_of_reads_strands_accumulated_usage():
    from types import SimpleNamespace

    from src.agent.result_agent import usage_of

    metrics = SimpleNamespace(cycle_count=3, accumulated_usage={
        "inputTokens": 2100, "outputTokens": 150, "totalTokens": 2250, "cacheReadInputTokens": 1024})
    assert usage_of(SimpleNamespace(event_loop_metrics=metrics)) == {
        "input": 2100, "output": 150, "total": 2250, "calls": 3, "cache_read": 1024}
    empty = SimpleNamespace(cycle_count=0, accumulated_usage={"inputTokens": 0, "outputTokens": 0, "totalTokens": 0})
    assert usage_of(SimpleNamespace(event_loop_metrics=empty)) is None    # 사용량을 안 주는 모델
    assert usage_of(object()) is None
