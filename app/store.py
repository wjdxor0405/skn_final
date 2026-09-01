"""
L1 — 중앙 서버 + 로그 append
로그를 먼저 붙여야 메시지 계층을 나중에 다시 안 건드린다 (문서 2절 원칙).

[SQLite 연동 버전]
- 거래(txid)별 원문 로그는 텍스트 파일(JSON Lines)로 저장 (data/logs/{txid}.jsonl)
- 카탈로그(등록된 셀러 오퍼)와 거래 요약은 SQLite에 저장, 로그 파일 "경로"를 같이 저장
  (멘토 피드백: "로그는 텍스트 파일로, 그걸 가리키는 경로만 DB에 저장" 원칙 반영)
- 이 파일 밖(negotiate.py, main.py, report.py)은 CentralStore의 메서드 시그니처가
  그대로라서 단 한 줄도 안 바뀐다 — 인메모리 → SQLite 전환이 이 파일 안에서만 끝난다.
- 나중에 MySQL(RDS)로 옮길 때도 create_engine()의 연결 문자열 한 줄만 바꾸면 된다.
  (SQLite 전용 문법을 쓰지 않고 SQLAlchemy ORM 표준 쿼리만 사용했기 때문)
"""

from __future__ import annotations
import json
from pathlib import Path
from sqlalchemy import create_engine, Column, String, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker

from .schemas import Envelope, SellerRegister, Item

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


class DealRow(Base):
    __tablename__ = "deals"
    txid = Column(String, primary_key=True)
    status = Column(String, nullable=False)
    seller_id = Column(String, nullable=True)
    item = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    price = Column(Integer, nullable=True)
    approval = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    log_path = Column(String, nullable=False)  # 원문 로그 텍스트 파일 경로 (멘토 지침 반영)


Base.metadata.create_all(engine)  # 테이블 없으면 최초 실행 시 자동 생성 (스키마는 여기 한 곳에서만 정의)


class CentralStore:
    """
    거래ID(txid) 기준으로 모든 걸 격리한다 (M-04 원칙 선반영).
    카탈로그·거래요약은 SQLite, 원문 로그는 파일 — 서버 재시작해도 둘 다 남는다.
    """

    # ── 셀러 등록 (화면 1) ──
    def register_seller(self, offer: SellerRegister) -> None:
        with SessionLocal() as session:
            row = SellerCatalogRow(
                seller_id=offer.seller_id,
                item=offer.item.value,
                qty=offer.qty,
                offer_price=offer.offer_price,
                floor_price=offer.floor_price,
            )
            session.add(row)
            session.commit()

    def sellers_for(self, item: str) -> list[SellerRegister]:
        with SessionLocal() as session:
            rows = session.query(SellerCatalogRow).filter_by(item=item).all()
            return [
                SellerRegister(
                    seller_id=r.seller_id, item=Item(r.item), qty=r.qty,
                    offer_price=r.offer_price, floor_price=r.floor_price,
                )
                for r in rows
            ]

    def list_sellers(self) -> list[SellerRegister]:
        """등록된 셀러 전체 조회 (조회/디버깅용, 화면 API가 사용)."""
        with SessionLocal() as session:
            rows = session.query(SellerCatalogRow).all()
            return [
                SellerRegister(
                    seller_id=r.seller_id, item=Item(r.item), qty=r.qty,
                    offer_price=r.offer_price, floor_price=r.floor_price,
                )
                for r in rows
            ]

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
                "txid": row.txid, "status": row.status, "seller_id": row.seller_id,
                "item": row.item, "qty": row.qty, "price": row.price,
                "approval": row.approval, "reason": row.reason, "log_path": row.log_path,
            }

    def set_approval(self, txid: str, approval: str) -> None:
        """승인/거절 처리 — approve/reject 엔드포인트에서 호출."""
        with SessionLocal() as session:
            row = session.get(DealRow, txid)
            if row:
                row.approval = approval
                session.commit()


# 프로세스 전역에서 공유하는 단일 인스턴스 (프로토타입 기준. 실제 분리 시 이 클래스를
# 별도 프로세스의 API 서버로 감싸고, 다른 컴포넌트는 HTTP로 호출하도록만 바꾸면 된다)
store = CentralStore()