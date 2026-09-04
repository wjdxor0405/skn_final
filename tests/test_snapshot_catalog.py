#!/usr/bin/env python3
"""
CATALOG_SOURCE=snapshot 회귀 검증.

pytest 없이 그대로 실행된다 (이 저장소에는 아직 테스트 의존성이 없다):

    python tests/test_snapshot_catalog.py

CATALOG_SOURCE 는 store.py 가 임포트 시점에 한 번만 읽는다. 그래서 모드별 검사는
각각 별도 프로세스에서 돌린다 — 한 프로세스 안에서 모드를 바꿔치기하면
실제 실행과 다른 것을 검사하게 된다.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nexar_snapshot_sample.json"


# ── 자식 프로세스에서 도는 검사들 ────────────────────────────────────────────
def check_snapshot_mode() -> None:
    """CATALOG_SOURCE=snapshot — 로더와 store 분기 (인가 필터 끈 상태)."""
    from app.store import CentralStore, CatalogReadOnly
    from app import snapshot_catalog as sc
    from app.schemas import SellerRegister

    store = CentralStore()

    # 품목 목록: 환산에 실패한 PASSIVE-C 는 후보가 없으므로 목록에도 없다.
    codes = [i["code"] for i in store.list_items()]
    assert codes == ["CONN-B", "MCU-A"], codes

    # 같은 품목에 판매자가 2곳 이상 — 협상 후보가 실제로 생긴다 (완료 기준)
    mcu = store.sellers_for("MCU-A")
    assert {s.seller_id for s in mcu} == {"Digi-Key", "Mouser"}, mcu

    # 전부 int 로 들어왔다 (schemas.SellerRegister 가 int 스키마다)
    for s in mcu:
        assert isinstance(s, SellerRegister)
        assert isinstance(s.offer_price, int) and isinstance(s.floor_price, int)
        assert isinstance(s.min_qty, int)

    dk = next(s for s in mcu if s.seller_id == "Digi-Key")
    mo = next(s for s in mcu if s.seller_id == "Mouser")

    # 수량을 모르면 최소수량 구간 = 가장 비싼 단가. USD 2.34 × 1400 = 3276원
    assert dk.offer_price == 3276, dk.offer_price
    # floor_price 는 유일한 합성값. 3276 × 0.90 = 2948.4 → 2948
    assert dk.floor_price == 2948, dk.floor_price
    # 같은 판매자의 Tape & Reel 오퍼($2.50)는 더 비싸서 접혔다
    assert dk.offer_price < 3500

    # moq=null 이 0 으로 정규화됐다 (그대로 넘기면 pydantic 이 거부한다)
    assert mo.min_qty == 1, mo.min_qty          # 구간 수량 1, MOQ 없음
    assert mo.lead_time_days == 0               # factoryLeadDays=null → 0
    assert mo.qty == 8000
    # 재고가 있으면 공장 리드타임(21일)이 아니라 재고 출하다 — 둘은 배타적이지 않다
    assert dk.qty == 12000 and dk.lead_time_days == 0, (dk.qty, dk.lead_time_days)
    # 소량에서는 Mouser 가 싸다
    assert mo.offer_price == 2940, mo.offer_price
    assert mo.offer_price < dk.offer_price

    # 수량을 주면 해당 가격구간으로 뒤집힌다 (함정 4)
    mcu500 = {s.seller_id: s for s in store.sellers_for("MCU-A", qty=500)}
    assert mcu500["Digi-Key"].offer_price == 2268   # $1.62 구간
    assert mcu500["Mouser"].offer_price == 2590     # $1.85 구간
    assert mcu500["Digi-Key"].offer_price < mcu500["Mouser"].offer_price
    # 그 단가를 받으려면 해당 수량 이상을 사야 한다 → MOQ 로 표현된다
    assert mcu500["Digi-Key"].min_qty == 500
    assert mcu500["Mouser"].min_qty == 250

    # inventoryLevel=null → 0 (부풀리지 않는다)
    conn = store.sellers_for("CONN-B")
    assert len(conn) == 1 and conn[0].qty == 0, conn
    # convertedPrice 가 있으면 API 환산을 쓴다 — 우리 고정 환율(0.85×1400=1190)이 아니라
    # 응답에 실린 1150.4 를 반올림한 값
    assert conn[0].offer_price == 1150, conn[0].offer_price
    # 반대로 재고가 없으면 공장 리드타임이 적용된다
    assert conn[0].lead_time_days == 14, conn[0].lead_time_days

    # 카탈로그의 주인이 외부다
    try:
        store.register_seller(SellerRegister(
            seller_id="X", item="MCU-A", qty=1, offer_price=1, floor_price=1))
    except CatalogReadOnly:
        pass
    else:
        raise AssertionError("snapshot 모드에서 register_seller 가 막히지 않았다")

    # 버린 행이 조용히 사라지지 않고 이유와 함께 남는다
    diag = sc.diagnostics()
    assert diag["dropped"]["가격 없는 오퍼"] == 1                    # NoPrice Inc
    assert diag["dropped"]["환율 미정의(EUR)"] == 1                  # EuroDist
    assert diag["dropped"]["환산 단가 0원"] == 1                     # PASSIVE-C
    assert diag["dropped"]["같은 판매자의 중복 오퍼"] == 1           # Digi-Key 2건
    assert diag["single_seller_items"] == ["CONN-B"], diag
    assert diag["moq_field"] == "moq"
    # 단가 출처가 구분돼 남는다 — 어디까지가 API 값이고 어디부터가 우리 가정인지
    assert diag["currency"] == "KRW"
    assert diag["price_sources"] == {"api": 1, "폴백환율": 2}, diag["price_sources"]
    print("  snapshot 모드 OK —", {k: v for k, v in diag.items() if k != "path"})


def check_sqlite_mode() -> None:
    """CATALOG_SOURCE 기본값 — 기존 동작이 그대로인지 (회귀 없음)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import store as store_mod
    from app.schemas import SellerRegister

    assert store_mod.CATALOG_SOURCE == "sqlite"

    # 실제 data/mmvp.db 를 건드리지 않도록 임시 DB로 갈아끼운다.
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'test.db'}")
        store_mod.Base.metadata.create_all(engine)
        store_mod.SessionLocal = sessionmaker(bind=engine)

        store = store_mod.CentralStore()
        assert store.sellers_for("A4용지") == []
        # 셀러가 없으면 옛 3종을 기본 선택지로 돌려주던 동작
        assert [i["code"] for i in store.list_items()] == list(store_mod.LEGACY_ITEMS)

        store.register_seller(SellerRegister(
            seller_id="셀러 A", item="A4용지", qty=100,
            offer_price=45, floor_price=38, spec="80g", lead_time_days=3))
        rows = store.sellers_for("A4용지")
        assert len(rows) == 1 and rows[0].offer_price == 45, rows
        assert len(store.list_sellers()) == 1
        assert [i["code"] for i in store.list_items()] == ["A4용지"]
    print("  sqlite 모드 OK — 등록·조회 왕복 정상")


def check_odoo_mode() -> None:
    """CATALOG_SOURCE=odoo — snapshot 분기가 odoo 경로를 가리지 않는지."""
    from app import store as store_mod

    assert store_mod.CATALOG_SOURCE == "odoo"

    calls = []

    class FakeClient:
        def vendor_offers(self, item):
            calls.append(("vendor_offers", item))
            return [{"vendor": "공급처 A", "price": 1000, "spec": "",
                     "lead_time_days": 5, "min_qty": 10}]

        def purchasable_products(self, prefix=""):
            calls.append(("purchasable_products", prefix))
            return [{"code": "RM-X", "name": "자재 X", "spec": "", "on_hand": 0.0}]

    store_mod.get_client = lambda *a, **k: FakeClient()

    store = store_mod.CentralStore()
    rows = store.sellers_for("RM-X")
    assert len(rows) == 1 and rows[0].seller_id == "공급처 A", rows
    # floor_price 는 여전히 store.py 가 ODOO_FLOOR_RATIO 로 유도한다
    assert rows[0].floor_price == round(1000 * store_mod.ODOO_FLOOR_RATIO), rows[0]
    assert rows[0].min_qty == 10
    assert [i["code"] for i in store.list_items()] == ["RM-X"]
    assert len(store.list_sellers()) == 1
    assert ("vendor_offers", "RM-X") in calls
    print("  odoo 모드 OK — 분기가 그대로 Odoo 로 간다")


def check_snapshot_authorized() -> None:
    """SNAPSHOT_AUTHORIZED_ONLY 기본값(켜짐) — 비인가 판매자가 후보에서 빠진다."""
    from app.store import CentralStore
    from app import snapshot_catalog as sc

    assert sc.SNAPSHOT_AUTHORIZED_ONLY is True
    store = CentralStore()

    # NoPrice Inc 는 isAuthorized=false — 가격이 없어서가 아니라 인가가 아니라서 빠진다
    diag = sc.diagnostics()
    assert diag["authorized_only"] is True
    assert diag["dropped"]["인가 판매자 아님"] == 1, diag["dropped"]
    assert "가격 없는 오퍼" not in diag["dropped"], diag["dropped"]

    # 걸러도 협상 후보는 그대로 2곳 이상이다 (완료 기준이 필터와 양립한다)
    mcu = store.sellers_for("MCU-A")
    assert {s.seller_id for s in mcu} == {"Digi-Key", "Mouser"}, mcu
    print("  snapshot 인가필터 OK — 비인가 1곳 제외, 후보는 2곳 유지")


CHECKS = {
    "snapshot": check_snapshot_mode,
    "snapshot_authorized": check_snapshot_authorized,
    "sqlite": check_sqlite_mode,
    "odoo": check_odoo_mode,
}


# ── pytest 가 나중에 들어와도 그대로 잡히도록 ────────────────────────────────
def _run(mode: str) -> None:
    env = {**os.environ, "CATALOG_SOURCE": mode, "PYTHONPATH": str(ROOT)}
    if mode.startswith("snapshot"):
        env["CATALOG_SOURCE"] = "snapshot"
        env["SNAPSHOT_PATH"] = str(FIXTURE)
        env["SNAPSHOT_FX"] = "USD=1400"
        env["SNAPSHOT_FLOOR_RATIO"] = "0.90"
        # 기본 검사는 필터 없이, 전용 검사는 기본값(켜짐)으로 돈다
        env["SNAPSHOT_AUTHORIZED_ONLY"] = "false" if mode == "snapshot" else "true"
    else:
        env.pop("SNAPSHOT_PATH", None)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--mode", mode],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        raise AssertionError(f"[{mode}] 실패\n{proc.stderr}")


def test_snapshot_mode(): _run("snapshot")
def test_snapshot_authorized(): _run("snapshot_authorized")
def test_sqlite_mode(): _run("sqlite")
def test_odoo_mode(): _run("odoo")


if __name__ == "__main__":
    if "--mode" in sys.argv:
        CHECKS[sys.argv[sys.argv.index("--mode") + 1]]()
    else:
        for name in CHECKS:
            print(f"[{name}]")
            _run(name)
        print("\n전부 통과")
