"""
L3 — 중앙 서버 매칭 + 바로구매 성사
지난 리뷰에서 지적된 "협상 라운드 상한 없으면 REJECT<->OFFER 무한루프 위험"을
MAX_ROUNDS로 반영했다. 초과 시 해당 셀러는 자동으로 결렬 처리한다.

[v3 확정안 반영]
- 매칭 조건에 사양·납기 추가 (기존엔 수량만 체크)
- 조건 미달 셀러는 협상 라운드에 들어가지도 못하고 "제외" 사유가 로그에 남는다
  (v3가 "룰로도 걸러지는 감사 검증"이라 명시한 지점 — 재고초과/납기초과 주장을 사전 차단)
- 실패를 2종류로 구분: NO_MATCH(조건 맞는 셀러 자체 없음) / NO_DEAL(협상은 했으나 결렬)

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


def _spec_matches(request_spec: str, seller_spec: str) -> bool:
    """사양 매칭 — v3의 "fuzzy match" 자리. 지금은 대소문자 무시 부분일치로 단순 구현."""
    if not request_spec:  # 사양 불문
        return True
    return request_spec.strip().lower() in seller_spec.strip().lower()


def _screen_candidates(store: CentralStore, txid: str, request: BuyerRequest) -> list[SellerRegister]:
    """
    1차 스크리닝 — 협상 이전에 재고·사양·납기를 검사한다.
    떨어진 셀러는 라운드에 들어가지 않고, 왜 떨어졌는지 로그에 즉시 남는다
    (v3: "재고 10대인데 100대 가능하다고 응답" 같은 할루시네이션을 룰로 사전 차단).
    """
    passed: list[SellerRegister] = []
    for seller in store.sellers_for(request.item):
        reasons = []
        if seller.qty < request.qty:
            reasons.append(f"재고부족(보유 {seller.qty} < 요청 {request.qty})")
        # MOQ는 재고와 부등호 방향이 반대다 — 주문량이 최소주문량 "이상"이어야 통과.
        # Odoo 공급처 단가표(product.supplierinfo.min_qty)에서 들어온다. 0이면 제약 없음.
        if seller.min_qty and request.qty < seller.min_qty:
            reasons.append(f"최소주문량 미달(요청 {request.qty} < MOQ {seller.min_qty})")
        if not _spec_matches(request.spec, seller.spec):
            reasons.append(f"사양불일치(요구 '{request.spec}' vs 보유 '{seller.spec}')")
        if seller.lead_time_days > request.max_lead_time_days:
            reasons.append(f"납기초과(제시 {seller.lead_time_days}일 > 허용 {request.max_lead_time_days}일)")

        if reasons:
            store.append(Envelope(**{
                "from": "central", "to": seller.seller_id, "type": MsgType.REJECT, "txid": txid,
                "payload": {"stage": "screening", "reason": ", ".join(reasons)},
            }))
        else:
            passed.append(seller)
    return passed


def run_standard_order(store: CentralStore, request: BuyerRequest,
                       seller_id: str, price: int, note: str) -> dict:
    """
    표준계약 범위 안의 발주 — **협상 라운드가 없다.**

    기존 거래처와 이미 조건이 정해져 있으면 매번 값을 다시 다툴 이유가 없다.
    그래서 이 경로는 REQUEST → OFFER → ACCEPT → SETTLED 네 줄만 남기고 끝난다.
    로그 형식은 협상 경로와 같으므로 리포트·발주서 생성은 그대로 재사용된다.

    에이전트가 "언제 안 나서는가"를 코드로 보여주는 자리이기도 하다.
    """
    txid = new_txid()
    common = {"txid": txid}

    store.append(Envelope(**{
        "from": "buyer", "to": seller_id, "type": MsgType.REQUEST, **common,
        "payload": {
            "item": request.item, "qty": request.qty, "cap_price": request.cap_price,
            "spec": request.spec, "max_lead_time_days": request.max_lead_time_days,
            "route": "STANDARD",
        },
    }))
    store.append(Envelope(**{
        "from": seller_id, "to": "buyer", "type": MsgType.OFFER, **common,
        "payload": {"price": price, "round": 0, "message": note},
    }))
    store.append(Envelope(**{
        "from": "buyer", "to": seller_id, "type": MsgType.ACCEPT, **common,
        "payload": {"price": price, "message": "표준계약 범위 내이므로 협상 없이 수용합니다."},
    }))

    summary = {
        "txid": txid, "status": "SETTLED", "seller_id": seller_id,
        "item": request.item, "qty": request.qty, "price": price,
        "approval": "PENDING", "reason": note,
    }
    store.append(Envelope(**{
        "from": "central", "to": "*", "type": MsgType.SETTLED, **common,
        "payload": {"seller_id": seller_id, "price": price, "rounds": 0},
    }))
    store.set_deal(txid, summary)
    return summary


def run_negotiation(store: CentralStore, request: BuyerRequest) -> dict:
    txid = new_txid()
    seller_agent, buyer_agent = _build_agents()

    # 1. REQUEST 브로드캐스트
    store.append(Envelope(**{
        "from": "buyer", "to": "*", "type": MsgType.REQUEST, "txid": txid,
        "payload": {
            "item": request.item, "qty": request.qty, "cap_price": request.cap_price,
            "spec": request.spec, "max_lead_time_days": request.max_lead_time_days,
        },
    }))

    # 2. 1차 스크리닝 (재고·사양·납기) — 통과 못 하면 라운드 자체에 안 들어감
    candidates = _screen_candidates(store, txid, request)

    if not candidates:
        summary = {
            "txid": txid, "status": "FAILED", "fail_type": "NO_MATCH",
            "item": request.item, "qty": request.qty,
            "reason": "조건(재고·사양·납기)을 만족하는 판매자가 없습니다. 조건 완화가 필요합니다.",
        }
        store.set_deal(txid, summary)
        return summary

    # 3. 스크리닝 통과 셀러끼리 가격 협상
    accepted: list[tuple[SellerRegister, int]] = []

    for seller in candidates:
        last_reject_price: int | None = None
        for round_no in range(1, MAX_ROUNDS + 1):
            raw_price, seller_msg = seller_agent.decide(seller, round_no, last_reject_price)
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
                # 이 엔벨로프는 셀러에게 가는 메시지다 — 상한가 액수를 싣지 않는다.
                # (리포트는 REQUEST 쪽 payload에서 상한가를 읽으므로 영향 없다)
                reject_payload = {"reason": "상한가 초과", "message": buyer_msg}
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
            "txid": txid, "status": "FAILED", "fail_type": "NO_DEAL",
            "item": request.item, "qty": request.qty,
            "reason": "조건에 맞는 판매자는 있었으나, 라운드 상한 내 가격 합의에 실패했습니다.",
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