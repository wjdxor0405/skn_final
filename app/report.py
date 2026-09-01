"""
L4 — 자연어 요약 + 리포트 생성
원칙(5절): 리포트는 로그에서만 파생된다. 에이전트가 리포트를 직접 쓰지 않는다.

TemplateSummarizer는 지금 쓰는 구현체. 나중에 LLM으로 바꾸려면
SummarizerPort를 만족하는 LLMSummarizer를 새로 만들어서
main.py에서 주입하는 인스턴스만 바꾸면 된다 (파일 하나 갈아끼우기).
"""

from __future__ import annotations
from pathlib import Path
from .schemas import Envelope, MsgType, SummarizerPort

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)


class TemplateSummarizer:
    """L4 1차 구현 — LLM 없이 문자열 조합만으로 자연어 요약을 만든다."""

    def summarize(self, log: list[Envelope]) -> str:
        request = next((e for e in log if e.type == MsgType.REQUEST), None)
        settled = next((e for e in log if e.type == MsgType.SETTLED), None)
        if not request:
            return "요청 정보를 찾을 수 없습니다."

        item = request.payload.get("item")
        qty = request.payload.get("qty")
        cap = request.payload.get("cap_price")

        # 셀러별 OFFER 시퀀스 정리
        by_seller: dict[str, list[Envelope]] = {}
        for e in log:
            if e.type == MsgType.OFFER:
                by_seller.setdefault(e.frm, []).append(e)

        sentences: list[str] = []
        for seller_id, offers in by_seller.items():
            first = offers[0].payload["price"]
            last = offers[-1].payload["price"]
            was_rejected = any(
                e.type == MsgType.REJECT and e.to == seller_id for e in log
            )
            if len(offers) == 1 and not was_rejected:
                sentences.append(f"{seller_id}가 {qty}건에 대해 단가 {first}원으로 제시했습니다.")
            elif was_rejected:
                sentences.append(
                    f"{seller_id}가 단가 {first}원으로 최초 제시했으나 바이어 상한가({cap}원)를 "
                    f"초과해 거절되었고, 이후 {last}원으로 재조정해 다시 제시했습니다."
                )

        if settled:
            winner = settled.payload.get("seller_id")
            price = settled.payload.get("price")
            others = [s for s in by_seller if s != winner]
            if others:
                comparisons = []
                for s in others:
                    other_price = by_seller[s][-1].payload["price"]
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
            sentences.append("라운드 상한 내에 상한가를 만족하는 셀러가 없어 협상이 결렬되었습니다.")

        return " ".join(sentences)


# 지금 쓰는 인스턴스. LLM으로 바꾸고 싶으면 이 줄만 교체:
#   summarizer: SummarizerPort = LLMSummarizer(client=...)
summarizer: SummarizerPort = TemplateSummarizer()


def write_report(txid: str, log: list[Envelope]) -> Path:
    """리포트는 오직 log로부터만 만들어진다 (에이전트가 직접 쓰지 않음)."""
    text = summarizer.summarize(log)
    path = REPORT_DIR / f"{txid}.txt"
    lines = [f"거래ID: {txid}", "", "[자연어 요약]", text, "", "[원문 로그]"]
    for e in log:
        lines.append(f"{e.ts}  {e.type.value:8s}  {e.frm} -> {e.to}  {e.payload}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path