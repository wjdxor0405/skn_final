"""
Odoo 연동 — JSON-RPC 클라이언트 (읽기 전용)

**왜 XML-RPC가 아니라 JSON-RPC인가**
Odoo의 XML-RPC 마샬러는 `None`을 실어 보내지 못한다. 그런데 Odoo의 액션 메서드
(`action_apply_inventory`, `button_confirm` 등)는 대부분 `None`을 반환한다.
그래서 **메서드는 정상 수행된 뒤 응답 직렬화 단계에서만** Fault가 난다 —
성공을 실패로 오인하기 딱 좋은 형태다. JSON-RPC는 `null`을 그대로 실어 보낸다.

**의존성 없음** — 표준 라이브러리(urllib)만 쓴다. 이 파일은 전송과 조회만 맡고,
도메인 객체(`SellerRegister` 등)로의 변환은 `store.py`가 한다.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

_TAG = re.compile(r"<[^>]+>")


def _plain(value: Any) -> str:
    """Odoo의 HTML 필드(description 등)를 평문으로. 사양 문자열은 부분일치 비교에
    쓰이므로 태그가 섞여 있으면 매칭이 조용히 실패한다."""
    if not value:
        return ""
    return html.unescape(_TAG.sub("", str(value))).strip()


class OdooError(RuntimeError):
    """Odoo가 돌려준 오류. 서버 쪽 traceback을 그대로 담는다."""


class OdooClient:
    def __init__(
        self,
        url: str | None = None,
        db: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.url = (url or os.getenv("ODOO_URL", "http://localhost:8069")).rstrip("/")
        self.db = db or os.getenv("ODOO_DB", "odoo19")
        self.user = user or os.getenv("ODOO_USER", "admin")
        self.password = password or os.getenv("ODOO_PASSWORD", "admin")
        self.timeout = timeout
        self._uid: int | None = None

    # ── 전송 ──────────────────────────────────────────────────────────
    def _rpc(self, service: str, method: str, args: list[Any]) -> Any:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/jsonrpc", data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise OdooError(f"Odoo에 연결하지 못했습니다 ({self.url}): {e}") from e

        if "error" in body:
            err = body["error"]
            detail = (err.get("data") or {}).get("message") or err.get("message")
            raise OdooError(f"Odoo 오류: {detail}")
        return body.get("result")

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = self._rpc("common", "authenticate", [self.db, self.user, self.password, {}])
            if not uid:
                raise OdooError(
                    f"인증 실패 — {self.url} / DB={self.db} / 계정={self.user}. "
                    ".env 의 ODOO_* 값을 확인하세요."
                )
            self._uid = uid
        return self._uid

    def execute(self, model: str, method: str, *args: Any, **kw: Any) -> Any:
        return self._rpc(
            "object", "execute_kw",
            [self.db, self.uid, self.password, model, method, list(args), kw],
        )

    def ping(self) -> bool:
        """연결·인증만 확인. 기동 시 헬스체크용."""
        return bool(self.uid)

    # ── 조회 ──────────────────────────────────────────────────────────
    def purchasable_products(self, code_prefix: str = "") -> list[dict]:
        """
        구매 대상 품목 목록. 반환 키: code / name / spec / on_hand
        `on_hand` 는 **우리 회사** 재고이지 공급처 재고가 아니다 (Odoo에는 후자가 없다).
        """
        domain: list[Any] = [["purchase_ok", "=", True], ["default_code", "!=", False]]
        if code_prefix:
            domain.append(["default_code", "=like", f"{code_prefix}%"])
        rows = self.execute(
            "product.product", "search_read", domain,
            fields=["default_code", "name", "description", "qty_available"],
            order="default_code",
        )
        return [
            {
                "code": r["default_code"],
                "name": r["name"],
                "spec": _plain(r.get("description")),
                "on_hand": r.get("qty_available") or 0.0,
            }
            for r in rows
        ]

    def vendor_offers(self, product_code: str) -> list[dict]:
        """
        한 품목의 공급처별 조건. 반환 키: vendor / price / min_qty / lead_time_days / spec

        `min_qty` 는 최소주문량(MOQ)이다. skn_final 기존 스크리닝의 `qty`(셀러 보유재고)와
        방향이 반대라는 점에 주의 — 재고는 "이상이어야" 통과지만 MOQ는 주문량이
        "그 이상이어야" 통과한다.
        """
        rows = self.execute(
            "product.supplierinfo", "search_read",
            [["product_tmpl_id.default_code", "=", product_code]],
            fields=["partner_id", "price", "min_qty", "delay", "product_tmpl_id"],
            order="price",
        )
        if not rows:
            return []
        spec = ""
        prod = self.execute(
            "product.product", "search_read", [["default_code", "=", product_code]],
            fields=["description"], limit=1,
        )
        if prod:
            spec = _plain(prod[0].get("description"))
        return [
            {
                "vendor": r["partner_id"][1],
                "price": int(round(r["price"])),
                "min_qty": int(round(r["min_qty"])),
                "lead_time_days": int(r["delay"]),
                "spec": spec,
            }
            for r in rows
        ]


    def product(self, code: str) -> dict | None:
        """품목 1건. 반환 키: id / code / name / spec / on_hand"""
        rows = self.execute(
            "product.product", "search_read", [["default_code", "=", code]],
            fields=["default_code", "name", "description", "qty_available"], limit=1,
        )
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "code": r["default_code"], "name": r["name"],
                "spec": _plain(r.get("description")), "on_hand": r.get("qty_available") or 0.0}

    def manufacturable_products(self) -> list[dict]:
        """자재명세서(BOM)가 있는 품목 = 우리가 만들어 파는 완제품."""
        boms = self.execute("mrp.bom", "search_read", [], fields=["product_tmpl_id"])
        tmpl_ids = list({b["product_tmpl_id"][0] for b in boms})
        if not tmpl_ids:
            return []
        rows = self.execute(
            "product.product", "search_read",
            [["product_tmpl_id", "in", tmpl_ids], ["default_code", "!=", False]],
            fields=["default_code", "name", "qty_available"], order="default_code",
        )
        return [{"code": r["default_code"], "name": r["name"],
                 "on_hand": r.get("qty_available") or 0.0} for r in rows]

    def bom_for(self, product_code: str) -> dict | None:
        """
        완제품의 자재명세서. 반환 키: produce_delay(제조 리드타임 일) / base_qty / lines

        1레벨만 편다. 데모의 BOM은 1레벨이라 충분하지만, 중간 조립품이 생기면
        Odoo의 mrp.bom.explode 를 쓰거나 여기서 재귀를 돌아야 한다.
        """
        boms = self.execute(
            "mrp.bom", "search_read", [["product_tmpl_id.default_code", "=", product_code]],
            fields=["id", "product_qty", "produce_delay"], limit=1,
        )
        if not boms:
            return None
        bom = boms[0]
        lines = self.execute(
            "mrp.bom.line", "search_read", [["bom_id", "=", bom["id"]]],
            fields=["product_id", "product_qty"],
        )
        out_lines = []
        for l in lines:
            prod = self.execute("product.product", "read", [l["product_id"][0]],
                                fields=["default_code", "name", "qty_available"])[0]
            out_lines.append({
                "code": prod["default_code"], "name": prod["name"],
                "qty_per": l["product_qty"] / (bom["product_qty"] or 1.0),
                "on_hand": prod.get("qty_available") or 0.0,
            })
        return {"produce_delay": bom["produce_delay"], "base_qty": bom["product_qty"],
                "lines": out_lines}

    # ── 쓰기 ──────────────────────────────────────────────────────────
    @staticmethod
    def _one(result: Any) -> Any:
        """create()는 리스트를 넘기면 리스트를 돌려준다 — 단일 id로 펴서 반환."""
        return result[0] if isinstance(result, list) else result

    def find_id(self, model: str, domain: list) -> int | None:
        ids = self.execute(model, "search", domain, limit=1)
        return ids[0] if ids else None

    def purchase_order_by_origin(self, origin: str) -> dict | None:
        """origin(거래ID)으로 이미 만든 발주서를 찾는다 — 같은 거래에 두 번 만들지 않기 위해."""
        rows = self.execute(
            "purchase.order", "search_read", [["origin", "=", origin]],
            fields=["name", "state", "amount_total"], limit=1,
        )
        return rows[0] if rows else None

    def create_draft_purchase_order(
        self, *, vendor: str, product_code: str, qty: float, price: float, origin: str,
    ) -> dict:
        """
        낙찰 결과를 발주서 **초안**으로 만든다. 확정(button_confirm)은 사람이 한다.

        `origin`에 거래ID를 넣어 두면 Odoo 발주서 화면의 '소스 문서'에 그대로 보이고,
        재실행 시 중복 생성을 막는 열쇠로도 쓴다.
        `name`(설명)과 `date_planned`는 Odoo가 품목에서 계산하므로 넘기지 않는다.
        """
        existing = self.purchase_order_by_origin(origin)
        if existing:
            return existing

        vendor_id = self.find_id(
            "res.partner", [["name", "=", vendor], ["supplier_rank", ">", 0]])
        if vendor_id is None:
            raise OdooError(f"공급처를 Odoo에서 찾지 못했습니다: {vendor!r}")
        product_id = self.find_id("product.product", [["default_code", "=", product_code]])
        if product_id is None:
            raise OdooError(f"품목을 Odoo에서 찾지 못했습니다: {product_code!r}")

        po_id = self._one(self.execute("purchase.order", "create", [{
            "partner_id": vendor_id,
            "origin": origin,
            "order_line": [(0, 0, {
                "product_id": product_id,
                "product_qty": qty,
                "price_unit": price,
            })],
        }]))
        rows = self.execute("purchase.order", "read", [po_id],
                            fields=["name", "state", "amount_total"])
        return rows[0]

    def post_note(self, model: str, res_id: int, body: str) -> None:
        """레코드의 chatter에 내부 메모를 남긴다 (mail.mt_note — 고객에게 발송되지 않음)."""
        self.execute(model, "message_post", [res_id],
                     body=body, message_type="comment", subtype_xmlid="mail.mt_note")

    def confirm_purchase_order(self, po_id: int) -> None:
        self.execute("purchase.order", "button_confirm", [po_id])

    def cancel_purchase_order(self, po_id: int) -> None:
        self.execute("purchase.order", "button_cancel", [po_id])


# 프로세스 전역 단일 인스턴스 (store.py 와 같은 패턴). 인증은 첫 호출 때 지연 수행된다.
odoo = OdooClient()
