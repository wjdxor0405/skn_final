"""
L0 계약서 (코드화)
- 품목 스키마 5필드: 품목ID, 수량, 제시가, 셀러 최저수용가, 바이어 상한가
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


# ── 고정 품목 (규격품 1종 원칙과 무관하게, 데모 편의를 위해 3종 중 선택하게 함) ──
class Item(str, Enum):
    A4용지 = "A4용지"
    토너 = "토너"
    볼트 = "볼트"


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


# ── 등록 시 입력 스키마 (5필드) ──
class SellerRegister(BaseModel):
    seller_id: str            # "셀러 A" / "셀러 B"
    item: Item
    qty: int
    offer_price: int          # 제시가
    floor_price: int          # 최저 수용가 (바이어에게 노출 금지)


class BuyerRequest(BaseModel):
    item: Item
    qty: int
    cap_price: int            # 상한가


# ── LLM 어댑터 인터페이스 (지금은 템플릿, 나중에 이 인터페이스만 만족하면 교체 가능) ──
class SummarizerPort(Protocol):
    def summarize(self, log: list[Envelope]) -> str:
        ...
