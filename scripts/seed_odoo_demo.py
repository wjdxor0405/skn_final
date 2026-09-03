"""
호두 프로젝트 — 제조업 데모 데이터 시딩 (멱등)

시나리오: GPU 서버·워크스테이션 조립 SI 업체가 부품 3종을 3개 공급처에서 조달한다.
수치는 기획 회의의 예시를 그대로 옮겼다 — "GPU 재고 10장인 상태에서 발주 100개를
받아 90개가 부족해지면 시스템이 자동 감지해 추가 발주서를 생성".
P0_확정안_v3 §4 (품목 3종 / 셀러 3곳, 1곳 탈락 / 요청 3건) 을 그대로 구현.

모든 레코드는 default_code / ref 접두사로 식별되므로 Odoo 기본 데모 데이터와 섞이지 않는다.
"""
import json
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
    {"default_code": "RM-GPU-H100", "name": "GPU 가속기 H100 80GB",
     "description": "PCIe 5.0, 80GB HBM3, 350W", "list_price": 0.0,
     "purchase_ok": True, "sale_ok": False},
    {"default_code": "RM-PSU-3000W", "name": "서버 전원공급장치 3000W",
     "description": "3000W, 80+ Titanium, 이중화(1+1)", "list_price": 0.0,
     "purchase_ok": True, "sale_ok": False},
    {"default_code": "RM-RACK-4U", "name": "4U 랙 섀시",
     "description": "4U, 19인치 랙마운트, GPU 슬롯 8", "list_price": 0.0,
     "purchase_ok": True, "sale_ok": False},
    {"default_code": "FG-GPUSRV-001", "name": "GPU 서버랙 4U",
     "description": "4U 랙마운트 · H100 탑재 · 이중화 전원", "list_price": 52000000.0,
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
#      RM-GPU-H100  : 3곳 모두 취급하나 납기가 3배 차이 → ① 납기가 낙찰을 가르는 사례
#      RM-PSU-3000W : 3곳 취급, 조건 상이            → ② 일부 탈락 (최소주문량/납기 필터)
#      RM-RACK-4U   : 2곳만 취급                    → ③ 소량 요청 시 후보가 줄어드는 사례
# ─────────────────────────────────────────────────────────────
SUPPLIERINFO = [
    # (품목,          공급처,             단가,      최소주문량, 납기일)
    #
    # GPU는 납기가 결과를 가른다:
    #   납기 10일 이내 요구 → 바이어드(7일)만 통과. 최고가로 사게 된다
    #   납기 30일 여유      → 셋 다 통과. 오퍼렛(최저가)이 낙찰
    ("RM-GPU-H100",  "HODU-V-HANBIT",  38000000,   10, 14),
    ("RM-GPU-H100",  "HODU-V-OFFERET", 36500000,   50, 30),   # 최저가지만 납기가 길다
    ("RM-GPU-H100",  "HODU-V-BUYERD",  41000000,    5,  7),   # 최고가지만 가장 빠르다

    ("RM-PSU-3000W", "HODU-V-HANBIT",    780000,   20,  5),
    ("RM-PSU-3000W", "HODU-V-OFFERET",   745000,  100, 10),
    ("RM-PSU-3000W", "HODU-V-BUYERD",    820000,   10,  3),

    ("RM-RACK-4U",   "HODU-V-HANBIT",   1250000,   10,  7),
    ("RM-RACK-4U",   "HODU-V-OFFERET",  1180000,   50, 12),
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
#    서버 100대 소요량 대비 'GPU'만 부족하도록 맞춘다 — 기획 회의의 예시 수치 그대로
# ─────────────────────────────────────────────────────────────
STOCK = {"RM-GPU-H100": 10, "RM-PSU-3000W": 400, "RM-RACK-4U": 150}

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
# 5. BOM — 서버 1대 = GPU 1 + 파워 2(이중화) + 섀시 1
#    서버 100대 → GPU 100(재고 10, 90 부족) / 파워 200(재고 400 OK) / 섀시 100(재고 150 OK)
# ─────────────────────────────────────────────────────────────
BOM_LINES = [("RM-GPU-H100", 1.0), ("RM-PSU-3000W", 2.0), ("RM-RACK-4U", 1.0)]

bom_ids = call("mrp.bom", "search", [["code", "=", "HODU-BOM-GPUSRV"]], limit=1)
if bom_ids:
    call("mrp.bom", "write", bom_ids, {"bom_line_ids": [(5, 0, 0)]})   # 기존 라인 비우고 다시 채움
    bom = bom_ids[0]
    act = "updated"
else:
    bom = create_one("mrp.bom", {
        "code": "HODU-BOM-GPUSRV", "product_tmpl_id": tmpl_id["FG-GPUSRV-001"],
        "product_qty": 1.0, "type": "normal"})
    act = "created"
# 제조 리드타임 — 자재가 다 있어도 조립·검수에 걸리는 날. 납기 판정의 구성요소다.
call("mrp.bom", "write", [bom], {"produce_delay": 5})
call("mrp.bom", "write", [bom], {"bom_line_ids": [
    (0, 0, {"product_id": product_id[c], "product_qty": q}) for c, q in BOM_LINES]})
print(f"── BOM ──\n  {act:8} HODU-BOM-GPUSRV  GPU 서버랙 4U x1  = " +
      " + ".join(f"{c} x{q}" for c, q in BOM_LINES))

# ─────────────────────────────────────────────────────────────
# 6. 고객사와 표준계약
#
#    표준계약은 "이 고객은 분기당 몇 대를 주문하는 게 정상인가"다. 이 범위 안이면
#    협상 없이 기존 조건으로 자동 처리하고, 벗어나는 이례적 발주에만 에이전트가 붙는다.
#
#    Odoo의 ir.config_parameter(자체 설정 키-값 저장소)에 둔다. 애드온을 만들지 않고도
#    마스터 데이터를 Odoo에 두는 원칙을 지킬 수 있고, Odoo 모듈들도 설정을 여기 넣는다.
#    키 형식: hodu.contract.<고객ref>.<품목코드>
# ─────────────────────────────────────────────────────────────
CUSTOMERS = [
    {"ref": "HODU-C-DAEHAN", "name": "대한AI연구원",
     "comment": "장기 거래처. 분기당 60대를 정기 발주."},
    {"ref": "HODU-C-NURI", "name": "누리클라우드",
     "comment": "장기 거래처. 분기당 40대."},
    {"ref": "HODU-C-SEBIT", "name": "세빛테크놀로지",
     "comment": "신규 고객. 표준계약이 없어 모든 발주가 예외로 분류된다."},
]

# (고객ref, 품목코드, 분기 표준수량, 허용배수)
CONTRACTS = [
    ("HODU-C-DAEHAN", "FG-GPUSRV-001", 60, 1.2),
    ("HODU-C-NURI",   "FG-GPUSRV-001", 40, 1.2),
    # 세빛테크놀로지는 일부러 비워둔다 — 신규 고객은 전부 예외 경로를 타야 한다
]

print("── 고객사 ──")
for c in CUSTOMERS:
    cid, act = upsert("res.partner", [["ref", "=", c["ref"]]], {
        "name": c["name"], "ref": c["ref"], "company_type": "company",
        "customer_rank": 1, "comment": c["comment"],
    })
    print(f"  {act:8} {c['name']:14} id={cid}")

print("── 표준계약 ──")
for cref, code, period_qty, tol in CONTRACTS:
    key = f"hodu.contract.{cref}.{code}"
    val = json.dumps({"period_qty": period_qty, "tolerance": tol}, ensure_ascii=False)
    _, act = upsert("ir.config_parameter", [["key", "=", key]], {"key": key, "value": val})
    name = next(c["name"] for c in CUSTOMERS if c["ref"] == cref)
    print(f"  {act:8} {name:14} {code}  분기 {period_qty}대 × 허용 {tol} "
          f"= {period_qty * tol:.0f}대까지 표준")

# ─────────────────────────────────────────────────────────────
# 6. 지난 도메인의 잔재 정리
#    이 스크립트가 만든 RM-/FG- 품목 중 지금 목록에 없는 것은 보관(archive)한다.
#    삭제하지 않는 이유: 과거 발주서가 참조하고 있어서 지우면 이력이 깨진다.
#    Odoo는 보관된 품목을 검색에서 기본 제외하므로 화면·드롭다운에서는 사라진다.
# ─────────────────────────────────────────────────────────────
current = {p["default_code"] for p in PRODUCTS}
stale = call("product.product", "search_read",
             ["|", ["default_code", "=like", "RM-%"], ["default_code", "=like", "FG-%"]],
             fields=["default_code"])
to_archive = [r["id"] for r in stale if r["default_code"] not in current]
print("── 지난 도메인 정리 ──")
if to_archive:
    call("product.product", "write", to_archive, {"active": False})
    print(f"  보관 {len(to_archive)}건: " +
          ", ".join(r["default_code"] for r in stale if r["default_code"] not in current))
else:
    print("  정리할 것 없음")

print("\n완료.")
