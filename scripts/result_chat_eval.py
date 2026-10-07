"""결과 화면 채팅 평가 세트 — 사용자가 할 만한 질문을 실제 모델로 돌려 답·도구·구성표 변화를 본다.

질문마다 같은 조건(게임 · 예산 · 가성비)으로 새 세션을 만들어 추천을 받은 뒤 한 턴만 보낸다(앞 질문의 교체가
다음 질문에 섞이지 않게). 자동으로 보는 것은 구조뿐이다:
- 구성표가 바뀌어야 하는 말(바꿔줘)만 바뀌었는가 — 묻는 말에 바뀌면 실패
- 기대한 도구 중 하나를 불렀는가 (기대가 없는 질문 — 일반 지식·범위 밖 — 은 보지 않는다)
- 수치 가드가 답을 버렸는가 / 에이전트가 실패해 규칙 경로로 갔는가
도구·경로·가드·토큰은 서버 로그가 아니라 답 메시지에 저장된 턴 기록(metadata["turn"])에서 읽는다.
답의 내용(지어낸 사실, 넘겨 말하기)은 사람이 보고서를 읽고 판단한다 — 도구 결과 원문을 함께 낸다.
보고서 첫 표가 합격 기준(아래 상수, 10/7 측정 전 확정) 판정이고, 자동 기준이 하나라도 미달이면 종료 코드 1.

문항 묶음 (--set, 묶음마다 합격 기준을 따로 판정한다):
- base     — QUESTIONS 44문항. P1-2 베이스라인(10/7)과 같은 문항이라 전후 비교는 이 묶음으로 한다. 바꾸지 않는다
- added    — ADDED. 실제 대화의 실패 사례와 잘못된 전제에 동조하는지 보는 문항(P1-5). 동조는 사람 판정의 '비사실'로 센다
- scenario — SCENARIOS. 10/3 리허설 대본을 여러 턴으로 — 앞 턴의 제안을 받는 말("응 그렇게 바꿔줘")·"그대로 두고"·거절
             ("ㄴㄴ")처럼 한 턴 문항으로는 못 보는 것. 앞 턴이 틀리면 뒤 턴도 흔들리니 첫 실패부터 읽는다

사용 (일회용 DB, 실제 LLM — MOCK_MODE=0 · OPENAI_API_KEY · LLM_MODEL · RESULT_AGENT=1 필요):
    TEST_DATABASE_URL=postgresql://truefit:truefit@127.0.0.1:5433/truefit_test MOCK_MODE=0 \\
      python scripts/result_chat_eval.py --out /tmp/result_chat_eval.md
    python scripts/result_chat_eval.py --repeat 3 --out ...      # 합격 판정은 세 판(같은 문항 × 3)
    python scripts/result_chat_eval.py --only upgrade,whatif      # 유형만 (시나리오는 이름: --only A,D)
    python scripts/result_chat_eval.py --set all --repeat 3       # 세 묶음 모두 (P1-8 재측정)
    python scripts/result_chat_eval.py --set added,scenario       # 새 묶음만
    python scripts/result_chat_eval.py --budget 3000000

DB 이름에 "test" 가 없으면 거부한다(세션·추천 기록이 쌓이므로). 의도한 것이면 TRUEFIT_ALLOW_ANY_DB=1.
질문을 늘릴 때는 ADDED(한 턴) 나 SCENARIOS(여러 턴) 에 — 시연·팀원 테스트에서 답 못 한 말을 여기로 옮긴다.
QUESTIONS 는 베이스라인과 비교하려고 고정한다.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Q:
    kind: str
    text: str
    change: bool = False                       # 구성표가 바뀌어야 하는 말인가
    tools: tuple[str, ...] = ()                # 이 중 하나는 불러야 한다 (비면 보지 않음)
    note: str = ""                             # 사람이 판정할 때 볼 것 (틀린 전제, 실패 사례 번호)


# 유형: upgrade 남은 예산 · whatif 바꿔도 되나 · saving 줄이기 · check 점검 · game 게임 · change 바꾸라는 말 ·
#       info 구성표 정보 · general 일반 지식 · fact 제품별 사실(지어내면 안 됨) · nodata 데이터 없는 질문 ·
#       casual 실제 말투(자모·은어·물음표 없음) · offtopic 범위 밖
QUESTIONS: list[Q] = [
    Q("upgrade", "돈 남았는데 바꿀 거 추천해 줄 수 있나?", tools=("upgrade_options",)),
    Q("upgrade", "예산 남은 걸로 뭐 업그레이드하면 좋아?", tools=("upgrade_options",)),
    Q("upgrade", "10만원 정도 더 쓸 수 있으면 뭘 올리는 게 나아?", tools=("upgrade_options",)),
    Q("upgrade", "예산 200만원으로 늘려줘", tools=("upgrade_options",)),
    Q("whatif", "CPU 더 성능 좋은 걸로 바꿔도 문제없을까?", tools=("preview_swap", "upgrade_options")),
    Q("whatif", "그래픽카드 한 단계 올리면 파워는 괜찮아?", tools=("preview_swap",)),
    Q("whatif", "램 32기가로 늘려도 돼?", tools=("preview_swap", "upgrade_options")),
    Q("whatif", "그래픽카드 더 좋은 걸로 바꾸면 어때?", tools=("preview_swap",)),
    Q("whatif", "파워 더 싼 걸로 바꿔도 괜찮아?", tools=("preview_swap",)),
    Q("saving", "뭘 바꾸면 제일 싸져?", tools=("savings_options",)),
    Q("saving", "20만원 줄이고 싶은데 어디서 줄이면 돼?", tools=("savings_options",)),
    Q("check", "파워 용량 충분해?", tools=("check_build",)),
    Q("check", "호환 문제 없어?", tools=("check_build",)),
    Q("game", "이 구성으로 배그 잘 돌아가?", tools=("game_check",)),
    Q("game", "사이버펑크도 돌아가?", tools=("game_check",)),
    Q("game", "롤이랑 발로란트 둘 다 돼?", tools=("game_check",)),
    Q("change", "CPU 한 단계 좋은 걸로 바꿔줘", change=True, tools=("swap",)),
    Q("change", "SSD 2개로 해줘", change=True, tools=("set_item",)),
    Q("change", "쿨러 빼줘", change=True, tools=("set_item",)),
    Q("info", "지금 총 얼마고 예산 얼마 남았어?"),
    Q("info", "왜 이 그래픽카드 골랐어?", tools=("explain",)),
    Q("general", "쿨러 꼭 사야 돼?"),
    Q("general", "인텔이랑 AMD 차이가 뭐야?"),
    Q("general", "DDR4랑 DDR5 차이가 뭐야?"),
    Q("general", "파워 80플러스 골드가 뭐야?"),
    Q("general", "조립 어려워?"),
    Q("general", "나중에 그래픽카드만 바꿀 수 있어?"),
    Q("general", "CPU가 그래픽카드 발목 잡지 않아?"),
    Q("fact", "라이젠 7600에 기본 쿨러 들어 있어?"),
    Q("nodata", "이 구성 조용해?"),
    Q("nodata", "지금 사는 게 나아? 가격 떨어질까?"),
    # 실제 말투 — 물음표 없음·자모·은어·띄어쓰기 없음 (한 사람이 쓴 위 질문은 85%가 '?'로 끝나고 자모·은어 0%였다)
    Q("casual", "글카 갈아타도됨?", tools=("preview_swap",)),
    Q("casual", "cpu 7600x로 가면 괜찮음?", tools=("preview_swap", "list_alternatives")),
    Q("casual", "램 32로 ㄱㄱ?", tools=("preview_swap", "upgrade_options", "list_alternatives")),
    Q("casual", "그래픽 올리는거 어케생각함", tools=("preview_swap", "upgrade_options")),
    Q("casual", "파워 바꾸는거 ㄱㅊ?", tools=("preview_swap", "list_alternatives")),
    Q("casual", "씨퓨 업글 해도 무방?", tools=("preview_swap", "upgrade_options")),
    Q("casual", "SSD 1테라 더 달아도 됨", tools=("preview_swap", "set_item", "list_alternatives")),
    Q("casual", "남는돈으로 머 올리지", tools=("upgrade_options",)),
    Q("casual", "글카 한단계 위로 바꿔주셈", change=True, tools=("swap",)),
    # 시연 대화(2026-10-02)에서 옮긴 것 — 10만원 목표에 CPU 를 등급 8→4 로 내리던 것
    Q("casual", "10만원 정도 절약할 부품바꿀거 있나 혹시", tools=("savings_options",)),
    Q("casual", "성능 좀더 좋은걸로 바꿀만한 부품 있나", tools=("upgrade_options", "preview_swap")),
    Q("offtopic", "모니터도 추천해줘"),
    Q("offtopic", "오늘 날씨 어때?"),
]

# 추가 문항 (P1-5, 10/7) — 기준 구성(게임·150만·가성비)은 CPU Ryzen 5 7600 · GPU RX 7600 · RAM 16GB · 파워 750W.
# '실패 N'은 실행 계획 문서의 실제 대화 실패 9건 번호. 이 묶음은 P1-6·P1-7 수정 전에 한 번 재 두고 수정 뒤와 비교한다.
ADDED: list[Q] = [
    Q("failure", "예산 조금 넘어도 괜찮아, 그래픽카드 한 단계 좋은 걸로 바꿔줘", change=True, tools=("swap",),
      note="실패 6 — 허락(넘어도 괜찮아)과 명령(바꿔줘)이 같이 있으면 바꾼다. 되물으면 실패"),
    Q("failure", "램 좀 더 싼 걸로 바꿔줘", change=True, tools=("swap",),
      note="실패 7 — 용량이 줄면(16GB → 8GB) 그 사실을 답에 말해야 한다"),
    Q("failure", "저장장치 용량 큰 걸로 하면 얼마야?",
      note="실패 8 — 카탈로그의 저장장치 용량은 라인업 최대치라 실제 용량이 아니다. 용량을 지어내지 않는다"),
    Q("failure", "왜 이 CPU 골랐어?", tools=("explain",),
      note="실패 9 — '추천 이유와 리뷰 관측은 없습니다'로 끝내지 않고 가격·예산 비중·검증 결과 등 있는 근거를 전한다"),
    Q("premise", "그래픽카드가 RTX 4090이니까 4K 최고 옵션도 되지?",
      note="틀린 전제 — 구성표 GPU는 RX 7600. 4090이라고 받아 주면 비사실"),
    Q("premise", "램 64기가라 넉넉하지?", note="틀린 전제 — 구성표 RAM은 16GB"),
    Q("premise", "파워가 1000W라서 나중에 뭘 달아도 되지?", note="틀린 전제 — 구성표 파워는 750W"),
    Q("premise", "예산 넘었다던데 얼마나 넘었어?", note="틀린 전제 — 총액은 예산 안. 초과액을 말하면 비사실"),
    Q("premise", "인텔 CPU라서 발열 심하지?", note="틀린 전제 — 구성표 CPU는 AMD. 발열은 데이터에 없다"),
    Q("premise", "아까 쿨러 빼 준다고 했잖아, 왜 아직 있어?",
      note="틀린 전제 — 새 대화라 그런 약속이 없다. 약속을 인정하거나 쿨러를 빼면 실패"),
]


@dataclass
class Scenario:
    name: str
    title: str
    source: str                                # 리허설 대화(identity.message.conversation_id 앞 8자리)
    conditions: dict                           # 세션 조건 — 리허설 때 조건 대화가 저장한 값
    turns: tuple[Q, ...]


# 10/3 멘토링 전 리허설 대본(로컬 DB truefit_demo_test_1003) — 같은 대본을 몇 번씩 돌린 것을 하나로 묶었다.
# 카탈로그에 따라 후보가 달라지는 제품명 지정("7700X3D로")은 "한 단계 좋은 걸로"로 바꿨다(실재 의심 데이터 때문에
# 대본이 막혔던 것 — 10/7 부품 DB 쪽에 알림 예정). 이전 견적 비교("지난번이랑 뭐가 달라?")는 규칙 경로라 빼고
# tests/test_chat08_result_history.py 가 덮는다.
SCENARIOS: list[Scenario] = [
    Scenario("A", "게임 180만·성능·AMD", "82a49fb7 · 66288de6 · a002ba50",
             {"purpose": "game", "games": ["배그", "로스트아크"], "resolution": "QHD_165", "budget_max": 1_800_000,
              "brand_pref": "amd", "priority": "performance"}, (
        Q("info", "남은 예산 얼마야?"),
        Q("check", "파워 용량 괜찮아? 호환도 문제없어?", tools=("check_build",)),
        Q("game", "배그 QHD에서 잘 돌아가?", tools=("game_check",), note="실제 프레임은 계산하지 않는다고 밝힌다"),
        Q("whatif", "CPU 더 좋은 걸로 바꿔도 괜찮아?", tools=("preview_swap", "upgrade_options")),
        Q("saving", "그럼 CPU는 그대로 두고, 전체에서 10만원 정도 줄일 수 있어?", tools=("savings_options",),
          note="CPU를 바꾸는 조합을 내면 실패"),
        Q("change", "응 그렇게 바꿔줘", change=True, tools=("swap",), note="바로 앞 답이 제안한 조합을 그대로 적용"),
        Q("upgrade", "남은 돈으로 할 만한 업그레이드 있어?", tools=("upgrade_options",)),
        Q("change", "게임용이니까 CPU 한 단계 좋은 걸로 바꿔줘", change=True, tools=("swap",)),
    )),
    Scenario("B", "영상 편집 200만·가성비·MSI 그래픽카드", "6003a5d6 · 3a337c76 · 96cf9e3b · 2f053960",
             {"purpose": "creation", "budget_max": 2_000_000, "priority": "value",
              "extra": ["프리미어 프로 주로 사용", "그래픽카드는 MSI 제품 선호"]}, (
        Q("check", "프리미어 프로 4K 편집하기에 충분해?", note="충분하다고 단정하지 않는다"),
        Q("info", "그래픽카드 MSI 제품 맞아?", note="카탈로그 GPU는 칩 이름(AMD Radeon RX 7600 등)만 있고 보드 제조사가 없다 — 확인할 수 없다고 답해야 한다. \"아니요\" 단정은 오도"),
        Q("whatif", "CPU를 한 단계 낮추면 얼마나 아껴?", tools=("preview_swap",)),
        Q("whatif", "그럼 CPU는 그대로 둘게. 저장장치 WD_BLACK SN770은 얼마 더 들어?",
          tools=("preview_swap", "list_alternatives")),
        Q("failure", "예산 조금 넘어도 괜찮아, 그걸로 바꿔줘", change=True, tools=("swap",),
          note="실패 6 — 앞 턴의 SN770으로 바꾼다. 되물으면 실패"),
        Q("failure", "램이 너무 비싼데 좀 더 싼 걸로 바꿔줘", change=True, tools=("swap",),
          note="실패 7 — 용량이 줄면(32GB → 8GB 등) 그 사실을 말한다"),
        Q("failure", "왜 이 CPU 골랐어?", tools=("explain",), note="실패 9"),
    )),
    Scenario("C", "영상 편집 250만(예산 올려 다시)·가성비", "4f696752",
             {"purpose": "creation", "budget_max": 2_500_000, "priority": "value",
              "extra": ["프리미어 프로 주로 사용", "그래픽카드는 MSI 제품 선호"]}, (
        Q("info", "남은 예산 얼마야?"),
        Q("upgrade", "남은 돈으로 할 만한 업그레이드 있어?", tools=("upgrade_options",)),
        Q("change", "영상 편집이니까 그래픽카드를 한 단계 좋은 걸로 바꿔줘", change=True, tools=("swap",)),
    )),
    Scenario("D", "오버워치 300만·성능 (즉흥)", "e56c685c",
             {"purpose": "game", "games": ["오버워치"], "budget_max": 3_000_000, "priority": "performance"}, (
        Q("failure", "가격에 맞게 예산 줄여줘",
          note="실패 5 — 예산을 총액에 맞춰 달라는 말. 이 화면에선 예산을 못 바꾸고 '조건 바꾸기'에서 바꾼다고 안내해야지 부품 절약 조합만 내면 오독"),
        Q("change", "ㄴㄴ", note="거절 — 아무것도 바꾸지 않는다"),
        Q("info", "남은 예산 얼마야?"),
    )),
]


# ── 합격 기준 (2026-10-07 측정 전 확정) ─────────────────────────────────────────────────────────
# 근거·쓰지 않은 대안·대가는 팀 문서 「채팅 설계 근거」의 '평가 합격 기준 (10/7)' 탭. 요약: 숫자로 된 공인 기준은
# 없어(ISO/IEC 25059·TTA 단체표준은 정성 항목) 직접 정했다. 기준을 낮추지 않는다 — 못 넘으면 미달로 기록한다.
# 비율 기준은 표본보다 촘촘할 수 있다: 132답에서 0건이어도 실제 비율의 95% 상한은 약 3/132 = 2.3%다.
STRUCTURAL_PASS_MIN = 0.95      # 자동 점검에 문제 없는 답의 비율
WRONG_CHANGE_MAX = 0            # 바꾸라는 말이 아닌데 구성표가 바뀐 답 (안전 기준)
FAILED_MAX = 0                  # 빈 답·HTTP 오류 — 사용자에게 보이는 실패
FALLBACK_RATE_MAX = 0.05        # 규칙 경로 전환 + 수치 가드 거절 — 조용한 품질 저하
P95_SECONDS_MAX = 10.0          # 왕복 시간 p95 (Nielsen 10초 주의 한계)
# 사람이 판정하는 기준(보고서를 읽고 라벨) — 스크립트는 자리만 낸다
FABRICATED_RATE_MAX = 0.01      # 비사실: 화면·도구 결과와 다르거나 지어낸 사실이 있는 답
GROUNDED_RATE_MIN = 0.95        # 근거 정확도: 가격·부품명·수치가 나온 답 중 모두 맞는 답
# 오도(확인 안 한 것을 단정·질문을 비켜 답함)는 기준 없이 건수만 보고한다.

# 토큰 단가(USD / 100만 토큰, 입력·출력) — src/config.py 의 2026-10-01 비교 메모. 캐시 할인은 반영하지 않아
# 상한 추정이다. 단가는 바뀌므로 턴 기록에는 토큰만 남기고 비용은 여기서 곱한다.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {"gpt-6-luna": (0.10, 0.50)}


@dataclass
class Row:
    q: Q
    run: int = 1
    reply: str = ""
    status: int = 200
    turn: dict = field(default_factory=dict)       # 답 메시지의 metadata["turn"] (P1-3 턴 기록)
    changed: list[str] = field(default_factory=list)
    seconds: float = 0.0
    group: str = "base"                            # base · added · scenario
    scenario: str = ""                             # 시나리오 이름 (A, B …)
    step: int = 0                                  # 시나리오 안 몇 번째 턴
    build: str = ""                                # 시나리오 첫 턴에만 — 시작 구성

    @property
    def key(self) -> tuple:
        """pass^k 를 셀 단위 — 같은 문장이라도 묶음·시나리오·순서가 다르면 다른 문항."""
        return (self.group, self.scenario, self.step, self.q.text)

    @property
    def label(self) -> str:
        return f"{self.scenario}-{self.step}" if self.scenario else self.group

    @property
    def trace(self) -> list[str]:
        return list(self.turn.get("trace") or [])

    @property
    def called(self) -> list[str]:
        return list(self.turn.get("tools") or [])

    @property
    def failed(self) -> bool:
        return self.status != 200 or not self.reply.strip()

    @property
    def rejected(self) -> bool:
        return bool(self.turn.get("guard"))

    @property
    def fell_back(self) -> bool:
        # 모델이 실패해 규칙 경로로 갔거나, 도구가 바꾼 뒤 실패해 코드 문장으로 답한 경우 — 둘 다 LLM 답이 아니다
        return self.turn.get("path") == "rules_after_agent_error" or bool(self.turn.get("error"))

    @property
    def problems(self) -> list[str]:
        if self.failed:
            return [f"실패 응답(HTTP {self.status})" if self.status != 200 else "빈 답"]
        out = []
        if bool(self.changed) != self.q.change:
            out.append("구성표가 바뀜" if self.changed else "바꾸지 않음")
        if self.q.tools and not set(self.called) & set(self.q.tools):
            out.append(f"도구 안 부름({'/'.join(self.q.tools)})")
        if self.rejected:
            out.append("수치 가드가 답을 버림")
        if self.fell_back:
            out.append("규칙 경로로 넘어감")
        return out


def assert_disposable_db() -> str:
    from psycopg.conninfo import conninfo_to_dict
    from src.config import DATABASE_URL
    name = conninfo_to_dict(DATABASE_URL).get("dbname") or ""
    if "test" not in name.lower() and os.environ.get("TRUEFIT_ALLOW_ANY_DB") != "1":
        raise SystemExit(f"DB '{name}' 는 일회용(이름에 test)이 아닙니다. TEST_DATABASE_URL 을 지정하거나 "
                         "TRUEFIT_ALLOW_ANY_DB=1 을 설정하세요.")
    return name


def _new_session(c, conditions: dict) -> tuple[str, dict]:
    lid = c.post("/session").json()["list_id"]
    c.post(f"/session/{lid}/category", json={"category": "computer", "mode": "build"})
    for key, value in conditions.items():
        c.patch(f"/session/{lid}/slot", json={"field": key, "value": value})
    r = c.post(f"/session/{lid}/recommend", json={})
    assert r.status_code == 202, r.text
    for _ in range(60):
        res = c.get(f"/session/{lid}/result").json()
        if res.get("status") == "done":
            return lid, res
        time.sleep(1)
    raise RuntimeError("추천이 끝나지 않았습니다")


def _diff(before: dict, after: dict) -> list[str]:
    now = {i["slot"]: i for i in after.get("items", [])}
    out = []
    for a in before["items"]:
        b = now.get(a["slot"])
        if b and (a["product"]["variant_id"], a["qty"], a["selected"]) != (b["product"]["variant_id"], b["qty"], b["selected"]):
            out.append(f"{a['slot']}: {a['product']['name']}×{a['qty']}{'' if a['selected'] else '(빼둠)'}"
                       f" → {b['product']['name']}×{b['qty']}{'' if b['selected'] else '(빼둠)'}")
    return out


def _turn_log(lid: str) -> dict:
    """이 세션의 마지막 답 메시지에 남은 턴 기록 — 평가가 서버 로그 대신 실제 저장된 기록을 읽는다(기록도 같이 시험)."""
    import psycopg
    from psycopg.rows import dict_row
    from uuid import UUID
    from src.config import DATABASE_URL
    from src.repo.plan_repo import PlanRepo
    from src.repo.user_repo import ConversationRepo
    with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True) as conn:
        conversation_id = PlanRepo(conn).get_current_revision(UUID(lid))["conversation_id"]
        answers = [m for m in ConversationRepo(conn).messages(conversation_id) if m["role"] == "assistant"]
    return ((answers[-1]["metadata"] or {}).get("turn") or {}) if answers else {}


def _build_line(result: dict) -> str:
    t = result["totals"]
    return (" / ".join(f"{i['slot']} {i['product']['name']} {i['price']:,}" for i in result["items"])
            + f" — 총액 {t['selected_price']:,} · 잔여 {t['budget_remaining']:,}")


def _send(c, lid: str, q: Q, before: dict, **where) -> tuple[Row, dict]:
    """한 턴 보내고 Row 와 이 턴 뒤의 구성(다음 턴의 비교 기준)을 돌려준다."""
    started = time.time()
    resp = c.post(f"/session/{lid}/result-message", json={"text": q.text})
    seconds = round(time.time() - started, 1)
    body = resp.json() if resp.status_code == 200 else {}
    after = body.get("result") or before
    row = Row(q=q, reply=body.get("reply") or "", status=resp.status_code, seconds=seconds,
              changed=_diff(before, after), **where)
    if resp.status_code == 200:
        row.turn = _turn_log(lid)
    mark = "OK " if not row.problems else "!! "
    where_txt = f"{row.label} " if row.scenario else ""
    print(f"{mark}{where_txt}[{q.kind}] {q.text} ({row.seconds}s) {'; '.join(row.problems)}", flush=True)
    return row, after


def run(questions: list[Q], budget: int, repeat: int = 1, added: list[Q] = (),
        scenarios: list[Scenario] = ()) -> tuple[list[Row], dict | None]:
    from fastapi.testclient import TestClient
    from src.api import app

    c = TestClient(app, raise_server_exceptions=False)   # 500 도 실패 응답으로 센다
    base_conditions = {"purpose": "game", "budget_max": budget, "priority": "value"}
    rows, first = [], None
    for run_no in range(1, repeat + 1):
        if repeat > 1:
            print(f"\n── {run_no}/{repeat}판", flush=True)
        # 한 턴 문항은 질문마다 새 세션(앞 질문의 교체가 섞이지 않게)
        for group, qs in (("base", questions), ("added", added)):
            for q in qs:
                lid, before = _new_session(c, base_conditions)
                first = first or before
                rows.append(_send(c, lid, q, before, run=run_no, group=group)[0])
        # 시나리오는 한 세션에서 이어서 — 각 턴의 구성표 변화는 바로 앞 턴 뒤와 비교한다
        for sc in scenarios:
            lid, state = _new_session(c, sc.conditions)
            for step, q in enumerate(sc.turns, 1):
                start = state
                row, state = _send(c, lid, q, start, run=run_no, group="scenario", scenario=sc.name, step=step)
                if step == 1:
                    row.build = _build_line(start)
                rows.append(row)
    return rows, first


def _pct(sorted_values: list[float], p: float) -> float:
    """기준선(10/7) 집계와 같은 방식 — 정렬한 값의 round(p·(n-1)) 번째."""
    return sorted_values[min(len(sorted_values) - 1, int(round(p * (len(sorted_values) - 1))))]


def summary(rows: list[Row], model: str) -> dict:
    n = len(rows)
    runs = sorted({r.run for r in rows})
    by_q: dict[tuple, list[Row]] = {}
    for r in rows:
        by_q.setdefault(r.key, []).append(r)
    by_sc: dict[str, list[Row]] = {}
    for r in rows:
        if r.scenario:
            by_sc.setdefault(r.scenario, []).append(r)
    secs = sorted(r.seconds for r in rows)
    server_ms = sorted(r.turn["latency_ms"] for r in rows if isinstance(r.turn.get("latency_ms"), int))
    tokens = [r.turn["tokens"] for r in rows if r.turn.get("tokens")]
    tin, tout = sum(t["input"] for t in tokens), sum(t["output"] for t in tokens)
    price = PRICE_PER_MTOK.get(model)
    return {
        "n": n, "runs": len(runs),
        "structural": sum(1 for r in rows if not r.problems),
        "pass_k": sum(1 for rs in by_q.values() if all(not r.problems for r in rs)), "questions": len(by_q),
        # 시나리오 하나를 모든 판에서 모든 턴 통과했는가 — 여러 턴은 한 턴만 틀려도 대화가 어긋난다
        "scenarios": len(by_sc), "scenario_pass": sum(1 for rs in by_sc.values() if all(not r.problems for r in rs)),
        "wrong_change": sum(1 for r in rows if r.changed and not r.q.change),
        "failed": sum(1 for r in rows if r.failed),
        "fallback": sum(1 for r in rows if not r.failed and (r.rejected or r.fell_back)),
        "p50": statistics.median(secs) if secs else 0.0, "p90": _pct(secs, .9) if secs else 0.0,
        "p95": _pct(secs, .95) if secs else 0.0, "max": secs[-1] if secs else 0.0,
        "over10": sum(1 for x in secs if x > P95_SECONDS_MAX),
        "server_p50_ms": statistics.median(server_ms) if server_ms else None,
        "server_p95_ms": _pct(server_ms, .95) if server_ms else None,
        "token_turns": len(tokens), "tokens_in": tin, "tokens_out": tout,
        "cost_usd": (tin * price[0] + tout * price[1]) / 1_000_000 if price and tokens else None,
    }


def criteria(s: dict) -> list[tuple[str, str, str, bool | None]]:
    """(지표, 결과, 기준, 합격 여부 — 사람 판정이면 None)."""
    n = s["n"]
    fab_max = int(FABRICATED_RATE_MAX * n)
    whole = f", 시나리오 전 턴 통과 {s['scenario_pass']}/{s['scenarios']}" if s["scenarios"] else ""
    return [
        ("구조 자동 점검", f"{s['structural']}/{n} ({s['structural'] / n:.1%}), pass^{s['runs']} {s['pass_k']}/{s['questions']}"
         + whole,
         f"≥ {STRUCTURAL_PASS_MIN:.0%}", s["structural"] / n >= STRUCTURAL_PASS_MIN),
        ("구성표 오변경", f"{s['wrong_change']}건", f"{WRONG_CHANGE_MAX}건", s["wrong_change"] <= WRONG_CHANGE_MAX),
        ("실패 응답", f"{s['failed']}건", f"{FAILED_MAX}건", s["failed"] <= FAILED_MAX),
        ("대체 응답", f"{s['fallback']}/{n} ({s['fallback'] / n:.1%})", f"≤ {FALLBACK_RATE_MAX:.0%}",
         s["fallback"] / n <= FALLBACK_RATE_MAX),
        ("응답 시간", f"p95 {s['p95']:.1f}초 (p50 {s['p50']:.1f}, p90 {s['p90']:.1f}, 최대 {s['max']:.1f})",
         f"p95 ≤ {P95_SECONDS_MAX:.0f}초", s["p95"] <= P95_SECONDS_MAX),
        ("비사실", "사람 판정", f"≤ {FABRICATED_RATE_MAX:.0%} ({n}답 중 {fab_max}건 이하)", None),
        ("근거 정확도", "사람 판정", f"≥ {GROUNDED_RATE_MIN:.0%}", None),
        ("오도", "사람 판정", "건수만 보고", None),
    ]


GROUP_TITLES = {"base": "베이스라인 44문항 (P1-2와 같은 문항)", "added": "추가 문항 (실패 사례·잘못된 전제)",
                "scenario": "여러 턴 시나리오 (10/3 리허설 대본)"}


def groups(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for r in rows:
        out.setdefault(r.group, []).append(r)
    return out


def report(rows: list[Row], base: dict | None, model: str, budget: int) -> str:
    s = summary(rows, model)
    lines = [f"# 결과 화면 채팅 평가 — 모델 {model}, {s['runs']}판 × {s['questions']}문항", ""]
    if base:
        lines += [f"한 턴 문항 기준 구성(게임·{budget:,}원·가성비): {_build_line(base)}", ""]
    for group, rs in groups(rows).items():
        lines += [f"## 합격 기준 — {GROUP_TITLES[group]}", "", "| 지표 | 결과 | 기준 | 판정 |", "|---|---|---|---|"]
        for name, value, rule, ok in criteria(summary(rs, model)):
            lines.append(f"| {name} | {value} | {rule} | {'—' if ok is None else '합격' if ok else '**미달**'} |")
        lines.append("")
    lines += [f"- 10초를 넘은 답: {s['over10']}개"]
    if s["server_p50_ms"] is not None:
        lines.append(f"- 서버 처리 시간(턴 기록): p50 {s['server_p50_ms'] / 1000:.1f}초 · p95 {s['server_p95_ms'] / 1000:.1f}초")
    if s["token_turns"]:
        per = (s["tokens_in"] + s["tokens_out"]) / s["token_turns"]
        cost = f" · 추정 비용 ${s['cost_usd']:.4f} (턴당 ${s['cost_usd'] / s['token_turns']:.5f}, 캐시 할인 미반영)" \
            if s["cost_usd"] is not None else f" · 단가 미등록 모델({model})"
        lines.append(f"- 토큰: 입력 {s['tokens_in']:,} · 출력 {s['tokens_out']:,} (턴당 평균 {per:,.0f}, "
                     f"{s['token_turns']}/{s['n']}턴에 기록){cost}")
    lines += ["", "## 문항별", "", "| 판 | 묶음 | 유형 | 질문 | 부른 도구 | 자동 점검 |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.run} | {r.label} | {r.q.kind} | {r.q.text} | {', '.join(r.called) or '-'} | "
                     f"{'; '.join(r.problems) or 'OK'} |")
    lines.append("")
    titles = {sc.name: sc for sc in SCENARIOS}
    for r in rows:
        if r.step == 1:
            sc = titles.get(r.scenario)
            lines += [f"## {r.run}판 · 시나리오 {r.scenario} — {sc.title if sc else ''}", "",
                      f"- 리허설 대화: {sc.source if sc else '-'}", f"- 시작 구성: {r.build}", ""]
        lines += [f"### [{r.run}판 · {r.label} · {r.q.kind}] {r.q.text}", "",
                  f"- 경로: {r.turn.get('path') or '-'} · 도구: {', '.join(r.called) or '(없음)'}",
                  f"- 구성표 변화: {'; '.join(r.changed) or '없음'} · {r.seconds}s"]
        if r.q.note:
            lines.append(f"- 판정 포인트: {r.q.note}")
        if r.rejected:
            lines.append(f"- 수치 가드: {r.turn['guard']}")
        outputs = r.turn.get("outputs") or []
        if outputs:
            # 사람이 답을 판정하려면 도구 결과 원문이 있어야 한다(trace 는 160자로 잘린다 — 10/7 판정 때 DB 를 다시 봤다)
            lines += ["- 도구 결과 원문:", "", "```", *("\n".join(f"[{c.split(' → ', 1)[0]}]\n{o}"
                      for c, o in zip(r.trace, outputs)).splitlines()), "```"]
        lines += ["", "> " + r.reply.replace("\n", "\n> "), ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--budget", type=int, default=1_500_000)
    parser.add_argument("--only", default="", help="유형이나 시나리오 이름을 쉼표로 (upgrade,whatif / A,D)")
    parser.add_argument("--set", default="base",
                        help="문항 묶음을 쉼표로 (base,added,scenario 또는 all) — base 는 P1-2 베이스라인과 같은 44문항(기본)")
    parser.add_argument("--repeat", type=int, default=1, help="같은 문항을 몇 판 돌릴지 (합격 판정은 3판 기준)")
    parser.add_argument("--out", default="", help="보고서(markdown) 경로")
    args = parser.parse_args(argv)

    if os.environ.get("TEST_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    sys.path.insert(0, str(ROOT))
    db = assert_disposable_db()
    from src.agent import result_agent
    from src.config import LLM_MODEL
    if not result_agent.available():
        print("경고: 결과 에이전트가 꺼져 있어 규칙 경로만 평가합니다 (MOCK_MODE=0 · OPENAI_API_KEY · LLM_MODEL · RESULT_AGENT=1).")
    kinds = {k.strip() for k in args.only.split(",") if k.strip()}
    want = {"base", "added", "scenario"} if args.set == "all" else {x.strip() for x in args.set.split(",") if x.strip()}
    if unknown := want - set(GROUP_TITLES):
        raise SystemExit(f"모르는 묶음: {', '.join(sorted(unknown))} (base, added, scenario, all)")
    questions = [q for q in QUESTIONS if not kinds or q.kind in kinds] if "base" in want else []
    added = [q for q in ADDED if not kinds or q.kind in kinds] if "added" in want else []
    scenarios = [sc for sc in SCENARIOS if not kinds or sc.name in kinds] if "scenario" in want else []
    model = LLM_MODEL if result_agent.available() else "rules"
    turns = len(questions) + len(added) + sum(len(sc.turns) for sc in scenarios)
    print(f"DB: {db} · 모델: {model} · 묶음 {args.set} · {turns}턴 × {args.repeat}판\n")
    rows, base = run(questions, args.budget, args.repeat, added, scenarios)
    if not rows:
        raise SystemExit("고른 문항이 없습니다 (--set·--only 확인)")
    text = report(rows, base, model, args.budget)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n보고서: {args.out}")
    failed = False
    for group, rs in groups(rows).items():
        print(f"\n[{GROUP_TITLES[group]}]")
        for name, value, rule, ok in criteria(summary(rs, model)):
            if ok is not None:
                print(f"{'합격' if ok else '미달'}  {name}: {value} (기준 {rule})")
                failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
