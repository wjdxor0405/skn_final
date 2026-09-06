"""
L4 — 자연어 요약 + 리포트 생성
원칙(5절): 리포트는 로그에서만 파생된다. 에이전트가 리포트를 직접 쓰지 않는다.

구현체는 SUMMARIZER_MODE 환경변수로 전환된다:
  - "template" (기본값): TemplateSummarizer — 문자열 조합. API 비용 없음
  - "llm": LLMSummarizer — **설명 제너레이션**. 아래 참고

[설명 제너레이션 — 왜 XAI 가 아닌가]
멘토 피드백(`docs/wiki_대조_20260907.md` §2-3)의 결론이다. XAI 는 기각됐다 —
팀이 쓰는 건 로컬 모델이 아니라 LLM API 라 어텐션을 볼 수 없고, LIME 은 토큰을
하나씩 빼며 재추론해야 해서 1회 예측에 수천 원까지 간다. 대안으로 지정된 것이
**예측 후에 "왜 이렇게 골랐는지"를 텍스트로 푸는 것**이다.
COT(답을 내기 전에 추론을 먼저 생성)와 혼동하지 말 것 — 여기서 판단은 이미
`negotiate.py` 의 룰이 끝냈고, LLM 은 **끝난 판단을 설명만** 한다.

그래서 LLMSummarizer 는 **로그를 직접 읽지 않는다.** `_facts()` 가 룰로 뽑아낸
사실 dict 만 받는다. 숫자와 판정이 전부 룰에서 오므로 모델이 지어낼 자리가 없고,
같은 사실을 TemplateSummarizer 도 쓰기 때문에 두 구현이 다른 사실을 말할 수 없다.

그래도 모델은 숫자를 흘릴 수 있다. `_verify()` 가 낙찰자·낙찰가가 출력에 실제로
들어 있는지 확인하고, 아니면 템플릿으로 되돌린다 — `audit.py` 가 가격 판단에
하는 것과 같은 종류의 가드레일이다. **어떤 실패에서도 리포트는 나온다.**
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .schemas import Envelope, MsgType, SummarizerPort

log = logging.getLogger(__name__)

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)

SUMMARIZER_MODE = os.environ.get("SUMMARIZER_MODE", "template").lower()


def _facts(log_: list[Envelope]) -> dict | None:
    """
    로그에서 요약에 필요한 사실만 뽑는다. 두 구현체가 **같은 dict** 를 쓴다.

    None 이면 요청 메시지 자체가 없는 로그다.
    """
    # 상한가는 셀러에게 나가지 않는다. 바이어가 중앙에 접수한 메시지에만 있다.
    # (to != "central" 인 옛 로그도 읽을 수 있게 폴백을 둔다)
    request = next(
        (e for e in log_ if e.type == MsgType.REQUEST and e.to == "central"),
        next((e for e in log_ if e.type == MsgType.REQUEST), None),
    )
    if not request:
        return None
    settled = next((e for e in log_ if e.type == MsgType.SETTLED), None)

    # 셀러별 OFFER 시퀀스 (스크리닝을 통과해 실제 라운드에 들어간 셀러만 존재)
    by_seller: dict[str, list[Envelope]] = {}
    for e in log_:
        if e.type == MsgType.OFFER:
            by_seller.setdefault(e.frm, []).append(e)

    sellers = {}
    for seller_id, offers in by_seller.items():
        sellers[seller_id] = {
            "n_offers": len(offers),
            "first": offers[0].payload["price"],
            "last": offers[-1].payload["price"],
            "was_rejected": any(
                e.type == MsgType.REJECT
                and e.to == seller_id
                and e.payload.get("stage") != "screening"
                for e in log_
            ),
        }

    return {
        "item": request.payload.get("item"),
        "qty": request.payload.get("qty"),
        "cap": request.payload.get("cap_price"),
        # 1차 스크리닝에서 제외된 셀러 (재고·사양·납기·MOQ 미달)
        "screening_rejects": [
            {"seller_id": e.to, "reason": e.payload.get("reason")}
            for e in log_
            if e.type == MsgType.REJECT and e.payload.get("stage") == "screening"
        ],
        "sellers": sellers,
        "winner": settled.payload.get("seller_id") if settled else None,
        "price": settled.payload.get("price") if settled else None,
    }


class TemplateSummarizer:
    """L4 1차 구현 — LLM 없이 문자열 조합만으로 자연어 요약을 만든다."""

    def summarize(self, log_: list[Envelope]) -> str:
        f = _facts(log_)
        if f is None:
            return "요청 정보를 찾을 수 없습니다."
        return self.render(f)

    def render(self, f: dict) -> str:
        qty, cap = f["qty"], f["cap"]
        sellers = f["sellers"]

        # ── 케이스 1: NO_MATCH — 라운드 자체에 아무도 못 들어감 ──
        if not sellers:
            if f["screening_rejects"]:
                excluded = "; ".join(
                    f"{r['seller_id']}({r['reason']})" for r in f["screening_rejects"]
                )
                return (
                    f"{qty}건 요청에 대해 등록된 판매자 중 조건을 만족하는 곳이 없었습니다. "
                    f"제외 사유 — {excluded}. 조건을 완화하거나 다른 판매자 등록이 필요합니다."
                )
            return "등록된 판매자가 없어 매칭을 시도하지 못했습니다."

        sentences: list[str] = []
        for seller_id, s in sellers.items():
            if s["n_offers"] == 1 and not s["was_rejected"]:
                sentences.append(
                    f"{seller_id}가 {qty}건에 대해 단가 {s['first']}원으로 제시했습니다."
                )
            elif s["was_rejected"]:
                sentences.append(
                    f"{seller_id}가 단가 {s['first']}원으로 최초 제시했으나 바이어 상한가({cap}원)를 "
                    f"초과해 거절되었고, 이후 {s['last']}원으로 재조정해 다시 제시했습니다."
                )

        if f["winner"]:
            winner, price = f["winner"], f["price"]
            others = [s for s in sellers if s != winner]
            if others:
                comparisons = []
                for s in others:
                    other_price = sellers[s]["last"]
                    if other_price > price:
                        comparisons.append(f"{s} {other_price}원(더 높음)")
                    elif other_price == price:
                        comparisons.append(f"{s} {other_price}원(동일가, 먼저 접수된 조건 우선)")
                    else:
                        comparisons.append(f"{s} {other_price}원(더 낮았으나 조건상 미채택)")
                detail = ", ".join(comparisons)
                sentences.append(
                    f"다른 제안({detail}) 대비 {winner}의 {price}원이 최종 채택되어 확정되었습니다."
                )
            else:
                sentences.append(f"{winner}가 {price}원에 최종 확정되었습니다.")
        else:
            # ── 케이스 2: NO_DEAL — 후보는 있었으나 가격 협상이 결렬 ──
            sentences.append(
                "조건을 만족하는 판매자는 있었으나, 라운드 상한 내에 상한가를 만족하는 제시가가 없어 협상이 결렬되었습니다."
            )

        return " ".join(sentences)


EXPLAIN_SYSTEM = (
    "당신은 B2B 조달 마켓플레이스의 거래 결과를 설명하는 역할입니다. "
    "판단은 이미 규칙 엔진이 끝냈습니다. 당신은 그 결과를 설명만 합니다.\n"
    "- 주어진 사실에 없는 숫자·업체명·이유를 절대 만들어내지 마세요\n"
    "- 왜 이 판매자가 채택됐는지, 탈락한 곳은 왜 탈락했는지를 구매 담당자가 "
    "상급자에게 보고할 수 있는 문장으로 쓰세요\n"
    "- 한국어 3~5문장. 표나 목록 없이 줄글로"
)


class LLMSummarizer:
    """
    설명 제너레이션 — 룰이 끝낸 판단에 근거 문장을 붙인다.

    실패하면 조용히 템플릿으로 되돌린다. 리포트는 승인 흐름의 산출물이라
    "요약을 못 만들어서 리포트가 없다"가 되면 안 된다.
    """

    def __init__(self, fallback: TemplateSummarizer | None = None) -> None:
        self._fallback = fallback or TemplateSummarizer()

    def summarize(self, log_: list[Envelope]) -> str:
        f = _facts(log_)
        if f is None:
            return "요청 정보를 찾을 수 없습니다."

        try:
            text = self._generate(f)
        except Exception as e:  # noqa: BLE001 — 아래 주석 참고
            # 모델·네트워크·키 무엇이 실패하든 리포트 자체는 나와야 한다.
            log.warning("설명 생성 실패, 템플릿으로 대체합니다: %s", e)
            return self._fallback.render(f)

        ok, why = _verify(text, f)
        if not ok:
            log.warning("설명이 사실과 어긋나 템플릿으로 대체합니다: %s", why)
            return self._fallback.render(f)
        return text

    def _generate(self, f: dict) -> str:
        # 임포트를 여기 두는 이유: 기본값 template 는 이 패키지 없이 돌아야 한다.
        from strands import Agent

        from .strands_agents import _model

        agent = Agent(model=_model(), system_prompt=EXPLAIN_SYSTEM)
        return str(agent(_explain_prompt(f))).strip()


def _explain_prompt(f: dict) -> str:
    """룰이 뽑은 사실만 넘긴다 — 원문 로그는 넘기지 않는다."""
    lines = [
        f"품목: {f['item']} / 요청 수량: {f['qty']}건 / 바이어 상한가: {f['cap']}원",
        "",
        "협상에 들어간 판매자:",
    ]
    if f["sellers"]:
        for sid, s in f["sellers"].items():
            note = " (상한가 초과로 한 번 거절당한 뒤 재조정)" if s["was_rejected"] else ""
            lines.append(f"  - {sid}: 최초 {s['first']}원 → 최종 {s['last']}원{note}")
    else:
        lines.append("  - 없음")

    if f["screening_rejects"]:
        lines += ["", "라운드 진입 전에 걸러진 판매자(조건 미달):"]
        lines += [f"  - {r['seller_id']}: {r['reason']}" for r in f["screening_rejects"]]

    lines += ["", f"낙찰: {f['winner']} @ {f['price']}원" if f["winner"] else "", ""]
    if not f["winner"]:
        lines.append("결과: 상한가를 만족하는 제시가가 없어 결렬되었습니다.")
    lines.append("위 사실만으로 결과를 설명하세요.")
    return "\n".join(lines)


def _verify(text: str, f: dict) -> tuple[bool, str]:
    """
    가드레일 — 설명이 사실을 담고 있는지 확인한다.

    문장 품질을 재는 게 아니라, **낙찰자와 낙찰가가 실제로 들어 있는지**만 본다.
    엉뚱한 업체를 이겼다고 쓰거나 금액을 지어내는 것이 이 리포트에서 가장 비싼
    실패이기 때문이다. 통과 못 하면 템플릿이 대신 나간다.
    """
    if not text:
        return False, "빈 응답"
    if f["winner"]:
        if f["winner"] not in text:
            return False, f"낙찰자({f['winner']})가 설명에 없음"
        price = f["price"]
        # 3,608 / 3608 둘 다 허용 — 모델이 천 단위 구분을 넣는 편이 자연스럽다
        if str(price) not in text.replace(",", ""):
            return False, f"낙찰가({price})가 설명에 없음"
    return True, ""


def _build_summarizer() -> SummarizerPort:
    if SUMMARIZER_MODE == "llm":
        return LLMSummarizer()
    return TemplateSummarizer()


summarizer: SummarizerPort = _build_summarizer()


def write_report(txid: str, log_: list[Envelope]) -> Path:
    """리포트는 오직 log로부터만 만들어진다 (에이전트가 직접 쓰지 않음)."""
    text = summarizer.summarize(log_)
    path = REPORT_DIR / f"{txid}.txt"
    lines = [f"거래ID: {txid}", "", "[자연어 요약]", text, "", "[원문 로그]"]
    for e in log_:
        lines.append(f"{e.ts}  {e.type.value:8s}  {e.frm} -> {e.to}  {e.payload}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
