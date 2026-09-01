"""
L3 — 중앙 서버 매칭 + 바로구매 성사
지난 리뷰에서 지적된 "협상 라운드 상한 없으면 REJECT<->OFFER 무한루프 위험"을
MAX_ROUNDS로 반영했다. 초과 시 해당 셀러는 자동으로 결렬 처리한다.

에이전트 구현체는 NEGOTIATOR_MODE 환경변수로 전환된다:
  - "rule" (기본값): RuleBasedSellerAgent/RuleBasedBuyerAgent — API 비용 없음
  - "llm": OpenAISellerAgent/OpenAIBuyerAgent — OPENAI_API_KEY 필요
어느 쪽이든 SellerAgentPort/BuyerAgentPort 인터페이스가 같아서 이 파일의
나머지 로직(라운드 진행, 로그 적재, 최저가 채택)은 전혀 안 바뀐다.
"""

from __future__ import annotations
import os
from .schemas import Envelope, MsgType, BuyerRequest, SellerRegister, new_txid
from .agents import RuleBasedSellerAgent, RuleBasedBuyerAgent, SellerAgentPort, BuyerAgentPort
from .audit import audit_seller_offer, audit_buyer_accept
from .store import CentralStore

MAX_ROUNDS = 3  # 하한선: 이 이상 왕복해도 안 맞으면 결렬 (무한루프 방지)


def _build_agents() -> tuple[SellerAgentPort, BuyerAgentPort]:
    mode = os.environ.get("NEGOTIATOR_MODE", "rule").lower()
    if mode == "llm":
        from .llm_agents import OpenAISellerAgent, OpenAIBuyerAgent  # 필요할 때만 임포트(키 없어도 rule 모드는 동작)
        return OpenAISellerAgent(), OpenAIBuyerAgent()
    return RuleBasedSellerAgent(), RuleBasedBuyerAgent()


def run_negotiation(store: CentralStore, request: BuyerRequest) -> dict:
    txid = new_txid()
    seller_agent, buyer_agent = _build_agents()

    # 1. REQUEST 브로드캐스트
    store.append(Envelope(**{
        "from": "buyer", "to": "*", "type": MsgType.REQUEST, "txid": txid,
        "payload": {"item": request.item, "qty": request.qty, "cap_price": request.cap_price},
    }))

    candidates = [s for s in store.sellers_for(request.item) if s.qty >= request.qty]
    accepted: list[tuple[SellerRegister, int]] = []

    for seller in candidates:
        last_reject_price: int | None = None
        for round_no in range(1, MAX_ROUNDS + 1):
            raw_price, seller_msg = seller_agent.decide(seller, round_no, request.cap_price, last_reject_price)
            price, seller_audit_note = audit_seller_offer(raw_price, seller.floor_price)

            offer_payload = {"price": price, "round": round_no, "message": seller_msg}
            if seller_audit_note:
                offer_payload["audit_note"] = seller_audit_note
            store.append(Envelope(**{
                "from": seller.seller_id, "to": "buyer", "type": MsgType.OFFER, "txid": txid,
                "payload": offer_payload,
            }))

            raw_accept, buyer_msg = buyer_agent.decide(request, price, seller_msg)
            accept, buyer_audit_note = audit_buyer_accept(raw_accept, price, request.cap_price)

            if accept:
                accept_payload = {"price": price, "message": buyer_msg}
                if buyer_audit_note:
                    accept_payload["audit_note"] = buyer_audit_note
                store.append(Envelope(**{
                    "from": "buyer", "to": seller.seller_id, "type": MsgType.ACCEPT, "txid": txid,
                    "payload": accept_payload,
                }))
                accepted.append((seller, price))
                break
            else:
                reject_payload = {"reason": "상한가 초과", "cap_price": request.cap_price, "message": buyer_msg}
                if buyer_audit_note:
                    reject_payload["audit_note"] = buyer_audit_note
                store.append(Envelope(**{
                    "from": "buyer", "to": seller.seller_id, "type": MsgType.REJECT, "txid": txid,
                    "payload": reject_payload,
                }))
                last_reject_price = price
                # 하한선: 라운드 상한 도달 시 이 셀러는 자동 결렬 (재시도 없음)

    if not accepted:
        summary = {
            "txid": txid, "status": "FAILED", "item": request.item, "qty": request.qty,
            "reason": "라운드 상한 내 조건을 만족하는 셀러 없음",
        }
        store.set_deal(txid, summary)
        return summary

    # 최저가 채택
    winner, price = min(accepted, key=lambda x: x[1])

    store.append(Envelope(**{
        "from": "central", "to": "*", "type": MsgType.SETTLED, "txid": txid,
        "payload": {"seller_id": winner.seller_id, "price": price},
    }))

    summary = {
        "txid": txid,
        "status": "SETTLED",
        "seller_id": winner.seller_id,
        "item": request.item,
        "qty": request.qty,
        "price": price,
        "approval": "PENDING",  # L4에서 사람이 바꾼다
    }
    store.set_deal(txid, summary)
    return summary
