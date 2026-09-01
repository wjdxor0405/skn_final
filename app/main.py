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

from .schemas import SellerRegister, BuyerRequest
from .store import store
from .negotiate import run_negotiation
from .report import write_report, summarizer

app = FastAPI(title="Deal Ledger MMVP")

# 데모 목적: 로컬에서 파일로 연 목업 HTML(origin: null)도 호출 가능하도록 전체 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/sellers/register")
def register_seller(offer: SellerRegister):
    store.register_seller(offer)
    return {"ok": True, "registered": offer}


@app.get("/api/sellers")
def list_sellers():
    return store.list_sellers()


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
    deal = store.get_deal(txid)
    if not deal:
        raise HTTPException(404, "존재하지 않는 거래ID입니다")
    store.set_approval(txid, "APPROVED")
    log = store.get_log(txid)
    path = write_report(txid, log)
    return {"ok": True, "status": "APPROVED", "report_path": str(path)}


@app.post("/api/deals/{txid}/reject")
def reject_deal(txid: str):
    deal = store.get_deal(txid)
    if not deal:
        raise HTTPException(404, "존재하지 않는 거래ID입니다")
    store.set_approval(txid, "REJECTED")
    log = store.get_log(txid)
    path = write_report(txid, log)
    return {"ok": True, "status": "REJECTED", "report_path": str(path)}