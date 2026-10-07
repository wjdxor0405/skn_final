"""결과 화면 대화 에이전트 — Strands Agents SDK.

`POST /session/{id}/result-message` 의 자유 텍스트를 처리한다. 기존 규칙 경로(`handle_result_message`)는
"슬롯 이름 + 저렴/좋은" 만 알아듣고 최저가/최고가 후보로 바꿨다. 여기서는 LLM 이 구성표를 읽고
**기존 서비스 함수를 도구로** 불러 후보 조회·교체·담기/빼기·수량·구매 시점을 처리하고, "왜 이거?" 에는
저장된 근거(추천 이유·검증 쟁점·리뷰 관측)만 돌려준다.

경계:
- 도구는 `recommendation_service` 의 `list_alternatives`·`swap_item`·`patch_item` 을 감싼다. 최적화·검증·
  순위는 그대로 엔진 몫이고 에이전트는 무엇을 부를지만 고른다.
- 판정 금지 — "이 리뷰 조작인가", "이 부품이 더 좋은가" 를 에이전트가 정하지 않는다(결정 0001).
  `explain` 은 관측·쟁점·저장된 이유를 그대로 옮긴다.
- 교체·담기/빼기·수량 변경 뒤에는 서비스가 세트 전체의 호환 점검을 다시 돌린다(`reverify_set`). 도구 결과에
  그 점검이 찾은 문제(소켓·전력·크기·예산)를 적어 모델이 그대로 전하게 한다. 순위·요약 문장은 다시 만들지
  않으니 전체 재구성은 화면의 "다른 구성 보기".
- 도구는 순차 실행(`SequentialToolExecutor`) — 요청 스레드의 psycopg 연결 하나를 같이 쓴다.
- 대화는 `identity.message`에 저장된다(CHAT-08, 2026-09-27 — 이전엔 프로세스 메모리뿐이라 재시작하면
  사라졌다). 저장·복원은 `recommendation_service.handle_result_message`가 맡고, 여기서는 이번 run이
  생긴 뒤의 턴만 최근 N개 가져와 "두 번째 걸로" 같은 이어 말하기 맥락으로 쓴다(`_db_history`).
- `available()` 이 False(MOCK_MODE·키 없음·`RESULT_AGENT=0`)면 규칙 경로.
- 묻는 말("돈 남았는데 뭐 올릴까?", "바꿔도 문제없어?", "뭘 바꾸면 싸져?", "파워 충분해?", "배그 돌아가?")은
  저장하지 않는 계산 도구(preview_swap·upgrade_options·savings_options·check_build·game_check — 본체는
  `src/services/result_advice.py`)로 답한다. 바꾸는 도구는 바꾸라는 말(classify_intent == 'change')에서만 열린다(read_only).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from uuid import UUID

from src.agent.conditions_agent import SEARCH_PERMISSION_MARKER as _SEARCH_PERMISSION_MARKER
from src.agent.conditions_agent import _model, is_search_confirmation
from src.config import LLM_MODEL, LLM_PROVIDER, MOCK_MODE, OPENAI_API_KEY, RESULT_AGENT
from src.errors import NotFound, ServiceUnavailable

log = logging.getLogger(__name__)

_HISTORY_TURNS = 8


def available() -> bool:
    return (not MOCK_MODE and RESULT_AGENT and LLM_PROVIDER == "openai"
            and bool(OPENAI_API_KEY) and bool(LLM_MODEL))


def _won(n: int | None) -> str:
    if n is None:
        return "-"
    return f"{n:,}원"


# ── 세션: 도구가 공유하는 연결·상태 ────────────────────────────────────────
@dataclass
class ResultSession:
    conn: object
    revision_id: UUID
    result: dict                                  # get_stored_result 스냅샷 (도구가 바꾸면 갱신)
    user_id: UUID | None = None                    # 채팅 스왑도 선호 신호 소스로 잡히게(swap/patch에 전달)
    trace: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)   # 도구 결과 원문 — 수치 가드의 허용 근거(trace 는 160자로 자른다)
    changed: bool = False
    read_only: bool = False                        # 바꾸라는 말이 아니면(classify_intent) 이번 턴은 구성표를 바꾸지 않는다
    search_confirmed: bool = False                  # 직전 턴에 실시간 검색 동의를 구했고 이번 메시지가 동의인지(run_turn이 미리 판정)

    def _record(self, call: str, out: str) -> str:
        self.trace.append(f"{call} → {out[:160]}")
        self.outputs.append(out)
        return out

    def _refuse_write(self, call: str) -> str | None:
        """바꾸라는 말이 아닌데 바꾸는 도구를 부르면 거절한다 — 프롬프트만으로는 "램 32기가로 늘려도 돼?"에 수량을
        2로 바꿔 버렸다(2026-10-02 실측). 모델이 이 문장을 보고 가정 결과(preview_swap)로 답하게 한다."""
        if not self.read_only:
            return None
        return self._record(call, "거절: 이번 말에는 바꾸라는 요청(바꿔줘·해줘·~로·응)이 없어 구성표를 바꾸지 않았습니다. "
                                  "preview_swap 으로 가정 결과를 확인해 전하고, 바꿀지 사용자에게 물으세요.")

    def refresh(self) -> None:
        from src.services.recommendation_service import get_stored_result
        self.result = get_stored_result(self.conn, self.revision_id)

    def item(self, slot: str) -> dict | None:
        s = slot.strip()
        for it in self.result.get("items", []):
            if it["slot"].lower() == s.lower() or it["slot_label"] == s:
                return it
        return None

    def _perf_min_by_slot(self) -> dict[str, int]:
        from src.repo.plan_repo import PlanRepo
        full = PlanRepo(self.conn).load_full(self.revision_id)
        node_slot = {n["id"]: n["template_key"] for n in full.get("nodes", [])}
        out = {}
        for r in full.get("requirements", []):
            spec = r.get("match_spec") or {}
            if spec.get("perf_tier_min") is not None and r["node_id"] in node_slot:
                out[node_slot[r["node_id"]]] = int(spec["perf_tier_min"])
        return out

    # ── 도구 본체 ──
    def list_alternatives(self, slot: str) -> str:
        from src.services.recommendation_service import list_alternatives
        call = f"list_alternatives({slot!r})"
        it = self.item(slot)
        if it is None:
            return self._record(call, f"오류: '{slot}' 슬롯이 없습니다. 슬롯: {[i['slot'] for i in self.result['items']]}")
        rows = list_alternatives(self.conn, self.revision_id, UUID(it["item_id"]))["items"]
        if not rows:
            return self._record(call, f"{it['slot']}: 다른 후보 없음")
        from src.services import result_advice
        pmin = self._perf_min_by_slot().get(it["slot"])
        short = result_advice.requirement_shortfalls(self.conn, self.revision_id, it["slot"],
                                                     [r["candidate_id"] for r in rows])
        lines = [f"{it['slot']} 현재: {it['product']['name']} {_won(it['price'])}"
                 + (f" · 요구 성능 티어 ≥ {pmin}" if pmin is not None else "")]
        for i, r in enumerate(rows, 1):
            tier = None
            if r["product"].get("spec_summary", "") and "티어" in r["product"]["spec_summary"]:
                try:
                    tier = int(r["product"]["spec_summary"].split("티어")[1])
                except ValueError:
                    tier = None
            if (why := short.get(str(r["candidate_id"]))) is not None:
                flag = f" ⚠ 요구 사양 미달({why})"
            else:
                flag = " ⚠ 요구 사양 미달" if (pmin is not None and tier is not None and tier < pmin) else ""
            lines.append(f"{i}. candidate_id={r['candidate_id']} · {r['product']['name']} · {_won(r['price'])}"
                         f" ({r['price_delta']:+,}원) · {r['label']}"
                         + (f" · 성능 티어 {tier}" if tier is not None else "") + flag)
        return self._record(call, "\n".join(lines))

    def swap(self, slot: str, candidate_id: str) -> str:
        from src.services.recommendation_service import swap_item
        call = f"swap({slot!r}, {candidate_id!r})"
        if (refused := self._refuse_write(call)) is not None:
            return refused
        it = self.item(slot)
        if it is None:
            return self._record(call, f"오류: '{slot}' 슬롯이 없습니다.")
        try:
            cid = UUID(candidate_id)
        except ValueError:
            return self._record(call, "오류: candidate_id 는 list_alternatives 가 돌려준 값이어야 합니다.")
        from src.services import result_advice
        if (why := result_advice.requirement_shortfalls(self.conn, self.revision_id, it["slot"], [cid]).get(str(cid))):
            # 프롬프트 규칙("⚠ 후보로 바꾸지 않음")만으로는 모델이 8GB 램으로 바꿨다(10/7 수정 전 측정 2/6) — 도구가 막는다.
            # 사용자가 알고도 원하면 화면의 후보 목록에서 직접 고른다(그 경로는 막지 않는다).
            return self._record(call, (
                f"바꾸지 않음: 이 후보는 이 견적의 요구 사양에 못 미칩니다({why}). 사용자에게 그 사실을 알리고 요구를 채우는"
                " 후보(list_alternatives 에서 ⚠ 없는 것)를 권하세요. 그래도 그 부품을 원하면 구성표의 후보 목록에서 직접"
                " 고를 수 있다고 안내하세요."))
        before = it["product"]["name"], it["price"]
        try:
            self.result = swap_item(self.conn, self.revision_id, UUID(it["item_id"]), cid, user_id=self.user_id)
        except NotFound as exc:
            return self._record(call, f"오류: {exc}")
        self.changed = True
        after = self.item(it["slot"])
        t = self.result["totals"]
        return self._record(call, (
            f"{it['slot']} 교체: {before[0]} {_won(before[1])} → {after['product']['name']} {_won(after['price'])}"
            f" · 총액 {_won(t['selected_price'])} · 예산 잔여 {_won(t['budget_remaining'])}"
            + (" · ⚠ 예산 초과" if t["over_budget"] else "")
            + self._compat_note()))

    def _compat_note(self) -> str:
        """교체 뒤 다시 돌린 호환 점검이 찾은 문제(major)를 도구 결과에 싣는다 — 없으면 그 사실만."""
        # 예산 초과도 검증 쟁점(major)으로 오지만 호환 문제가 아니다 — 총액·잔여·'예산 초과'는 따로 적는다.
        # 섞어 두면 모델이 "호환 점검에 문제 표시가 있지만 근거가 없다"고 말했다(2026-10-02 실측).
        majors = [i["text"] for i in (self.result.get("verification") or {}).get("issues", [])
                  if i.get("severity") == "major" and i.get("axis") not in ("budget", "예산")]
        note = (" · ⚠ 호환 점검 문제: " + " / ".join(majors) if majors
                else " · 호환 점검을 교체 후 구성으로 다시 했고 확정된 문제는 없음(스펙을 모르는 부품은 확인 못 함)")
        from src.services import result_advice
        if result_advice.is_pc(self.conn, self.revision_id):
            from src.services.recommendation_service import _require_done_run
            from src.repo.engine_repo import EngineRepo
            stored = EngineRepo(self.conn).get_candidates(_require_done_run(self.conn, self.revision_id)[1]["id"])
            note += "".join(f" · {x}" for x in result_advice.cooler_lines(self.conn, stored) if x.startswith("⚠"))
        return note

    def set_item(self, slot: str, selected: str = "", qty: str = "") -> str:
        from src.services.recommendation_service import patch_item
        call = f"set_item({slot!r}, selected={selected!r}, qty={qty!r})"
        if (refused := self._refuse_write(call)) is not None:
            return refused
        it = self.item(slot)
        if it is None:
            return self._record(call, f"오류: '{slot}' 슬롯이 없습니다.")
        sel = None if selected == "" else selected.strip().lower() in ("true", "1", "yes")
        q = None
        if qty != "":
            if not qty.strip().isdigit() or not 1 <= int(qty) <= 99:
                return self._record(call, "오류: qty 는 1~99 정수")
            q = int(qty)
        if sel is None and q is None:
            return self._record(call, "오류: 바꿀 값이 없습니다")
        self.result = patch_item(self.conn, self.revision_id, UUID(it["item_id"]), selected=sel, qty=q, timing=None,
                                 user_id=self.user_id)
        self.changed = True
        after = self.item(it["slot"])
        t = self.result["totals"]
        return self._record(call, (
            f"{it['slot']}: selected={after['selected']} qty={after['qty']}"
            f" · 총액 {_won(t['selected_price'])} · 예산 잔여 {_won(t['budget_remaining'])}"
            + (" · ⚠ 예산 초과" if t["over_budget"] else "")
            + self._compat_note()))

    def explain(self, slot: str) -> str:
        from src.services import review_service
        call = f"explain({slot!r})"
        it = self.item(slot)
        if it is None:
            return self._record(call, f"오류: '{slot}' 슬롯이 없습니다.")
        lines = [f"{it['slot']} {it['product']['name']} {_won(it['price'])}"
                 + (f" · {it['product']['spec_summary']}" if it['product'].get('spec_summary') else "")
                 + (f" · 예산 비중 {it['budget_share']:.0%}" if it.get("budget_share") else "")]
        r = it.get("reason") or {}
        lines.append("저장된 추천 이유: " + (r.get("text") if r.get("status") == "ready" and r.get("text")
                                       else f"(없음 — 상태 {r.get('status')})"))
        if it["slot"] in ("CPU", "쿨러") and self.conn is not None:
            from src.services import result_advice
            if result_advice.is_pc(self.conn, self.revision_id):
                from src.services.recommendation_service import _require_done_run
                from src.repo.engine_repo import EngineRepo
                stored = EngineRepo(self.conn).get_candidates(_require_done_run(self.conn, self.revision_id)[1]["id"])
                lines += result_advice.cooler_lines(self.conn, stored)
        # engine.candidate_evidence 는 0012 에서 삭제됐다(engine_repo.get_candidate_evidence 는 옛 코드) — 인용은 못 낸다
        issues = (self.result.get("verification") or {}).get("issues") or []
        if issues:
            lines.append("세트 검증 쟁점(전체 구성 기준): " + " | ".join(f"[{i['axis']}] {i['text']}" for i in issues))
        try:
            s = review_service.get_summary(it["product"]["product_key"])
            obs = [x["text"] for x in s.summaries] if s.summaries else []
            lines.append("리뷰 관측(상품 단위, 개별 리뷰 진위 아님): " + (" | ".join(obs) if obs else s.data_notice))
        except NotFound:
            lines.append("리뷰 관측: 없음")
        return self._record(call, "\n".join(lines))

    # ── 묻는 말: 저장하지 않고 코드가 계산한 사실만 (src/services/result_advice.py) ──
    def _pc_only(self, call: str) -> str | None:
        from src.services import result_advice
        if result_advice.is_pc(self.conn, self.revision_id):
            return None
        return self._record(call, "이 도구는 PC 견적에서만 쓸 수 있습니다.")

    def preview_swap(self, slot: str, candidate_id: str = "", direction: str = "") -> str:
        from src.services import result_advice
        call = f"preview_swap({slot!r}, {candidate_id!r}, direction={direction!r})"
        if (no := self._pc_only(call)) is not None:
            return no
        it = self.item(slot)
        if it is None:
            return self._record(call, f"오류: '{slot}' 슬롯이 없습니다.")
        return self._record(call, result_advice.preview_swap(self.conn, self.revision_id, it["slot"],
                                                             candidate_id.strip() or None, direction.strip().lower() or None))

    def check_build(self) -> str:
        from src.services import result_advice
        call = "check_build()"
        if (no := self._pc_only(call)) is not None:
            return no
        return self._record(call, result_advice.check_build(self.conn, self.revision_id))

    def upgrade_options(self, extra: str = "", new_budget: str = "") -> str:
        from src.agent.conditions_agent import _parse_amount
        from src.services import result_advice
        call = f"upgrade_options(extra={extra!r}, new_budget={new_budget!r})"
        if (no := self._pc_only(call)) is not None:
            return no
        budget, total = None, None
        if new_budget.strip():
            total = _parse_amount(new_budget)
            if not total:
                return self._record(call, "오류: new_budget 은 금액(예: 2000000, 200만원)")
            if total - int((self.result.get("totals") or {}).get("selected_price") or 0) <= 0:
                return self._record(call, f"새 예산 {_won(total)}이 지금 총액 {_won((self.result.get('totals') or {}).get('selected_price'))} 이하라 올릴 여유가 없습니다.")
        elif extra.strip():
            budget = _parse_amount(extra)
            if not budget:
                return self._record(call, "오류: extra 는 금액(예: 100000, 10만원)")
        return self._record(call, result_advice.upgrade_options(self.conn, self.revision_id, budget, new_budget=total))

    def savings_options(self, target: str = "") -> str:
        from src.agent.conditions_agent import _parse_amount
        from src.services import result_advice
        call = f"savings_options(target={target!r})"
        if (no := self._pc_only(call)) is not None:
            return no
        amount = _parse_amount(target) if target.strip() else None
        return self._record(call, result_advice.savings_options(self.conn, self.revision_id, amount))

    def game_check(self, game: str) -> str:
        from src.services import result_advice
        call = f"game_check({game!r})"
        if (no := self._pc_only(call)) is not None:
            return no
        return self._record(call, result_advice.game_check(self.conn, self.revision_id, game))

    # ── DB 미보유 부품 실시간 검색 (docs/미보유부품_실시간스펙검색_설계.md 확장) ──
    def search_unavailable_part(self, slot: str, product_text: str) -> str:
        """사용자가 언급한 제품이 카탈로그에 없을 때 — 먼저 코드가 직접 카탈로그 전체에서 다시
        확인하고(LLM이 "없다"고 잘못 판단했을 수 있어서), 정말 없으면 동의를 구하는 문장만
        돌려준다(검색은 아직 안 함). 실제 검색은 `self.search_confirmed`가 True일 때만 — 이건
        LLM이 정하는 게 아니라 run_turn이 "직전 턴에 동의를 구했고 이번 메시지가 동의"인지를
        코드로 판정해 미리 정해 둔다(§2 자동 실행 금지, 2턴 확인을 LLM 재량이 아니라 코드로 강제)."""
        from src.repo.catalog_repo import load_candidates_by_slot_from_db
        from src.engine.owned_parts import _match_catalog, catalog_family_matches
        call = f"search_unavailable_part({slot!r}, {product_text!r})"
        it = self.item(slot)
        if it is None:
            return self._record(call, f"오류: '{slot}' 슬롯이 없습니다.")
        pool = load_candidates_by_slot_from_db(self.conn).get(it["slot"], [])
        matches = _match_catalog(product_text, pool)
        if matches:
            names = ", ".join(c.name for c in matches[:3])
            return self._record(call, f"'{product_text}'는 실제로 카탈로그에 있습니다: {names}. "
                                      "list_alternatives로 candidate_id를 확인해 안내하세요.")
        family = catalog_family_matches(product_text, pool)
        if family:       # "엔비디아 5070"처럼 시리즈 낱말이 빠졌을 뿐 카탈로그에 있는 제품 — 없다고 하면 안 된다
            names = ", ".join(c.name for c in family[:4])
            return self._record(call, f"'{product_text}'는 카탈로그에 있는 제품입니다: {names}. DB에 없는 상품이 아니니 외부 검색을 "
                                      "묻지 마세요. list_alternatives로 candidate_id를 확인해 안내하고, 어느 제품인지 정해지지 않았으면 "
                                      "그것만 되물으세요.")
        if not self.search_confirmed:
            return self._record(call, f"'{product_text}'는 저희 DB에 없는 상품으로 확인됩니다. {_SEARCH_PERMISSION_MARKER}")
        from src.services import live_spec_lookup
        if not live_spec_lookup.available():
            return self._record(call, "지금은 실시간 검색을 쓸 수 없습니다.")
        try:
            result = live_spec_lookup.lookup(self.conn, product_text, slot=it["slot"])
        except ServiceUnavailable as exc:      # 검색이 몰려 있거나 연결 실패 — 대화를 끊지 않고 문장으로 알린다
            return self._record(call, f"{exc.message} 지금은 이 제품 정보를 가져오지 못했습니다.")
        if not result.relevant or not result.has_any_field():
            return self._record(call, f"'{product_text}'에 대한 정보를 실시간 검색으로도 찾지 못했습니다.")
        fields = ", ".join(f"{k}={v}" for k, v in result.supported_fields.model_dump().items() if v is not None)
        src = f" · 출처: {result.source_url}" if result.source_url else ""
        return self._record(call, f"실시간 검색 결과 — {fields}{src} · 카탈로그 정식 등재 값이 아니니 참고만 하세요.")


# ── 도구 등록 ──────────────────────────────────────────────────────────────
def make_tools(s: ResultSession) -> list:
    from strands import tool

    @tool
    def list_alternatives(slot: str) -> str:
        """슬롯의 다른 후보를 가격순으로 보여준다. 교체 전에 반드시 이걸로 candidate_id 를 확인한다.

        Args:
            slot: 슬롯 이름 (예: GPU, CPU, RAM, 메인보드, 저장장치, 파워, 케이스, 쿨러)
        """
        return s.list_alternatives(slot)

    @tool
    def swap(slot: str, candidate_id: str) -> str:
        """슬롯의 부품을 다른 후보로 교체한다. candidate_id 는 list_alternatives 결과의 값.

        Args:
            slot: 슬롯 이름
            candidate_id: list_alternatives 가 돌려준 candidate_id
        """
        return s.swap(slot, candidate_id)

    @tool
    def set_qty(slot: str, qty: str) -> str:
        """부품의 수량만 바꾼다 ("SSD 2개로").

        Args:
            slot: 슬롯 이름
            qty: 1~99
        """
        return s.set_item(slot, qty=qty)

    @tool
    def remove_or_restore(slot: str, keep: str) -> str:
        """부품을 장바구니에서 빼거나("빼줘", "필요 없어" → keep="false") 다시 담는다("다시 담아줘" → keep="true").

        Args:
            slot: 슬롯 이름
            keep: "false" = 뺌, "true" = 담음
        """
        return s.set_item(slot, selected=keep)

    @tool
    def explain(slot: str) -> str:
        """왜 이 부품인지 묻는 질문("왜 이 CPU야?", "이거 괜찮아?", "리뷰 어때?")에 답하기 위해 부른다.
        저장된 추천 이유, 예산 비중, 근거 인용, 세트 검증 쟁점, 리뷰 관측(상품 단위)을 그대로 돌려준다.
        판정은 하지 않는다.

        Args:
            slot: 슬롯 이름
        """
        return s.explain(slot)

    @tool
    def preview_swap(slot: str, candidate_id: str = "", direction: str = "") -> str:
        """부품을 바꾸면 어떻게 되는지 *바꾸지 않고* 계산한다 — 차액·바꾼 뒤 총액·예산 초과 여부, 성능 등급 변화,
        이 견적의 요구 사양 충족 여부, 소켓·전력·크기 호환 점검. "바꿔도 돼?", "괜찮을까?", "문제없어?" 에 쓴다.
        특정 후보 대신 "한 단계 위/더 좋은 걸로"면 direction="up", "한 단계 아래/더 싼 걸로"면 direction="down" —
        코드가 성능 등급(RAM 은 용량) 기준 바로 다음 단계 후보를 골라 계산하고 그 candidate_id 를 돌려준다.

        Args:
            slot: 슬롯 이름
            candidate_id: list_alternatives·upgrade_options·savings_options 가 돌려준 candidate_id (direction 을 쓰면 비움)
            direction: "up" 또는 "down" (candidate_id 를 쓰면 비움)
        """
        return s.preview_swap(slot, candidate_id, direction)

    @tool
    def upgrade_options(extra: str = "", new_budget: str = "") -> str:
        """남은 예산(또는 주어진 금액) 안에서 CPU·GPU 성능 등급, RAM 용량을 올릴 수 있는 후보를 슬롯별로 돌려준다.
        요구 사양·호환을 통과한 것만, 용도 기준 등급에 못 미치는 부품부터. 바꾸지는 않는다.
        "돈 남았는데 뭐 올릴까?", "10만원 더 쓰면?", "예산 200만원이면?" 에 쓴다.

        Args:
            extra: 추가로 더 쓸 금액(예: "10만원"). 비우면 예산 잔여
            new_budget: 사용자가 말한 새 총예산(예: "200만원"). 주면 새 예산 - 지금 총액 안에서 찾는다
        """
        return s.upgrade_options(extra, new_budget)

    @tool
    def savings_options(target: str = "") -> str:
        """요구 사양을 지키면서 더 싸게 바꿀 수 있는 부품과 절약액, 요구 사양을 낮추면 줄일 수 있는 것을 돌려준다.
        target 을 주면 그 금액을 줄이는 조합을 계산한다. 바꾸지는 않는다. "뭘 바꾸면 싸져?", "20만원 줄이려면?" 에 쓴다.

        Args:
            target: 줄이고 싶은 금액(예: "20만원"). 없으면 비움
        """
        return s.savings_options(target)

    @tool
    def check_build() -> str:
        """지금 구성의 호환 점검 결과를 항목별로 돌려준다(소켓·메모리·크기·파워 용량 계산·커넥터·예산).
        "파워 충분해?", "호환 문제 없어?", "이대로 사도 돼?" 에 쓴다.
        """
        return s.check_build()

    @tool
    def game_check(game: str) -> str:
        """게임 요구 사양 표와 지금 구성의 성능 등급·RAM·VRAM 을 비교한다. "이 구성으로 배그 돌아가?" 에 쓴다.

        Args:
            game: 게임 이름만 (예: "배그", "사이버펑크 2077")
        """
        return s.game_check(game)

    @tool
    def search_unavailable_part(slot: str, product_text: str) -> str:
        """사용자가 말한 제품이 이 견적의 후보 목록·카탈로그에 안 보일 때 부른다 — "RTX 6090으로
        바꿔줘", "9950X3D 있어?"처럼 지금 카탈로그에 없을 수 있는 제품을 언급했을 때.
        먼저 카탈로그 전체에서 코드가 다시 확인하고, 정말 없으면 실시간 검색에 동의를 구하는
        문장만 돌려준다(이 호출로 바로 검색하지 않는다) — 동의는 사용자의 다음 메시지로 받는다.

        Args:
            slot: 슬롯 이름
            product_text: 사용자가 말한 제품명 원문(브랜드·모델 포함, 예: "RTX 6090")
        """
        return s.search_unavailable_part(slot, product_text)

    return [list_alternatives, swap, set_qty, remove_or_restore, explain,
            preview_swap, upgrade_options, savings_options, check_build, game_check,
            search_unavailable_part]


# ── 프롬프트 ───────────────────────────────────────────────────────────────
def _build_table(result: dict) -> str:
    rows = []
    for it in result.get("items", []):
        mark = "" if it["selected"] else " (빼둠)"
        r = it.get("reason") or {}
        reason = f" · 추천 이유: {r['text']}" if r.get("status") == "ready" and r.get("text") else ""
        rows.append(f"- {it['slot']}: {it['product']['name']} · {_won(it['price'])} × {it['qty']}"
                    f"{mark} · 다른 후보 {it['alternatives_count']}개{reason}")
    return "\n".join(rows) or "(부품 없음)"


_WHY = ("왜", "이유", "근거", "괜찮", "믿을", "어때", "리뷰", "총평", "요약", "설명",
        "why", "reason", "review", "good", "ok?", "summary", "overall", "explain")


# ── 바꾸라는 말인가 (구성표를 바꾸는 도구의 허용 조건) ──────────────────────────────
# 예전엔 "바꿔도 돼?" 꼴의 묻는 말을 찾아 막았다(차단 목록). 실제 말투("글카 갈아타도됨?", "파워 바꾸는거 ㄱㅊ?",
# "씨퓨 업글 해도 무방?")는 7개 중 1개만 걸렸다(2026-10-02) — 표현은 끝이 없어서 놓치면 구성표가 바뀐다.
# 그래서 거꾸로, **바꾸라는 표시가 있을 때만** 바꾸는 도구를 연다(허용 목록). 놓쳤을 때의 피해가 "잘못 바뀜"에서
# "한 번 더 물어봄"으로 줄어든다. 묻는 말 표시가 같이 있으면 묻는 말이 이긴다.
_CHANGE_VERB = r"(?:바꾸|바꿔|바꿀|갈아|올리|올려|올릴|늘리|늘려|늘릴|업글|업그레이드|교체|변경|빼|뺄|넣|담|달아|내리|내려|줄이|줄여|가면)"
_ASK_RE = re.compile(
    r"도\s*(?:돼|되나|될까|되려나|되겠|되냐|되남|됨|괜찮|괜춘|문제|상관|무방|ok|ㄱㅊ)"
    r"|면\s*.{0,15}?(?:어때|어떨|어떻|괜찮|문제|될까|되나|돼\?|충분|부족)"
    r"|문제\s*(?:없|안\s*생|생기|있)"
    r"|(?:바꿀|올릴|늘릴|갈아탈|업글할|교체할|뺄|넣을)까"
    r"|ㄱㄱ\s*\?"
)
_ASK_LOOSE_RE = re.compile(r"ㄱㅊ|괜찮음|괜춘|무방|어케\s*생각|어떻게\s*생각|어떰|어떨까|나을까|낫나|좋을까")
_IMPERATIVE = (r"(?:바꿔|교체해|변경해|빼|넣어|담아|올려|내려|늘려|줄여|적용해|진행해|업글해|업그레이드해|로\s*해|로\s*가자|로\s*할게|로\s*갈게)"
               r"\s*(?:줘|주세요|줄래|주라|줄\s*수|주셈|주삼|주쇼|쥬|줭|봐|요|라)")
_IMPERATIVE_RE = re.compile(_IMPERATIVE)
_CLAUSE_SPLIT = re.compile(r"[,.!?\n]+")
_CHANGE_RE = re.compile(
    _IMPERATIVE
    + r"|(?:로\s*해|로\s*할게|로\s*갈게|로\s*가자)\b"
    r"|(?:으)?로\s*[요.!~]*\s*$"                              # "7600X로", "SSD 2개로", "더 싼 걸로" — 말줄임 요청
    r"|^\s*(?:응|ㅇㅇ|ㅇㅋ|오케이|ok|okay|네|넵|예|그래|좋아|콜|ㄱㄱ|고고|부탁해|그렇게\s*해)(?:\s|[.!~,]|$)"
    r"|ㄱㄱ(?!\s*\?)"
)


# ── DB 미보유 부품 실시간 검색 — 동의 확인(conditions_agent.is_search_confirmation 공유) ──
def _search_confirmed(pairs: list[tuple[str, str]], text: str) -> bool:
    last_reply = pairs[-1][1] if pairs else None
    return is_search_confirmation(last_reply, text)


# 조건(예산·우선순위·용도…)을 바꾸라는 말 — 결과 채팅은 부품만 바꾼다. "남은 예산으로", "예산 안에서", "예산 넘어도"는
# 조건을 쓰는 말이지 바꾸라는 말이 아니라서 뺀다. 사이에 부품 이름이 있으면("예산 맞춰서 그래픽카드 올려줘") 부품 요청이다.
_CONDITION_CHANGE_RE = re.compile(
    r"(?:예산|우선순위|용도|목적|해상도)(?!\s*(?:으로|로|안|내|에서|에|초과|넘|잔여|남|대비))([^,.!?\n]{0,12}?)" + _CHANGE_VERB)
CONDITION_NOTICE = "예산·우선순위·용도 같은 조건은 이 채팅에서 바뀌지 않아요. 화면의 '조건 바꾸기'를 누르면 조건 대화로 돌아가 바꿀 수 있습니다."


def is_condition_change_request(text: str) -> bool:
    from src.engine.brands import SLOT_SYNONYMS
    low = text.lower()
    return any(not any(k in m.group(1) for k in SLOT_SYNONYMS) for m in _CONDITION_CHANGE_RE.finditer(low))


def with_condition_notice(text: str, reply: str) -> str:
    """조건을 바꾸라는 말이면 답 끝에 '조건 바꾸기' 안내를 붙인다(이미 있으면 그대로). 프롬프트 규칙만으로는 "가격에
    맞게 예산 줄여줘"가 부품 절약 조합으로 오독되고(10/7 수정 전 측정 2/3), "우선순위를 성능으로 바꿔줘"에 조건이
    안 바뀌는데 반영된 것처럼 읽혔다(팀원 관찰 10/7). 모델 경로·규칙 경로 모두 여기를 지난다."""
    if not is_condition_change_request(text) or "조건 바꾸기" in reply:
        return reply
    return f"{reply.rstrip()}\n\n{CONDITION_NOTICE}" if reply.strip() else CONDITION_NOTICE


def _is_ask(low: str) -> bool:
    return bool(_ASK_RE.search(low) or (_ASK_LOOSE_RE.search(low) and re.search(_CHANGE_VERB, low)))


def classify_intent(text: str) -> str:
    """'change'(바꾸라는 말) / 'ask'(바꿔도 되는지 묻는 말) / 'other'. 구성표를 바꾸는 도구는 'change'에서만 열린다."""
    low = text.lower().strip()
    # 허락 + 명령("예산 조금 넘어도 괜찮아, 그걸로 바꿔줘") — 앞 절의 허락('도 괜찮')이 묻는 말로 먼저 잡혀 되묻던 것
    # (10/3 리허설 실패 6). 묻는 말 표시가 없는 절에 바꾸라는 명령이 있으면 바꾸라는 말이다.
    if any(_IMPERATIVE_RE.search(c) and not _is_ask(c) for c in _CLAUSE_SPLIT.split(low)):
        return "change"
    if _is_ask(low):
        return "ask"
    if _CHANGE_RE.search(low):
        return "change"
    return "other"


def is_whatif_question(text: str) -> bool:
    return classify_intent(text) == "ask"


_WHOLE_BUILD = ("구성", "견적", "전체", "이거", "이것", "이 조합", "총평", "build", "overall", "this")


def _prefetch_explanations(session: ResultSession, text: str) -> str:
    """'왜 이 CPU야?' 류는 모델이 explain 을 안 부르고 답하는 일이 있어서, 코드가 먼저 조회해 프롬프트에 싣는다.
    슬롯을 못 찾으면 담긴 부품 전부. 판단은 여전히 안 한다 — 저장된 사실을 옮길 뿐."""
    from src.services.recommendation_service import _match_slot
    low = text.lower()
    if not any(w in low for w in _WHY) or is_whatif_question(text):
        return ""          # "바꿔도 괜찮아?"는 지금 부품의 근거가 아니라 가정 결과(preview_swap)를 묻는다
    slots = [it["slot"] for it in session.result.get("items", [])]
    hit = _match_slot(text, set(slots))
    if hit is None and not any(w in low for w in _WHOLE_BUILD):
        return ""          # "오늘 날씨 어때?" — 부품도 구성 전체도 가리키지 않는 말에 8개 부품을 다 조회하지 않는다
    targets = [hit] if hit else [it["slot"] for it in session.result.get("items", []) if it["selected"]]
    out = "\n\n".join(session.explain(sl) for sl in targets)
    session.trace[:] = [f"prefetch:{t}" for t in session.trace]   # 도구 호출과 구분
    return out


def system_prompt(result: dict, user_text: str, history: list[dict], prefetched: str = "") -> str:
    t = result.get("totals") or {}
    v = result.get("verification") or {}
    issues = " | ".join(f"[{i['axis']}] {i['text']}" for i in (v.get("issues") or [])) or "없음"
    return "\n".join([
        "당신은 TrueFit 추천 결과 화면의 도우미입니다. 아래 구성표를 읽고 사용자의 요청을 도구로 처리하거나 질문에 답합니다.",
        "",
        f"카테고리: {result.get('category')} · 조건: {result.get('conditions_summary') or '-'}",
        f"예산 상한: {_won(result.get('budget_max'))} · 총액: {_won(t.get('selected_price'))} · 잔여: {_won(t.get('budget_remaining'))}"
        + (" · ⚠ 예산 초과" if t.get("over_budget") else ""),
        f"세트 검증 쟁점: {issues}",
        "구성표:",
        _build_table(result),
        "",
        "규칙:",
        "1. '바꿔줘', '로 해줘', '바꿔 주세요'처럼 바꾸라고 하면 되묻지 말고 바로 swap 합니다. candidate_id 는 list_alternatives·upgrade_options·savings_options·preview_swap 결과에서만 가져오고, 지어내거나 답변에 보여 주지 않습니다.",
        "2. 방향만 말하면('한 단계 좋은 걸로 바꿔줘', '더 싼 걸로') preview_swap(direction='up'/'down') 으로 바로 다음 단계 후보를 찾아 그 candidate_id 로 swap 합니다. "
        "'⚠ 요구 사양' 표시가 있는 후보는 바꾸지 말고 그 사실을 알립니다. 사용자가 고른 이름이 여럿에 해당하면 이름·가격을 나열하고 고르게 합니다.",
        "3. '빼줘/필요 없어' → remove_or_restore(false). '2개로' → set_qty. "
        "한 문장에 부품 여러 개가 나오면 각 부품에 그 부품 앞뒤에 붙은 요청만 적용하고 도구를 따로 부릅니다.",
        "4. '왜 이거?', '이유가 뭐야?', '이거 괜찮아?', '믿을 만해?' 처럼 지금 부품의 근거를 묻는 말에는 **반드시 explain 을 먼저 부르고** 그 내용만 전합니다. "
        "저장된 추천 이유가 없어도 explain 이 준 가격·예산 비중·검증 쟁점·리뷰 관측은 전합니다.",
        "5. '바꿔도 돼?', '바꾸면 괜찮을까?', '올려도 문제없어?', '32기가로 늘려도 돼?' 처럼 **바꿔도 되는지 묻는 말은 교체 요청이 아닙니다**. "
        "preview_swap(특정 제품이면 candidate_id, '더 좋은 걸로/한 단계 올리면'이면 direction='up')으로 가정 결과만 확인해 차액·예산 초과 여부·요구 사양·호환 점검(전력 포함) 결과를 전한 뒤 '바꿔 드릴까요?'로 묻습니다. swap·set_qty 를 부르지 않습니다.",
        "6. '돈 남았는데 뭐 바꿀까', '남은 예산으로 업그레이드', 'N만원 더 쓰면' → upgrade_options. '예산을 N원으로 늘려줘'처럼 새 총예산을 말하면 upgrade_options(new_budget=N) — "
        "조건의 예산 자체는 이 화면에서 못 바꾸고 화면의 '조건 바꾸기'에서 바꾼다고 한 번 말하고 그 금액 기준 후보를 보여 줍니다. 결과의 순서와 후보를 그대로 전하고, 사용자가 고르기 전에는 바꾸지 않습니다.",
        "7. '뭘 바꾸면 싸져?', 'N만원 줄이고 싶어' → savings_options(target). 금액을 말했으면 '→ … 성능을 가장 적게 잃는 조합' 줄을 먼저 전합니다 "
        "— 가장 많이 줄어드는 후보를 대신 권하지 않습니다. 요구 사양을 낮춰야 하는 항목은 그 사실(⚠)과 함께 전합니다.",
        "8. '파워 충분해?', '호환 문제 없어?', '이대로 사도 돼?', 'CPU가 발목 잡아?(병목)' → check_build. '쿨러 꼭 사야 돼?', '기본 쿨러 들어 있어?' → explain('쿨러') 의 'CPU 기본 쿨러' 값으로 답합니다. 이 게임 돌아가? → game_check(게임 이름). 도구 결과에 있는 항목만 말하고 fps·체감 성능은 말하지 않습니다.",
        "9. **이 견적**의 부품 우열, 리뷰의 진위, 호환 여부를 스스로 판정하지 않고 도구 결과에 없는 수치·사실을 만들지 않습니다. 금액을 직접 더하거나 빼지 말고 도구가 계산한 금액을 옮깁니다. "
        "호환은 '문제 없다'·'충분하다'고 단정하지 말고 '점검 기준(예: 230W ≤ 675W)을 통과했다'처럼 도구가 계산한 비교를 옮깁니다.",
        "10. 답변은 5문장 이내. 바꿨으면 바뀐 것과 총액·예산 잔여를 말하고, 도구 결과에 '예산 초과'나 '호환 점검 문제'가 있으면 그것도 한 번 언급합니다.",
        "11. 전체를 다시 짜 달라는 요청('처음부터', '다른 구성')은 화면의 '다른 구성 보기' 버튼을 안내합니다. 구성표에 없는 품목(모니터·키보드 등)은 이 화면에서 다루지 않는다고 짧게 안내합니다.",
        "12. 구성표에 없는 슬롯이나 상품을 만들지 않습니다. '죄송'·'확인할 수 없다' 로 시작하지 않습니다 — 아는 사실부터 말합니다.",
        "13. 일반 안내: 맞는 도구가 없는 PC 일반 질문(부품의 역할, 규격·등급의 뜻, 조립·업그레이드 일반론, 쿨러가 왜 필요한가 등)은 '일반적으로'로 시작해 일반 지식으로 2~3문장 답합니다. "
        "'판단하기 어렵다'로 끝내지 말고 일반론을 먼저 말합니다. 단, 일반 안내에는 (a) 입력에 없는 숫자를 쓰지 않고 "
        "(b) 이 견적 부품에 대한 판정(충분하다·호환된다·더 낫다)을 하지 않고 (c) 특정 제품의 사실(동봉품·사양·출시·가격 전망)을 지어내지 않습니다 — 그런 건 '데이터에 없다'고 말합니다. "
        "마지막에 이 견적으로 확인할 수 있는 질문을 하나 제안합니다(예: '파워 충분해?', '남은 예산으로 뭘 올릴까?', '쿨러 빼줘').",
        "14. 소음·발열·가격 전망처럼 데이터가 없는 질문은 없다고 말하고 추측하지 않습니다. 날씨처럼 PC와 무관한 말은 이 화면은 PC 견적만 다룬다고 한 문장으로 답합니다.",
        "15. 사용자가 말한 제품이 list_alternatives·upgrade_options·savings_options 결과에 안 보이면(지어내지 말고) "
        "search_unavailable_part(slot, product_text)를 부릅니다. 돌려준 문장을 그대로 전합니다 — 동의를 구하는 "
        "문장이면 그걸로 끝내고(이 턴에 swap하지 않습니다), 검색 결과가 오면 '카탈로그 정식 등재 값이 아니다'는 "
        "부분까지 그대로 전합니다. 검색 결과로 얻은 스펙만으로 swap하지 않습니다 — 그 제품은 여전히 카탈로그에 없어 "
        "교체할 candidate_id가 없습니다.",
        *(["", "사용자 질문에 대해 미리 조회한 근거 (이걸로 답합니다. 더 필요하면 explain):", prefetched] if prefetched else []),
        "",
        "답변 언어: 한국어 존댓말. 화면은 평문이라 마크다운(**굵게**, #, 표)을 쓰지 않습니다.",
    ])


def _db_history(conn, revision_id: UUID, run_id: str, limit: int = _HISTORY_TURNS) -> list[tuple[str, str]]:
    """DB(identity.message)에서 이번 run의 결과 화면 채팅만 최근 N턴 — 프로세스 메모리(재시작하면
    사라지던 예전 `_HISTORY`) 대신이다(CHAT-08). 조건 대화(session_service)는 같은 conversation에
    더 앞서 쌓여 있지만, 이 run이 생기기 전(created_at 이전) 것들이라 여기서는 제외한다 — 결과
    화면 에이전트에게 "게임용 컴퓨터 맞춰주세요" 같은 조건 대화 턴을 섞어 넣으면 구성표 얘기를
    하는 이 에이전트의 맥락과 안 맞는다. 저장(대화 이력 전체를 화면에 복원하는 것)은
    `GET /session/{id}`가 이미 하므로 여기서는 에이전트가 볼 맥락만 좁힌다."""
    from src.repo.plan_repo import PlanRepo
    from src.repo.user_repo import ConversationRepo

    conversation_id = PlanRepo(conn).get_revision(revision_id)["conversation_id"]
    row = conn.execute("SELECT created_at FROM engine.recommendation_run WHERE id=%s", (UUID(run_id),)).fetchone()
    cutoff_at = row[0] if row else None
    messages = ConversationRepo(conn).messages(conversation_id)
    if cutoff_at is not None:
        messages = [m for m in messages if m["created_at"] >= cutoff_at]

    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for m in messages:
        if m["role"] == "user":
            pending = m["content"]
        elif m["role"] == "assistant" and pending is not None:
            pairs.append((pending, m["content"]))
            pending = None
    return pairs[-limit:]


def _history_messages(pairs: list[tuple[str, str]]) -> list[dict]:
    msgs = []
    for u, a in pairs:
        msgs.append({"role": "user", "content": [{"text": u}]})
        msgs.append({"role": "assistant", "content": [{"text": a}]})
    return msgs


# ── 수치 가드 ──────────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    """문장 속 숫자를 정규화해 모은다: 쉼표 제거, 소수점·퍼센트는 숫자 부분만 ("1,457,000"→"1457000", "18%"→"18")."""
    return {m.group(0).replace(",", "").rstrip(".") for m in _NUM_RE.finditer(text or "")}


# 평가어 — "뛰어난 1순위 선택" 처럼 부품 우열을 말하면 규칙 4 위반. [5] 의 금지어 중 평가 표현만.
_EVALUATIVE = ("강력", "뛰어나", "최고", "압도적", "완벽", "훌륭", "우수", "극대화")


def evaluative_words(reply: str, user_text: str = "") -> list[str]:
    """답변 속 평가어. '최고가'(가장 비싼 가격)는 평가가 아니다 — "최고가 후보로 바꿨다"가 버려지던 것.
    사용자 말에 있던 낱말은 빼다 — "4K 최고 옵션도 되지?"의 전제를 정정하려고 '최고 옵션'을 옮긴 답이 3/3 버려지고,
    대체 문장은 전제를 정정하지 않았다(10/7 수정 전 측정). 이 가드는 모델이 스스로 붙인 과장어를 막는 것이다."""
    return [w for w in _EVALUATIVE
            if re.search(w + ("(?!가)" if w == "최고" else ""), reply) and w not in user_text]


def _reply_within(reply: str, allowed_sources: list[str]) -> tuple[bool, set[str]]:
    """답변의 숫자가 전부 입력(프롬프트·도구 결과·사용자 메시지)에 있던 숫자인지. (통과 여부, 밖의 숫자들)"""
    allowed: set[str] = set()
    for src in allowed_sources:
        allowed |= _numbers(src)
    # "750W × 0.9" 를 "750W의 90%"로 옮기는 건 같은 값이다 — 1 미만 소수는 백분율 표기도 허용
    allowed |= {f"{float(n) * 100:g}" for n in allowed if n.startswith("0.")}
    outside = _numbers(reply) - allowed
    return (not outside), outside


_READ_TOOLS = ("preview_swap(", "upgrade_options(", "savings_options(", "check_build(", "game_check(",
              "search_unavailable_part(")


def _guarded_reply(session: ResultSession, prefetched: str) -> str:
    """LLM 문장을 버릴 때 내는 코드 문장 — 사실만."""
    t = session.result.get("totals") or {}
    if session.changed:
        # 바꾼 도구의 결과만 — list_alternatives 의 후보 목록·내부 id 가 화면에 나가던 것(2026-10-02 실측)
        done = [x.split(" → ", 1)[1].split(" · ")[0] for x in session.trace
                if x.startswith(("swap(", "set_item(")) and "오류" not in x and "거절:" not in x]
        return ("적용된 변경: " + " / ".join(done) + f" · 총액 {_won(t.get('selected_price'))} · 예산 잔여 {_won(t.get('budget_remaining'))}"
                + (" · ⚠ 예산 초과" if t.get("over_budget") else ""))
    # 묻는 말에 쓴 계산 도구의 결과가 있으면 그 원문(사실)을 그대로 — 화면에 내부 id 는 빼고
    read = [out for call, out in zip(session.trace, session.outputs) if call.startswith(_READ_TOOLS) and not out.startswith("오류")]
    if read:
        from src.services.result_advice import for_user
        return for_user(read[-1])
    if prefetched:
        return prefetched.replace("\n", " ")
    return (f"구성표 기준으로만 답할 수 있어요 — 총액 {_won(t.get('selected_price'))}, 예산 잔여 {_won(t.get('budget_remaining'))}. "
            "바꾸고 싶은 부품과 방향(더 저렴한/더 좋은), 또는 궁금한 부품을 말씀해 주세요.")


# ── 실행 ───────────────────────────────────────────────────────────────────
@dataclass
class TurnResult:
    reply: str
    result: dict
    trace: list[str]
    changed: bool
    # 턴 기록(P1-3) — 호출자가 답 메시지의 metadata["turn"]에 남긴다. 경로·가드·토큰을 나중에 로그로 셀 수 있게.
    intent: str = ""
    guard: dict | None = None          # 수치·표현 가드가 LLM 문장을 버렸으면 {"numbers": [...], "words": [...]}
    error: str | None = None           # 도구가 이미 바꾼 뒤 모델이 실패했으면 예외 이름
    usage: dict | None = None          # 이 턴의 모델 호출 토큰 합계(usage_of)
    outputs: list[str] = field(default_factory=list)   # 도구 결과 원문 — trace 는 160자로 잘려 사람이 답을 판정할 수 없다


def usage_of(agent) -> dict | None:
    """Strands 에이전트가 이 턴에 쓴 토큰 합계 — 턴마다 새 Agent 라 누적값이 곧 이 턴의 값이다.
    공급자가 사용량을 안 주면(가짜 모델 등) None. 비용은 여기서 계산하지 않는다 — 단가는 바뀌므로 보고서에서 곱한다."""
    metrics = getattr(agent, "event_loop_metrics", None)
    usage = getattr(metrics, "accumulated_usage", None)
    if not usage:
        return None
    out = {"input": int(usage.get("inputTokens") or 0), "output": int(usage.get("outputTokens") or 0),
           "total": int(usage.get("totalTokens") or 0), "calls": int(getattr(metrics, "cycle_count", 0) or 0)}
    if usage.get("cacheReadInputTokens"):
        out["cache_read"] = int(usage["cacheReadInputTokens"])
    return out if out["total"] or out["input"] or out["output"] else None


def run_turn(conn, revision_id: UUID, result: dict, text: str, user_id: UUID | None = None) -> TurnResult:
    """한 턴. `result` 는 호출 시점의 get_stored_result. 도구가 바꾸면 갱신된 것을 돌려준다."""
    from strands import Agent
    from strands.tools.executors import SequentialToolExecutor

    run_id = result["run_id"]
    pairs = _db_history(conn, revision_id, run_id)
    hist_rows = [{"role": "user", "content": u} for u, _ in pairs]
    intent = classify_intent(text)
    session = ResultSession(conn=conn, revision_id=revision_id, result=result, user_id=user_id,
                            read_only=intent != "change",
                            search_confirmed=_search_confirmed(pairs, text))
    prefetched = _prefetch_explanations(session, text)
    prompt = system_prompt(result, text, hist_rows, prefetched)
    agent = Agent(
        model=_model(),
        system_prompt=prompt,
        tools=make_tools(session),
        messages=_history_messages(pairs),
        tool_executor=SequentialToolExecutor(),   # 도구들이 요청 스레드의 DB 연결 하나를 같이 쓴다
        callback_handler=None,
    )
    guard, error = None, None
    try:
        reply = str(agent(text)).strip()
    except Exception as exc:
        if not session.changed:
            raise                      # 아무것도 안 바꿨으면 호출자가 규칙 경로로
        # 도구가 이미 구성표를 바꿨다 — 규칙 경로가 또 바꾸면 안 되니 바뀐 것만 알린다
        log.exception("result agent failed after tool writes; reporting trace")
        reply = "요청을 처리하다 답변 생성에 실패했어요. " + _guarded_reply(session, prefetched)
        error = type(exc).__name__
    else:
        # 수치 가드 — 답변의 숫자는 전부 입력에 있던 것이어야 한다. 아니면 LLM 문장을 버리고 코드 문장으로.
        ok, outside = _reply_within(reply, [prompt, text, *session.outputs])
        bad_words = evaluative_words(reply, text)
        if not ok or bad_words:
            log.warning("result agent reply rejected (numbers %s, words %s) — replaced: %r", sorted(outside), bad_words, reply[:120])
            guard = {"numbers": sorted(outside), "words": bad_words}
            reply = _guarded_reply(session, prefetched)
    # 채팅 말풍선은 평문(pre-wrap)이라 '**굵게**'가 별표 그대로 보인다 — 프롬프트로도 막지만 남으면 코드가 지운다
    reply = reply.replace("**", "")
    # 대화 저장은 호출자(recommendation_service.handle_result_message)가 한다 — 여기서 두 번 쓰지 않는다.
    if session.changed:
        session.refresh()
    return TurnResult(reply=reply, result=session.result, trace=list(session.trace), changed=session.changed,
                      intent=intent, guard=guard, error=error, usage=usage_of(agent),
                      outputs=list(session.outputs))
