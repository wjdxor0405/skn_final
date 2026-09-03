"""
목업 화면 ↔ API 매핑
  화면 1 (셀러 등록)        -> POST /api/sellers/register
  화면 2 (구매 요청 등록)    -> POST /api/buyers/request   (등록 즉시 협상까지 동기 실행)
  화면 3 (협상 결과·승인)    -> GET  /api/deals/{txid}
                              POST /api/deals/{txid}/approve
                              POST /api/deals/{txid}/reject
"""

from __future__ import annotations
from dotenv import load_dotenv

load_dotenv()  # .env 파일을 읽어서 OPENAI_API_KEY, NEGOTIATOR_MODE 등을 환경변수로 등록
# (OS/셸 종류(Windows PowerShell, macOS/Linux bash 등)와 무관하게 항상 같은 방식으로 동작)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from pydantic import BaseModel

from .schemas import SellerRegister, BuyerRequest
from . import feasibility, intake
from .odoo_client import odoo
from .store import store, CatalogReadOnly, CATALOG_SOURCE
from .negotiate import run_negotiation
from .report import write_report, summarizer
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Deal Ledger MMVP")

# 데모 목적: 로컬에서 파일로 연 목업 HTML(origin: null)도 호출 가능하도록 전체 허용
#
# !! EC2 배포 전 반드시 좁힐 것 !!
# 이 서버에는 인증이 없고, 아래 POST 중 셋(feasibility/procure, deals/approve,
# deals/reject)은 Odoo 발주서를 생성·확정·취소한다. 즉 지금 상태로 외부에 노출하면
# 사용자가 방문한 아무 웹페이지의 JS가 발주를 확정시킬 수 있다
# (단순 POST는 CORS와 무관하게 전송되고, allow_origins="*" 는 응답까지 읽게 해 준다).
# 최소한 ① allow_origins 를 실제 오리진으로 좁히고 ② 승인 계열에 토큰을 둘 것.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 목업 화면(static/index.html)을 백엔드가 직접 서빙 — API와 같은 도메인/포트에서
# 나가기 때문에 상대경로("/api")가 로컬이든 AWS든 어디서나 그대로 동작한다.
app.mount("/static-assets", StaticFiles(directory="static"), name="static-assets")


@app.get("/")
def serve_mockup():
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")


@app.post("/api/sellers/register")
def register_seller(offer: SellerRegister):
    try:
        store.register_seller(offer)
    except CatalogReadOnly as e:
        # 500이 아니라 "왜 안 되는지"가 화면에 보여야 한다
        raise HTTPException(409, str(e))
    return {"ok": True, "registered": offer}


@app.get("/api/sellers")
def list_sellers():
    return store.list_sellers()


@app.get("/api/items")
def list_items():
    """화면의 품목 드롭다운용. odoo 모드면 Odoo 품목 마스터, sqlite 모드면 등록된 품목."""
    return store.list_items()


@app.get("/api/products")
def list_finished_products():
    """완제품(BOM 보유) 목록 — 납품 가능성 검증 화면의 드롭다운용."""
    if CATALOG_SOURCE != "odoo":
        return []
    return odoo.manufacturable_products()


class FeasibilityRequest(BaseModel):
    """고객 주문을 받았을 때의 납품 가능성 질의."""
    item: str                 # 완제품 코드 (예: FG-DESK-001)
    qty: int
    due_days: int


@app.post("/api/feasibility")
def check_feasibility(req: FeasibilityRequest):
    """
    "이 수량을 이 납기 안에 댈 수 있는가" — 전 구간 룰 기반. 숫자는 Odoo에서만 온다.
    LLM이 "재고 10대인데 100대 가능"이라고 답할 자리를 없애는 것이 목적.
    """
    try:
        result = feasibility.check(req.item, req.qty, req.due_days)
    except ValueError as e:
        raise HTTPException(404, str(e))
    result["procurement_requests"] = feasibility.procurement_requests(result)
    return result


@app.post("/api/feasibility/procure")
def procure_shortages(req: FeasibilityRequest):
    """부족 자재마다 협상을 돌려 Odoo 발주서 초안까지 만든다 (검증 → 조달 연결)."""
    try:
        result = feasibility.check(req.item, req.qty, req.due_days)
    except ValueError as e:
        raise HTTPException(404, str(e))
    deals = []
    for r in feasibility.procurement_requests(result):
        summary = run_negotiation(store, BuyerRequest(**r))
        deal = store.get_deal(summary["txid"]) or {}
        deals.append({**summary,
                      **{k: deal.get(k) for k in ("po_name", "po_url", "po_error")}})
    return {"feasible": result["feasible"], "reasons": result["reasons"], "deals": deals}


@app.get("/api/customers")
def list_customers():
    """고객사 목록 — 주문 접수 화면의 드롭다운용."""
    if CATALOG_SOURCE != "odoo":
        return []
    return odoo.customers()


class OrderIntake(BaseModel):
    """고객사로부터 들어온 발주."""
    customer_ref: str         # 고객사 ref (예: HODU-C-DAEHAN)
    item: str                 # 완제품 코드
    qty: int
    due_days: int


@app.post("/api/orders/intake")
def order_intake(req: OrderIntake):
    """
    발주 접수 — 표준계약 범위면 협상 없이 자동 처리하고, 벗어나면 에이전트가 협상한다.
    에이전트가 '언제 나서지 않는지'를 보여주는 엔드포인트다.
    """
    try:
        return intake.run_intake(store, req.customer_ref, req.item, req.qty, req.due_days)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/buyers/request")
def buyer_request(request: BuyerRequest):
    """규칙 기반이라 즉시(동기) 협상까지 끝내고 결과를 바로 반환한다."""
    summary = run_negotiation(store, request)
    return summary


@app.get("/api/deals/{txid}")
def get_deal(txid: str):
    deal = store.get_deal(txid)
    if not deal:
        raise HTTPException(404, "존재하지 않는 거래ID입니다")
    log = store.get_log(txid)
    return {
        "deal": deal,
        "summary": summarizer.summarize(log),
        "log": [e.model_dump(by_alias=True) for e in log],
    }


@app.post("/api/deals/{txid}/approve")
def approve_deal(txid: str):
    # 주의: 인증 없음. odoo 모드에서는 이 호출이 Odoo 발주서를 확정한다 (상단 CORS 주석 참고).
    deal = store.get_deal(txid)
    if not deal:
        raise HTTPException(404, "존재하지 않는 거래ID입니다")
    store.set_approval(txid, "APPROVED")
    log = store.get_log(txid)
    path = write_report(txid, log)
    return {"ok": True, "status": "APPROVED", "report_path": str(path)}


@app.post("/api/deals/{txid}/reject")
def reject_deal(txid: str):
    # 주의: 인증 없음. odoo 모드에서는 이 호출이 Odoo 발주서를 취소한다 (상단 CORS 주석 참고).
    deal = store.get_deal(txid)
    if not deal:
        raise HTTPException(404, "존재하지 않는 거래ID입니다")
    store.set_approval(txid, "REJECTED")
    log = store.get_log(txid)
    path = write_report(txid, log)
    return {"ok": True, "status": "REJECTED", "report_path": str(path)}