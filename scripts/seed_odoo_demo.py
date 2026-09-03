"""
호두 프로젝트 — 제조업 데모 데이터 시딩 (멱등)

시나리오: 사무용 가구 제조사가 원자재 3종을 3개 공급처에서 조달한다.
P0_확정안_v3 §4 (품목 3종 / 셀러 3곳, 1곳 탈락 / 요청 3건) 을 그대로 구현.

모든 레코드는 default_code / ref 접두사로 식별되므로 Odoo 기본 데모 데이터와 섞이지 않는다.
"""
import os
import xmlrpc.client

# 접속 정보는 .env 에서 읽는다 (프로젝트 공통 규약 — env.example 참고).
# python-dotenv 가 없는 인터프리터(예: Odoo venv)로 돌릴 수도 있어서 soft import.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

URL = os.getenv("ODOO_URL", "http://localhost:8069")
DB = os.getenv("ODOO_DB", "odoo19")
USER = os.getenv("ODOO_USER", "admin")
PW = os.getenv("ODOO_PASSWORD", "admin")

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid = common.authenticate(DB, USER, PW, {})
if not uid:
    raise SystemExit(f"인증 실패 — {URL} / DB={DB} / 계정={USER}. .env 의 ODOO_* 값을 확인하세요.")
api = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")


def call(model, method, *args, **kw):
    return api.execute_kw(DB, uid, PW, model, method, list(args), kw)


def call_void(model, method, *args, **kw):
    """반환값이 None인 액션용. Odoo의 XML-RPC 마샬러는 None을 못 실어서
    메서드가 정상 수행된 뒤 응답 직렬화 단계에서만 Fault가 난다 — 그 한 가지만 삼킨다."""
    try:
        return call(model, method, *args, **kw)
    except xmlrpc.client.Fault as e:
        if "cannot marshal None" in str(e):
            return None
        raise


def create_one(model, vals):
    """create()는 리스트를 넘기면 리스트를 돌려준다 — 항상 단일 id로 펴서 반환."""
    res = call(model, "create", [vals])
    return res[0] if isinstance(res, list) else res


def upsert(model, domain, vals):
    """도메인으로 찾아 있으면 write, 없으면 create. 재실행해도 중복이 안 생긴다."""
    ids = call(model, "search", domain, limit=1)
    if ids:
        call(model, "write", ids, vals)
        return ids[0], "updated"
    return create_one(model, vals), "created"


# ─────────────────────────────────────────────────────────────
# 1. 공급처 3곳 (기획안의 표준 예시 이름)
# ─────────────────────────────────────────────────────────────
VENDORS = [
    {"ref": "HODU-V-HANBIT", "name": "한빛테크",
     "comment": "주력 공급처. 단가·납기 균형형."},
    {"ref": "HODU-V-OFFERET", "name": "오퍼렛",
     "comment": "납기 최단. 단가는 다소 높음."},
    {"ref": "HODU-V-BUYERD", "name": "바이어드",
     "comment": "최저 단가. 최소주문량이 크고 납기가 길어 조건 미달로 탈락하는 경우가 많음."},
]

vendor_id = {}
print("── 공급처 ──")
for v in VENDORS:
    vid, act = upsert("res.partner", [["ref", "=", v["ref"]]], {
        "name": v["name"], "ref": v["ref"], "company_type": "company",
        "supplier_rank": 1, "comment": v["comment"],
    })
    vendor_id[v["ref"]] = vid
    print(f"  {act:8} {v['name']:8} id={vid}")

# ─────────────────────────────────────────────────────────────
# 2. 품목 — 원자재 3종 + 완제품 1종
#    Odoo 19: type='consu' + is_storable=True 가 재고 추적 품목
# ─────────────────────────────────────────────────────────────
PRODUCTS = [
    {"default_code": "RM-STEEL-001", "name": "냉연강판 SS400",
     "description": "1.2T x 1219 x 2438", "list_price": 0.0, "purchase_ok": True, "sale_ok": False},
    {"default_code": "RM-TOP-001", "name": "집성목 상판",
     "description": "1200 x 600 x 25mm", "list_price": 0.0, "purchase_ok": True, "sale_ok": False},
    {"default_code": "RM-BOLT-M8", "name": "육각볼트 M8",
     "description": "M8 x 25, 아연도금", "list_price": 0.0, "purchase_ok": True, "sale_ok": False},
    {"default_code": "FG-DESK-001", "name": "사무용 책상 1200",
     "description": "1200 x 600, 스틸 프레임 + 집성목 상판", "list_price": 189000.0,
     "purchase_ok": False, "sale_ok": True},
]

product_id = {}   # default_code -> product.product id
tmpl_id = {}      # default_code -> product.template id
print("── 품목 ──")
for p in PRODUCTS:
    code = p["default_code"]
    vals = dict(p, type="consu", is_storable=True)
    pid, act = upsert("product.product", [["default_code", "=", code]], vals)
    product_id[code] = pid
    tmpl_id[code] = call("product.product", "read", [pid], fields=["product_tmpl_id"])[0]["product_tmpl_id"][0]
    print(f"  {act:8} {code:14} {p['name']}")

# ─────────────────────────────────────────────────────────────
# 3. 공급처별 단가표 (product.supplierinfo)
#    price=단가, min_qty=최소주문량, delay=납기일수
#
#    설계 의도 (P0 §4 요청 3건에 대응):
#      RM-STEEL-001 : 3곳 모두 취급          → ① 전원 충족, 순수 가격 비교
#      RM-TOP-001   : 3곳 취급하나 조건 상이  → ② 일부 탈락 (최소주문량/납기 필터)
#      RM-BOLT-M8   : 2곳만 취급, 최소주문량 큼 → ③ 소량 요청 시 전원 불가
# ─────────────────────────────────────────────────────────────
SUPPLIERINFO = [
    # (품목,          공급처,            단가,    최소주문량, 납기일)
    ("RM-STEEL-001", "HODU-V-HANBIT",   42000,   100,  5),
    ("RM-STEEL-001", "HODU-V-OFFERET",  44500,    50,  3),
    ("RM-STEEL-001", "HODU-V-BUYERD",   39800,   500, 21),   # 최저가지만 MOQ·납기로 탈락 유도

    ("RM-TOP-001",   "HODU-V-HANBIT",   28000,    50,  7),
    ("RM-TOP-001",   "HODU-V-OFFERET",  26500,   200, 14),
    ("RM-TOP-001",   "HODU-V-BUYERD",   31000,    20,  4),

    ("RM-BOLT-M8",   "HODU-V-HANBIT",     180,  1000, 10),
    ("RM-BOLT-M8",   "HODU-V-OFFERET",    210,   500,  6),
]

print("── 공급처 단가표 ──")
for code, vref, price, min_qty, delay in SUPPLIERINFO:
    _, act = upsert("product.supplierinfo",
                    [["product_tmpl_id", "=", tmpl_id[code]],
                     ["partner_id", "=", vendor_id[vref]]],
                    {"product_tmpl_id": tmpl_id[code], "partner_id": vendor_id[vref],
                     "price": price, "min_qty": min_qty, "delay": delay})
    vname = next(v["name"] for v in VENDORS if v["ref"] == vref)
    print(f"  {act:8} {code:14} {vname:8} {price:>7,}원  MOQ {min_qty:>4}  납기 {delay:>2}일")

# ─────────────────────────────────────────────────────────────
# 4. 현재 재고
#    책상 100개 소요량 대비 '집성목 상판'만 부족하도록 맞춘다 (데모 시나리오)
# ─────────────────────────────────────────────────────────────
STOCK = {"RM-STEEL-001": 120, "RM-TOP-001": 45, "RM-BOLT-M8": 3200}

wh = call("stock.warehouse", "search_read", [], fields=["lot_stock_id"], limit=1)
loc = wh[0]["lot_stock_id"][0]
print(f"── 재고 (창고 위치 id={loc}) ──")
for code, qty in STOCK.items():
    qids = call("stock.quant", "search",
                [["product_id", "=", product_id[code]], ["location_id", "=", loc]], limit=1)
    if qids:
        call("stock.quant", "write", qids, {"inventory_quantity": qty})
    else:
        qids = [create_one("stock.quant", {
            "product_id": product_id[code], "location_id": loc, "inventory_quantity": qty})]
    call_void("stock.quant", "action_apply_inventory", qids)
    print(f"  {code:14} {qty:>6,}")

# ─────────────────────────────────────────────────────────────
# 5. BOM — 책상 1개 = 강판 0.5 + 상판 1 + 볼트 8
#    책상 100개 → 강판 50(재고 120 OK) / 상판 100(재고 45, 55 부족) / 볼트 800(재고 3200 OK)
# ─────────────────────────────────────────────────────────────
BOM_LINES = [("RM-STEEL-001", 0.5), ("RM-TOP-001", 1.0), ("RM-BOLT-M8", 8.0)]

bom_ids = call("mrp.bom", "search", [["code", "=", "HODU-BOM-DESK"]], limit=1)
if bom_ids:
    call("mrp.bom", "write", bom_ids, {"bom_line_ids": [(5, 0, 0)]})   # 기존 라인 비우고 다시 채움
    bom = bom_ids[0]
    act = "updated"
else:
    bom = create_one("mrp.bom", {
        "code": "HODU-BOM-DESK", "product_tmpl_id": tmpl_id["FG-DESK-001"],
        "product_qty": 1.0, "type": "normal"})
    act = "created"
# 제조 리드타임 — 자재가 다 있어도 만드는 데 걸리는 날. 납기 판정의 구성요소다.
call("mrp.bom", "write", [bom], {"produce_delay": 3})
call("mrp.bom", "write", [bom], {"bom_line_ids": [
    (0, 0, {"product_id": product_id[c], "product_qty": q}) for c, q in BOM_LINES]})
print(f"── BOM ──\n  {act:8} HODU-BOM-DESK  사무용 책상 1200 x1  = " +
      " + ".join(f"{c} x{q}" for c, q in BOM_LINES))

print("\n완료.")
