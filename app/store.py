"""
L1 — 중앙 서버 + 로그 append
로그를 먼저 붙여야 메시지 계층을 나중에 다시 안 건드린다 (문서 2절 원칙).

[저장 구조]
- 거래(txid)별 원문 로그는 텍스트 파일(JSON Lines)로 저장 (data/logs/{txid}.jsonl)
- 거래 요약은 SQLite에 저장하고, 로그 파일 "경로"를 같이 남긴다
  (멘토 피드백: "로그는 텍스트 파일로, 그걸 가리키는 경로만 DB에 저장" 원칙 반영)
- 나중에 MySQL(RDS)로 옮길 때도 create_engine()의 연결 문자열 한 줄만 바꾸면 된다
  (SQLite 전용 문법을 쓰지 않고 SQLAlchemy ORM 표준 쿼리만 사용했기 때문)

[카탈로그 출처는 두 가지 — CATALOG_SOURCE 참고]
- sqlite : 화면에서 등록한 셀러 카탈로그 (기존 동작)
- odoo   : Odoo의 공급처 단가표를 읽고, 낙찰 시 Odoo 발주서 초안까지 만든다

이 파일이 저장·연동을 모두 흡수하므로 negotiate.py / report.py 는 출처를 알지 못한다.
(다만 MOQ는 Odoo에만 있던 제약이라 negotiate.py 의 스크리닝에 한 절이 추가돼 있다)
"""

from __future__ import annotations
import html
import json
import logging
import os
from pathlib import Path
from sqlalchemy import create_engine, Column, String, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker

from .schemas import Envelope, SellerRegister, Item, LEGACY_ITEMS
from .odoo_client import odoo

log = logging.getLogger(__name__)


class CatalogReadOnly(RuntimeError):
    """카탈로그의 주인이 Odoo일 때 쓰기를 시도한 경우. API 계층에서 409로 바꾼다."""

# ── 카탈로그 출처 ────────────────────────────────────────────────────────────
# sqlite : 화면에서 직접 등록한 셀러 카탈로그 (기존 동작, 기본값)
# odoo   : Odoo의 공급처 단가표(product.supplierinfo)를 카탈로그로 읽는다
CATALOG_SOURCE = os.getenv("CATALOG_SOURCE", "sqlite").lower()

# Odoo 모드에서만 쓰는 두 가지 가정 — Odoo에 해당 데이터가 아예 없어서 유도해야 한다.
#
#  floor_price : 공급처의 "최저 수용가"는 Odoo에 없다(공급처가 알려줄 리 없는 값이다).
#                제시가의 이 비율로 가정한다. 협상 여지의 크기를 정하는 손잡이.
#  qty         : 공급처의 보유재고·공급능력도 Odoo에 없다(qty_available은 '우리' 재고다).
#                사실상 무제한으로 두고, 실질 제약은 MOQ(min_qty)가 담당한다.
ODOO_FLOOR_RATIO = float(os.getenv("ODOO_FLOOR_RATIO", "0.90"))
ODOO_VENDOR_CAPACITY = int(os.getenv("ODOO_VENDOR_CAPACITY", "1000000"))
ODOO_ITEM_PREFIX = os.getenv("ODOO_ITEM_PREFIX", "")

# 화면에 띄울 발주서 링크의 주소. 브라우저가 도는 곳에서 접근 가능한 주소여야 하므로
# 서버가 쓰는 ODOO_URL(localhost일 수 있다)과 별개로 둔다.
ODOO_PUBLIC_URL = os.getenv(
    "ODOO_PUBLIC_URL", os.getenv("ODOO_URL", "http://localhost:8069")).rstrip("/")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "mmvp.db"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# echo=False로 두되, 디버깅 필요하면 True로 바꿔서 실제 SQL 쿼리 로그를 볼 수 있다
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class SellerCatalogRow(Base):
    __tablename__ = "seller_catalog"
    id = Column(Integer, primary_key=True, autoincrement=True)
    seller_id = Column(String, nullable=False)
    item = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    offer_price = Column(Integer, nullable=False)
    floor_price = Column(Integer, nullable=False)
    spec = Column(String, nullable=False, default="")
    lead_time_days = Column(Integer, nullable=False, default=0)
    min_qty = Column(Integer, nullable=False, default=0)


class DealRow(Base):
    __tablename__ = "deals"
    txid = Column(String, primary_key=True)
    status = Column(String, nullable=False)
    fail_type = Column(String, nullable=True)  # "NO_MATCH"(조건 맞는 셀러 자체 없음) / "NO_DEAL"(협상은 했으나 결렬)
    seller_id = Column(String, nullable=True)
    item = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    price = Column(Integer, nullable=True)
    approval = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    log_path = Column(String, nullable=False)  # 원문 로그 텍스트 파일 경로 (멘토 지침 반영)
    # CATALOG_SOURCE=odoo 에서 낙찰 시 만들어지는 Odoo 발주서 초안
    po_id = Column(Integer, nullable=True)
    po_name = Column(String, nullable=True)
    po_error = Column(Text, nullable=True)   # 발주서를 못 만들었을 때 그 이유 (조용히 넘어가지 않기 위해)


Base.metadata.create_all(engine)  # 테이블 없으면 최초 실행 시 자동 생성 (스키마는 여기 한 곳에서만 정의)


def _row_to_seller(r: SellerCatalogRow) -> SellerRegister:
    return SellerRegister(
        seller_id=r.seller_id, item=Item(r.item), qty=r.qty,
        offer_price=r.offer_price, floor_price=r.floor_price,
        spec=r.spec, lead_time_days=r.lead_time_days, min_qty=r.min_qty or 0,
    )


def _offer_to_seller(item: str, offer: dict) -> SellerRegister:
    """Odoo 공급처 단가표 1행 -> SellerRegister. 유도되는 두 값은 위 설정 주석 참고."""
    return SellerRegister(
        seller_id=offer["vendor"],
        item=item,
        qty=ODOO_VENDOR_CAPACITY,
        offer_price=offer["price"],
        floor_price=int(round(offer["price"] * ODOO_FLOOR_RATIO)),
        spec=offer["spec"],
        lead_time_days=offer["lead_time_days"],
        min_qty=offer["min_qty"],
    )


def _chatter_body(txid: str, summary: dict, envelopes: list[Envelope]) -> str:
    """협상 로그를 발주서 chatter에 남길 HTML로. 승인자가 발주서 화면에서
    '왜 이 업체·이 가격인지'를 바로 볼 수 있게 하는 것이 목적이다."""
    rows = []
    for e in envelopes:
        payload = ", ".join(f"{k}={v}" for k, v in e.payload.items() if k != "item")
        rows.append(
            f"<li><b>{html.escape(e.type)}</b> "
            f"{html.escape(e.frm)} → {html.escape(e.to)}"
            + (f" : {html.escape(payload)}" if payload else "")
            + "</li>"
        )
    price = summary.get("price")
    price_txt = f"{price:,}원" if isinstance(price, (int, float)) else "-"
    # 표준계약 경로는 협상 라운드가 없다 — 제목이 사실과 달라지지 않게 구분한다
    standard = any(e.payload.get("route") == "STANDARD" for e in envelopes)
    title = "표준계약 자동 처리 로그 (협상 없음)" if standard else "AI 에이전트 협상 로그"
    return (
        f"<p><b>{title}</b> — 거래 {html.escape(txid)}</p>"
        f"<p>낙찰: <b>{html.escape(str(summary.get('seller_id')))}</b> / "
        f"{summary.get('qty')}개 / 단가 {price_txt}</p>"
        f"<ul>{''.join(rows)}</ul>"
        f"<p><i>이 발주서는 초안입니다. 확정은 사람이 합니다.</i></p>"
    )


class CentralStore:
    """
    거래ID(txid) 기준으로 모든 걸 격리한다 (M-04 원칙 선반영).
    카탈로그·거래요약은 SQLite, 원문 로그는 파일 — 서버 재시작해도 둘 다 남는다.
    """

    # ── 셀러 등록 (화면 1) ──
    def register_seller(self, offer: SellerRegister) -> None:
        if CATALOG_SOURCE == "odoo":
            raise CatalogReadOnly(
                "CATALOG_SOURCE=odoo 에서는 카탈로그의 주인이 Odoo입니다. "
                "공급처·단가는 Odoo에서 등록하세요: Purchase > Products > 해당 품목 > Purchase 탭."
            )
        with SessionLocal() as session:
            row = SellerCatalogRow(
                seller_id=offer.seller_id,
                item=offer.item,
                qty=offer.qty,
                offer_price=offer.offer_price,
                floor_price=offer.floor_price,
                spec=offer.spec,
                lead_time_days=offer.lead_time_days,
                min_qty=offer.min_qty,
            )
            session.add(row)
            session.commit()

    def sellers_for(self, item: str) -> list[SellerRegister]:
        if CATALOG_SOURCE == "odoo":
            return [_offer_to_seller(item, o) for o in odoo.vendor_offers(item)]
        with SessionLocal() as session:
            rows = session.query(SellerCatalogRow).filter_by(item=item).all()
            return [_row_to_seller(r) for r in rows]

    def list_sellers(self) -> list[SellerRegister]:
        """등록된 셀러 전체 조회 (조회/디버깅용, 화면 API가 사용)."""
        if CATALOG_SOURCE == "odoo":
            # 품목 수만큼 조회가 나간다. 데모 규모(3종)에서는 문제없지만
            # 품목이 많아지면 supplierinfo를 한 번에 읽어 품목별로 묶는 편이 낫다.
            out: list[SellerRegister] = []
            for product in odoo.purchasable_products(ODOO_ITEM_PREFIX):
                out += [_offer_to_seller(product["code"], o)
                        for o in odoo.vendor_offers(product["code"])]
            return out
        with SessionLocal() as session:
            rows = session.query(SellerCatalogRow).all()
            return [_row_to_seller(r) for r in rows]

    def list_items(self) -> list[dict]:
        """선택 가능한 품목 목록 (화면의 드롭다운용)."""
        if CATALOG_SOURCE == "odoo":
            return odoo.purchasable_products(ODOO_ITEM_PREFIX)
        with SessionLocal() as session:
            codes = sorted({r.item for r in session.query(SellerCatalogRow).all()})
        # 아직 등록된 셀러가 없으면 화면의 드롭다운이 비어 셀러 등록 자체를 못 하게 된다.
        # 그때는 옛 3종을 기본 선택지로 돌려준다.
        return [{"code": c, "name": c, "spec": "", "on_hand": 0.0}
                for c in (codes or LEGACY_ITEMS)]

    # ── 원문 로그: 텍스트 파일(append-only) ──
    def _log_path(self, txid: str) -> Path:
        return LOG_DIR / f"{txid}.jsonl"

    def append(self, env: Envelope) -> None:
        path = self._log_path(env.txid)
        with path.open("a", encoding="utf-8") as f:
            f.write(env.model_dump_json(by_alias=True) + "\n")

    def get_log(self, txid: str) -> list[Envelope]:
        path = self._log_path(txid)
        if not path.exists():
            return []
        envelopes: list[Envelope] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    envelopes.append(Envelope(**json.loads(line)))
        return envelopes

    # ── 거래 확정 요약: SQLite (+ 로그 파일 경로 같이 저장) ──
    def set_deal(self, txid: str, summary: dict) -> None:
        with SessionLocal() as session:
            row = session.get(DealRow, txid)
            if row is None:
                row = DealRow(txid=txid)
                session.add(row)
            row.status = summary.get("status")
            row.fail_type = summary.get("fail_type")
            row.seller_id = summary.get("seller_id")
            row.item = summary.get("item")
            row.qty = summary.get("qty")
            row.price = summary.get("price")
            row.approval = summary.get("approval")
            row.reason = summary.get("reason")
            row.log_path = str(self._log_path(txid))

            if CATALOG_SOURCE == "odoo" and summary.get("status") == "SETTLED" and not row.po_id:
                try:
                    po = odoo.create_draft_purchase_order(
                        vendor=summary["seller_id"],
                        product_code=summary["item"],
                        qty=summary["qty"],
                        price=summary["price"],
                        origin=txid,
                    )
                    row.po_id, row.po_name, row.po_error = po["id"], po["name"], None
                    odoo.post_note("purchase.order", po["id"],
                                   _chatter_body(txid, summary, self.get_log(txid)))
                except Exception as e:  # noqa: BLE001 - 아래 주석 참고
                    # 협상 결과 자체는 이미 확정된 사실이다. Odoo 쓰기는 부가 작업이므로
                    # 어떤 예외도 거래를 되돌리게 두지 않고, 실패 사실만 남겨 화면에 보인다.
                    row.po_error = str(e)
                    log.warning("발주서 초안 생성 실패 (txid=%s): %s", txid, e)

            session.commit()

    def get_deal(self, txid: str) -> dict | None:
        with SessionLocal() as session:
            row = session.get(DealRow, txid)
            if row is None:
                return None
            return {
                "txid": row.txid, "status": row.status, "fail_type": row.fail_type,
                "seller_id": row.seller_id, "item": row.item, "qty": row.qty,
                "price": row.price, "approval": row.approval, "reason": row.reason,
                "log_path": row.log_path,
                "po_id": row.po_id, "po_name": row.po_name, "po_error": row.po_error,
                "po_url": f"{ODOO_PUBLIC_URL}/odoo/purchase/{row.po_id}" if row.po_id else None,
            }

    def set_approval(self, txid: str, approval: str) -> None:
        """
        승인/거절 처리 — approve/reject 엔드포인트에서 호출.
        Odoo 모드에서는 발주서 초안까지 같이 확정/취소해서, 어느 화면에서 눌러도
        두 시스템의 상태가 갈라지지 않게 한다.
        """
        with SessionLocal() as session:
            row = session.get(DealRow, txid)
            if not row:
                return
            row.approval = approval

            if CATALOG_SOURCE == "odoo" and row.po_id:
                try:
                    if approval == "APPROVED":
                        odoo.confirm_purchase_order(row.po_id)
                        note = "협상 결과가 <b>승인</b>되어 발주서를 확정했습니다."
                    else:
                        odoo.cancel_purchase_order(row.po_id)
                        note = "협상 결과가 <b>거절</b>되어 발주서를 취소했습니다."
                    odoo.post_note("purchase.order", row.po_id,
                                   f"<p>{note} (거래 {html.escape(txid)})</p>")
                    row.po_error = None
                except Exception as e:  # noqa: BLE001 - 승인 기록 자체는 남아야 한다
                    row.po_error = str(e)
                    log.warning("발주서 상태 반영 실패 (txid=%s): %s", txid, e)

            session.commit()


# 프로세스 전역에서 공유하는 단일 인스턴스 (프로토타입 기준. 실제 분리 시 이 클래스를
# 별도 프로세스의 API 서버로 감싸고, 다른 컴포넌트는 HTTP로 호출하도록만 바꾸면 된다)
store = CentralStore()