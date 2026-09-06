"""
L1 — 중앙 서버 + 로그 append
로그를 먼저 붙여야 메시지 계층을 나중에 다시 안 건드린다 (문서 2절 원칙).

[저장 구조]
- 거래(txid)별 원문 로그는 텍스트 파일(JSON Lines)로 저장 (data/logs/{txid}.jsonl)
- 거래 요약은 SQLite에 저장하고, 로그 파일 "경로"를 같이 남긴다
  (멘토 피드백: "로그는 텍스트 파일로, 그걸 가리키는 경로만 DB에 저장" 원칙 반영)
- 나중에 MySQL(RDS)로 옮길 때도 create_engine()의 연결 문자열 한 줄만 바꾸면 된다
  (SQLite 전용 문법을 쓰지 않고 SQLAlchemy ORM 표준 쿼리만 사용했기 때문)

[카탈로그 출처는 세 가지 — CATALOG_SOURCE 참고]
- snapshot : Nexar(Octopart) API 응답 스냅샷을 읽는다 (기본값). 외부 ERP가 없으므로 발주서는 안 만든다
- sqlite   : 화면에서 등록한 셀러 카탈로그

이 파일이 저장을 흡수하므로 negotiate.py / report.py 는 출처를 알지 못한다.
(다만 MOQ 스크리닝은 negotiate.py 에 한 절로 남아 있다 — 스냅샷도 MOQ 를 갖고 있다)
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from sqlalchemy import create_engine, Column, String, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker

from .schemas import Envelope, SellerRegister, Item, LEGACY_ITEMS
from . import snapshot_catalog


class CatalogReadOnly(RuntimeError):
    """카탈로그의 주인이 외부일 때 쓰기를 시도한 경우. API 계층에서 409로 바꾼다."""

# ── 카탈로그 출처 ────────────────────────────────────────────────────────────
# snapshot : 커밋된 Nexar(Octopart) 응답 원본을 읽는다 (app/snapshot_catalog.py).
#            API 키 없이 실데이터로 돌리기 위한 경로다. 환산·합성 가정은 그쪽에 있다.
#            **기본값** — 아무것도 설정하지 않아도 실데이터로 돈다.
# sqlite   : 화면에서 직접 등록한 셀러 카탈로그
#
# 기본값이 sqlite 에서 snapshot 으로 바뀌었다. 지어낸 시연용 값보다 실제 유통
# 데이터로 첫 화면이 뜨는 편이 낫고, 도메인이 제조업에서 유통으로 옮겨갔다.
CATALOG_SOURCE = os.getenv("CATALOG_SOURCE", "snapshot").lower()

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


Base.metadata.create_all(engine)  # 테이블 없으면 최초 실행 시 자동 생성 (스키마는 여기 한 곳에서만 정의)


def _row_to_seller(r: SellerCatalogRow) -> SellerRegister:
    return SellerRegister(
        seller_id=r.seller_id, item=Item(r.item), qty=r.qty,
        offer_price=r.offer_price, floor_price=r.floor_price,
        spec=r.spec, lead_time_days=r.lead_time_days, min_qty=r.min_qty or 0,
    )


class CentralStore:
    """
    거래ID(txid) 기준으로 모든 걸 격리한다 (M-04 원칙 선반영).
    카탈로그·거래요약은 SQLite, 원문 로그는 파일 — 서버 재시작해도 둘 다 남는다.
    """

    # ── 셀러 등록 (화면 1) ──
    def register_seller(self, offer: SellerRegister) -> None:
        if CATALOG_SOURCE == "snapshot":
            raise CatalogReadOnly(
                "CATALOG_SOURCE=snapshot 에서는 카탈로그의 주인이 외부(Nexar/Octopart)입니다. "
                "내용을 바꾸려면 scripts/nexar_snapshot.py 로 스냅샷을 다시 뜨세요."
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

    def sellers_for(self, item: str, qty: int | None = None) -> list[SellerRegister]:
        if CATALOG_SOURCE == "snapshot":
            # qty 를 받으면 그 수량에 맞는 가격구간을 고른다. 호출부가 아직 수량을
            # 넘기지 않아서 기본값은 최소수량 구간 — 자격 없는 볼륨가를 부르지 않는다.
            return snapshot_catalog.sellers_for(item, qty)
        with SessionLocal() as session:
            rows = session.query(SellerCatalogRow).filter_by(item=item).all()
            return [_row_to_seller(r) for r in rows]

    def list_sellers(self) -> list[SellerRegister]:
        """등록된 셀러 전체 조회 (조회/디버깅용, 화면 API가 사용)."""
        if CATALOG_SOURCE == "snapshot":
            return snapshot_catalog.list_sellers()
        with SessionLocal() as session:
            rows = session.query(SellerCatalogRow).all()
            return [_row_to_seller(r) for r in rows]

    def list_items(self) -> list[dict]:
        """선택 가능한 품목 목록 (화면의 드롭다운용)."""
        if CATALOG_SOURCE == "snapshot":
            return snapshot_catalog.list_items()
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
            }

    def set_approval(self, txid: str, approval: str) -> None:
        """승인/거절 처리 — approve/reject 엔드포인트에서 호출."""
        with SessionLocal() as session:
            row = session.get(DealRow, txid)
            if not row:
                return
            row.approval = approval
            session.commit()


# 프로세스 전역에서 공유하는 단일 인스턴스 (프로토타입 기준. 실제 분리 시 이 클래스를
# 별도 프로세스의 API 서버로 감싸고, 다른 컴포넌트는 HTTP로 호출하도록만 바꾸면 된다)
store = CentralStore()