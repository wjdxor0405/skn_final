"""
L0 계약서 (코드화)
- 품목 스키마: 품목ID, 수량, 제시가, 셀러 최저수용가, 바이어 상한가 (최초 5필드)
  + 사양·납기일수 (v3 D-02) + 최소주문량 MOQ (Odoo 공급처 단가표 연동)
- 메시지 타입 5종: REQUEST, OFFER, ACCEPT, REJECT, SETTLED
- 메시지 봉투(envelope): from/to/type/txid/ts/payload
- LLM 어댑터 인터페이스: 지금은 템플릿 구현체, 나중에 파일 하나만 갈아끼우면 LLM으로 교체됨
"""

from __future__ import annotations
from enum import Enum
from typing import Any, Protocol
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import uuid


# ── 품목 코드 ──
# 예전에는 3종 고정 Enum이었다. CATALOG_SOURCE=odoo 에서는 품목 마스터가 Odoo에 있어서
# 어떤 코드가 유효한지를 이 파일이 알 수 없으므로, 코드 문자열을 그대로 받는다.
# (sqlite 모드의 기존 데모 값 "A4용지/토너/볼트"도 문자열이라 그대로 유효하다)
Item = str

# 옛 Enum이 갖고 있던 3종. sqlite 모드 목업의 기본 선택지로만 쓴다.
LEGACY_ITEMS: tuple[str, ...] = ("A4용지", "토너", "볼트")


class MsgType(str, Enum):
    REQUEST = "REQUEST"
    OFFER = "OFFER"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    SETTLED = "SETTLED"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_txid() -> str:
    # TX-YYYYMMDD-XXXX 형식 (목업 예시와 동일한 규칙)
    today = datetime.now().strftime("%Y%m%d")
    return f"TX-{today}-{uuid.uuid4().hex[:4].upper()}"


class Envelope(BaseModel):
    """모든 메시지는 반드시 이 형태로만 오간다. 확장은 payload 안에서만 (5절 원칙)."""
    frm: str = Field(alias="from")
    to: str
    type: MsgType
    txid: str
    ts: str = Field(default_factory=now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


# ── 등록 시 입력 스키마 ──
# v3 확정안 D-02 반영: 사양·납기 필드 추가 (기존 5필드에서 확장)
class SellerRegister(BaseModel):
    seller_id: str            # "셀러 A" / "셀러 B"
    item: Item
    qty: int                  # 재고
    offer_price: int          # 제시가
    floor_price: int          # 최저 수용가 (바이어에게 노출 금지)
    spec: str = ""            # 사양 (예: "80g/2500매")
    lead_time_days: int = 0   # 납기일수
    min_qty: int = 0          # 최소주문량(MOQ). Odoo product.supplierinfo.min_qty 대응.
                              # 0이면 제약 없음 — 기존 sqlite 데이터의 동작이 바뀌지 않는다.


class BuyerRequest(BaseModel):
    item: Item
    qty: int
    cap_price: int                  # 상한가
    spec: str = ""                  # 요구 사양 (빈 문자열이면 사양 불문)
    max_lead_time_days: int = 999   # 허용 최대 납기일수 (기본값: 사실상 제한 없음)


# ── LLM 어댑터 인터페이스 (지금은 템플릿, 나중에 이 인터페이스만 만족하면 교체 가능) ──
class SummarizerPort(Protocol):
    def summarize(self, log: list[Envelope]) -> str:
        ...