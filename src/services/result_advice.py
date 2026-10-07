"""결과 화면 채팅의 "묻는 말"에 코드가 계산해 주는 사실들 (PC 만).

"돈 남았는데 뭐 올릴까?", "CPU 더 좋은 걸로 바꿔도 문제없어?", "뭘 바꾸면 제일 싸져?", "배그 돌아가?" 같은
질문은 교체 요청이 아니라 묻는 말이다. 예전 결과 에이전트는 이런 말에 도구가 없어서 되묻기만 하거나,
일반 지식으로 "충분합니다"를 지어내거나, 묻는 말인데 수량을 바꿔 버렸다(2026-10-02 실측).

여기 함수들은 **아무것도 저장하지 않는다**. 지금 구성에서 한 부품만 바꾼다고 가정하고, 추천 엔진이 쓰는 것과
같은 판정을 그대로 돌린다:
- 요구 사양(성능 등급·용량·소켓·파워 용량 …) — `stage3a_hardfilter._judge_computer` 를 계획에 저장된 요구 사양으로
- 부품 사이 호환(소켓·메모리·크기·전력 …) — `stage4_optimize._pc_known_failures` / `pc_compat_details`
- 예산 — 계획의 `budget_max` 와 선택된 부품 합계

"더 좋다"는 코드가 아는 값만으로 정한다: CPU·GPU 는 성능 등급(perf_tier), RAM 은 용량. 그 밖의 슬롯(케이스·쿨러·
파워·저장장치)은 무엇이 더 좋은지 정할 값이 없어 올릴 후보로 내지 않는다(저장장치 용량은 라인업 최대치라 실제
판매 용량과 다를 수 있다). 결과 문장은 사실만 담고, 고를지는 사용자가 정한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from src.engine.lang import fmt_money

# "더 좋다"를 코드가 정할 수 있는 슬롯과 그 값. 순서는 용도 기준 등급과의 차이로 다시 정한다(_upgrade_order).
_UPGRADE_SLOTS = ("GPU", "CPU", "RAM")


@dataclass
class _Ctx:
    conn: object
    revision_id: UUID
    stored: list[dict]                 # engine.get_candidates 행 (빼둔 것 포함)
    cvals: dict                        # 계획 조건 값
    targets: dict[str, dict]           # 슬롯 → 저장된 요구 사양(match_spec)
    pool: dict                         # 슬롯 → [Candidate] (가격 있는 카탈로그 전체)
    by_variant: dict                   # variant_id → Candidate
    spec: object                       # RequirementSpec (업그레이드의 보유 부품 포함)
    rules: dict                        # 규칙 문서 verification 절

    @property
    def picked(self) -> list[dict]:
        return [r for r in self.stored if r.get("selected") is not False]

    def row(self, slot: str) -> dict | None:
        return next((r for r in self.picked if r["slot"] == slot), None)

    def chosen(self) -> dict:
        out = {}
        for r in self.picked:
            cand = self.by_variant.get(str(r["variant_id"]))
            if cand is not None:
                out[r["slot"]] = cand
        return out

    @property
    def budget_max(self) -> int | None:
        return int(self.cvals["budget_max"]) if self.cvals.get("budget_max") else None

    def total(self) -> int:
        return sum((int(r["price"]) if r["price"] is not None else 0) * int(r["qty"] or 1) for r in self.picked)


def _context(conn, revision_id: UUID) -> _Ctx:
    from src.categories import load_category
    from src.dto import RequirementSpec
    from src.engine.owned_parts import owned_for_conditions
    from src.engine.stage2_requirement import load_computer_rules
    from src.repo.catalog_repo import load_candidates_by_slot_from_db
    from src.repo.plan_repo import PlanRepo
    from src.services.recommendation_service import _require_done_run

    erepo, run = _require_done_run(conn, revision_id)
    stored = erepo.get_candidates(run["id"])
    full = PlanRepo(conn).load_full(revision_id)
    cvals = {r["condition_key"]: r["value"].get("value") for r in full["conditions"]}
    node_slot = {n["id"]: n["template_key"] for n in full.get("nodes", [])}
    targets = {node_slot[r["node_id"]]: (r.get("match_spec") or {})
               for r in full.get("requirements", []) if r["node_id"] in node_slot}
    from src.services.live_spec_lookup import cached_for_kept_parts

    pool = load_candidates_by_slot_from_db(conn)
    by_variant = {c.variant_id: c for cands in pool.values() for c in cands}
    owned = owned_for_conditions(cvals, pool, {r["slot"] for r in stored},
                                 load_category("computer").get("slot_structure", []))
    cached_for_kept_parts(conn, owned, cvals.get("current_specs"))
    spec = RequirementSpec(
        list_id=str(revision_id), category="computer",
        mode="upgrade" if cvals.get("mode") == "upgrade" else "build",
        owned=owned,
    )
    return _Ctx(conn=conn, revision_id=revision_id, stored=stored, cvals=cvals, targets=targets, pool=pool,
                by_variant=by_variant, spec=spec, rules=load_computer_rules()["verification"])


def is_pc(conn, revision_id: UUID) -> bool:
    from src.repo.plan_repo import PlanRepo
    for r in PlanRepo(conn).load_full(revision_id)["conditions"]:
        if r["condition_key"] == "category":
            return r["value"].get("value") == "computer"
    return False


# ── 판정 조각 ───────────────────────────────────────────────────────────────
def _metric(slot: str, specs: dict) -> float | None:
    if slot in ("CPU", "GPU"):
        tier = specs.get("perf_tier")
        return float(tier) if tier is not None else None
    if slot == "RAM":
        cap = specs.get("capacity_gb")
        return float(cap) if cap is not None else None
    return None


def _metric_text(slot: str, value: float | None) -> str:
    if value is None:
        return "등급 정보 없음"
    return f"{value:g}GB" if slot == "RAM" else f"성능 등급 {value:g}"


def _new_failures(ctx: _Ctx, slot: str, cand) -> set[str]:
    """이 슬롯만 cand 로 바꿨을 때 *새로* 생기는 확정 비호환(지금 구성에 이미 있던 문제는 후보 탓이 아니다)."""
    from src.engine.stage4_optimize import _pc_known_failures
    chosen = ctx.chosen()
    baseline = _pc_known_failures(chosen, ctx.spec, ctx.rules)
    return _pc_known_failures({**chosen, slot: cand}, ctx.spec, ctx.rules) - baseline


def _requirement_verdict(ctx: _Ctx, slot: str, cand) -> tuple[str, list[str]]:
    """계획에 저장된 이 슬롯의 요구 사양(성능 등급 하한·용량·파워 용량 …)으로 본 판정 — Pass/Pending/Fail."""
    from src.engine.stage3a_hardfilter import _judge_computer
    judged = _judge_computer(cand, ctx.targets.get(slot, {}))
    return judged.verdict, judged.reasons


_REQ_REASON_TEXT = {
    "FAIL_PERF_BELOW": "요구 성능 등급 미달",
    "FAIL_CAPACITY_BELOW": "요구 용량 미달",
    "FAIL_WATTAGE_BELOW": "요구 파워 용량 미달",
    "FAIL_RATING_BELOW": "요구 효율 등급 미달",
    "FAIL_VRAM_BELOW": "요구 VRAM 미달",
    "FAIL_SOCKET_NOT_IN": "요구 소켓 아님",
    "FAIL_MEM_TYPE": "메모리 규격 다름",
    "FAIL_FORM_NOT_IN": "요구 규격 아님",
    "FAIL_PROTOCOL": "요구 인터페이스 아님",
}


def _reason_text(reasons: list[str]) -> str:
    out = []
    for r in reasons:
        key = r.split(":", 1)[0]
        out.append(_REQ_REASON_TEXT.get(key, key))
    return ", ".join(dict.fromkeys(out))


def requirement_shortfalls(conn, revision_id: UUID, slot: str, variant_ids) -> dict[str, str]:
    """후보마다 이 견적의 요구 사양(성능 등급·용량·파워 용량 …)을 못 채우면 그 이유 문구 — 채우거나 판정할 수 없으면 빠진다.
    교체 후보 목록의 ⚠ 와 교체 거절에 쓴다. 성능 등급만 보던 목록이 RAM 용량 미달(32GB → 8GB)을 못 잡았다(10/3 리허설 실패 7)."""
    if not is_pc(conn, revision_id):
        return {}
    ctx = _context(conn, revision_id)
    out = {}
    for vid in variant_ids:
        cand = ctx.by_variant.get(str(vid))
        if cand is None or cand.slot != slot:
            continue
        verdict, reasons = _requirement_verdict(ctx, slot, cand)
        if verdict == "Fail":
            out[str(vid)] = _reason_text(reasons) or "요구 사양 미달"
    return out


def _qty(row: dict) -> int:
    return int(row.get("qty") or 1)


def _price(row_or_cand) -> int:
    p = row_or_cand["price"] if isinstance(row_or_cand, dict) else row_or_cand.price
    return int(p) if p is not None else 0


def _ideal_tiers(ctx: _Ctx) -> dict[str, float]:
    """용도·해상도 기준 등급(추천 엔진 ranking.ideal_tiers). 게임 요구 하한이 더 높으면 그 하한."""
    from src.engine.stage2_requirement import load_computer_rules
    rules = load_computer_rules()
    purpose = ctx.cvals.get("purpose") or "game"
    res = ctx.cvals.get("resolution") or rules["requirements"]["default_resolution"]
    by_purpose = rules["ranking"]["ideal_tiers"].get(purpose) or {}
    ideals = dict(by_purpose.get(res) or by_purpose.get("default") or {})
    for slot in ("CPU", "GPU"):
        tmin = ctx.targets.get(slot, {}).get("perf_tier_min")
        if tmin is not None and slot in ideals:
            ideals[slot] = max(float(ideals[slot]), float(tmin))
    return {k: float(v) for k, v in ideals.items()}


# ── 기본 쿨러 ───────────────────────────────────────────────────────────────
def box_cooler(conn, cpu_variant_id) -> str | None:
    """CPU 정품 박스에 기본 쿨러가 드는지 — 카탈로그 원문 값("O (정품 박스 기준)", "X / SKU 확인"). 없으면 None.
    추천 엔진은 이 값을 읽지 않는다(엔진 담당 영역) — 채팅 답의 근거로만 쓴다."""
    row = conn.execute(
        "SELECT s.cooler_included FROM catalog.cpu_spec s JOIN catalog.product_variant v ON v.product_id = s.product_id "
        "WHERE v.id = %s", (str(cpu_variant_id),)).fetchone()
    if row is None:
        return None
    return (row[0] or "").strip() or None


def _has_box_cooler(value: str | None) -> bool | None:
    if not value:
        return None
    return True if value.upper().startswith("O") else False if value.upper().startswith("X") else None


def cooler_lines(conn, stored_or_ctx, cpu_variant_id=None) -> list[str]:
    """쿨러 관련 사실: CPU 기본 쿨러 포함 여부, 별도 쿨러가 담겼는지, 둘 다 없으면 ⚠.
    cpu_variant_id 를 주면 그 CPU 로 바꾼다고 가정한다(preview)."""
    picked = stored_or_ctx.picked if isinstance(stored_or_ctx, _Ctx) else [r for r in stored_or_ctx if r.get("selected") is not False]
    cpu = next((r for r in picked if r["slot"] == "CPU"), None)
    cooler = next((r for r in picked if r["slot"] == "쿨러"), None)
    cpu_vid = cpu_variant_id or (cpu["variant_id"] if cpu else None)
    if cpu_vid is None:
        return []
    raw = box_cooler(conn, cpu_vid)
    has = _has_box_cooler(raw)
    lines = [f"CPU 기본 쿨러(카탈로그 값): {raw or '정보 없음'}"]
    if cooler is not None:
        lines.append(f"별도 쿨러가 담겨 있음: {cooler['product_name']} {fmt_money(_price(cooler))}"
                     + (" — CPU 박스에 기본 쿨러가 들어 있어(정품 박스 기준) 빼면 그만큼 줄고 기본 쿨러를 쓰게 됨" if has else ""))
    elif has is False:
        lines.append("⚠ 별도 쿨러가 빠져 있는데 이 CPU는 기본 쿨러가 없을 수 있음(카탈로그 값 '" + raw + "') — CPU를 식힐 쿨러가 없을 수 있음")
    elif has is None:
        lines.append("별도 쿨러가 빠져 있고 CPU 기본 쿨러 포함 여부는 데이터에 없음")
    else:
        lines.append("별도 쿨러는 빠져 있고 CPU 박스의 기본 쿨러를 쓰게 됨")
    return lines


# ── 1. 바꾸면 어떻게 되나 (저장 안 함) ─────────────────────────────────────────
def step_candidate(ctx: _Ctx, slot: str, direction: str):
    """"한 단계 위/아래" — 지금 구성과 확정 비호환이 없는 후보 중 바로 다음 단계. CPU·GPU 는 성능 등급, RAM 은 용량으로
    가장 가까운 단계를 고르고(같은 단계면 더 싼 것), 그 값이 없는 슬롯은 가격으로 바로 위/아래. 없으면 None.
    예산은 보지 않는다 — 넘으면 preview 가 '예산 초과'로 알린다."""
    row = ctx.row(slot)
    cur = ctx.by_variant.get(str(row["variant_id"])) if row else None
    if cur is None:
        return None
    up = direction == "up"
    m0 = _metric(slot, cur.specs)
    pool = [c for c in ctx.pool.get(slot, []) if c.variant_id != cur.variant_id and not _new_failures(ctx, slot, c)]
    # 이 견적의 요구 사양(파워 용량·효율, 성능 하한 …)을 채우는 후보가 있으면 그 안에서 고른다 — "파워 더 싼 걸로"에
    # 750W·골드 요구를 못 채우는 700W 를 고르던 것(2026-10-02 평가). 다 못 채우면 그대로 두고 preview 가 ⚠ 로 알린다.
    meets = [c for c in pool if _requirement_verdict(ctx, slot, c)[0] != "Fail"]
    if m0 is not None:
        stepped = [c for c in meets if _metric(slot, c.specs) is not None
                   and (_metric(slot, c.specs) > m0 if up else _metric(slot, c.specs) < m0)]
        pool = stepped or pool
    else:
        stepped = [c for c in meets if (_price(c) > _price(cur) if up else _price(c) < _price(cur))]
        pool = stepped or pool
    if m0 is not None:
        pool = [c for c in pool if _metric(slot, c.specs) is not None
                and (_metric(slot, c.specs) > m0 if up else _metric(slot, c.specs) < m0)]
        if not pool:
            return None
        nearest = (min if up else max)(_metric(slot, c.specs) for c in pool)
        return min((c for c in pool if _metric(slot, c.specs) == nearest), key=_price)
    pool = [c for c in pool if (_price(c) > _price(cur) if up else _price(c) < _price(cur))]
    if not pool:
        return None
    return (min if up else max)(pool, key=_price)


def preview_swap(conn, revision_id: UUID, slot: str, variant_id: str | None = None, direction: str | None = None) -> str:
    """slot 을 variant_id(또는 direction='up'|'down' 이면 한 단계 위/아래 후보)로 바꾼다고 가정한 결과 — 가격·예산,
    성능 등급, 요구 사양, 부품 사이 호환. 구성표는 그대로다."""
    from src.services.recommendation_service import compat_checks
    ctx = _context(conn, revision_id)
    row = ctx.row(slot)
    if row is None:
        return f"오류: 담긴 부품 중 '{slot}' 슬롯이 없습니다."
    cur = ctx.by_variant.get(str(row["variant_id"]))
    if variant_id:
        cand = ctx.by_variant.get(str(variant_id))
        if cand is None or cand.slot != slot:
            return "오류: candidate_id 는 이 슬롯의 list_alternatives·upgrade_options·savings_options 결과 값이어야 합니다."
    elif direction in ("up", "down"):
        cand = step_candidate(ctx, slot, direction)
        if cand is None:
            word = "위" if direction == "up" else "아래"
            return (f"{slot}: 지금 {row['product_name']} {_metric_text(slot, _metric(slot, cur.specs) if cur else None)}"
                    f" — 지금 구성과 맞는 한 단계 {word} 후보가 카탈로그에 없습니다.")
    else:
        return "오류: candidate_id 또는 direction('up'/'down') 중 하나가 필요합니다."
    qty = _qty(row)
    delta = (_price(cand) - _price(row)) * qty
    total = ctx.total() + delta
    lines = [f"(가정 계산 — 구성표는 그대로) {slot}: {row['product_name']} {fmt_money(_price(row))}"
             f" → {cand.name} {fmt_money(_price(cand))}" + (f" × {qty}" if qty > 1 else "")
             + f" (차액 {fmt_money(delta, signed=True)}) · candidate_id={cand.variant_id}"]
    if ctx.budget_max:
        remaining = ctx.budget_max - total
        lines.append(f"바꾼 뒤 총액 {fmt_money(total)} · 예산 {fmt_money(ctx.budget_max)} · "
                     + (f"⚠ 예산 초과 {fmt_money(-remaining)}" if remaining < 0 else f"잔여 {fmt_money(remaining)}"))
    else:
        lines.append(f"바꾼 뒤 총액 {fmt_money(total)} (예산 상한 없음)")
    m0 = _metric(slot, cur.specs) if cur is not None else None
    m1 = _metric(slot, cand.specs)
    if m0 is not None or m1 is not None:
        lines.append(f"{slot} {_metric_text(slot, m0)} → {_metric_text(slot, m1)}")
    verdict, reasons = _requirement_verdict(ctx, slot, cand)
    if verdict == "Fail" and not variant_id:
        # step_candidate 는 요구 사양을 채우는 후보를 먼저 고른다 — 여기 왔으면 그런 후보가 카탈로그에 없다
        word = "위" if direction == "up" else "아래"
        lines.insert(0, f"이 견적의 요구 사양을 채우는 한 단계 {word} {slot}는 카탈로그에 없어, 가장 가까운 후보로 계산함:")
    if verdict == "Fail":
        lines.append(f"⚠ 이 견적의 요구 사양을 못 채움: {_reason_text(reasons)}")
    elif verdict == "Pending":
        lines.append("요구 사양 중 일부는 이 후보의 스펙 정보가 없어 확인 못 함")
    # 부품 사이 호환 — 화면 '호환성 점검 상세'와 같은 계산을 바꾼 구성으로 다시 돌린다.
    swapped = [{**r, "variant_id": cand.variant_id, "price": cand.price} if r["id"] == row["id"] else r
               for r in ctx.stored]
    before = {r["axis"]: r for r in compat_checks(conn, revision_id, ctx.cvals, ctx.stored)}
    after = [r for r in compat_checks(conn, revision_id, ctx.cvals, swapped) if r["axis"] != "budget"]
    fails = [r for r in after if r["state"] == "fail"]
    new_unknown = [r for r in after if r["state"] == "unknown" and (before.get(r["axis"]) or {}).get("state") != "unknown"]
    if fails:
        lines.append("⚠ 호환 점검 문제: " + " / ".join(f"{r['label']}: {r['detail']}" for r in fails))
    else:
        lines.append("호환 점검(소켓·메모리·크기·전력 등)을 바꾼 구성으로 돌렸고 확정된 문제는 없음")
    power = next((r for r in after if r["axis"] == "power" and r["state"] != "skipped"), None)
    if power and power not in fails:
        lines.append(f"전력: {power['detail']}")
    if new_unknown:
        lines.append("스펙 정보가 없어 확인 못 한 항목: " + ", ".join(r["label"] for r in new_unknown))
    if slot == "CPU":
        lines += [x for x in cooler_lines(conn, ctx, cand.variant_id) if x.startswith(("CPU 기본 쿨러", "⚠"))]
    return "\n".join(lines)


# ── 2. 지금 구성 점검 ──────────────────────────────────────────────────────
def check_build(conn, revision_id: UUID) -> str:
    """지금 선택된 구성의 호환 점검 항목별 결과(화면 '호환성 점검 상세'와 같은 계산)."""
    from src.services.recommendation_service import compat_checks
    ctx = _context(conn, revision_id)
    rows = [r for r in compat_checks(conn, revision_id, ctx.cvals, ctx.stored) if r["state"] != "skipped"]
    if not rows:
        return "점검할 부품이 없습니다."
    mark = {"ok": "통과", "fail": "⚠ 문제", "unknown": "스펙 정보 없어 확인 못 함"}
    lines = [f"- {r['label']}: {mark.get(r['state'], r['state'])} — {r['detail']}" for r in rows]
    # "CPU가 그래픽카드 발목 잡지 않아?" — 병목을 fps 로 계산하지는 않는다. 추천 엔진이 쓰는 용도별 기준 등급과
    # 지금 등급을 나란히 보여 줄 뿐이다(어느 쪽이 기준에 못 미치는지).
    ideals = _ideal_tiers(ctx)
    tiers = []
    for slot in ("CPU", "GPU"):
        row = ctx.row(slot)
        cand = ctx.by_variant.get(str(row["variant_id"])) if row else None
        tier = _metric(slot, cand.specs) if cand is not None else None
        if tier is not None and slot in ideals:
            tiers.append(f"{slot} {tier:g}(기준 {ideals[slot]:g}{', 못 미침' if tier < ideals[slot] else ''})")
    if tiers:
        lines.append("- 성능 등급과 이 용도 기준 등급: " + " · ".join(tiers)
                     + " — 병목(fps)은 계산하지 않고 등급만 비교")
    lines += [f"- {x}" for x in cooler_lines(conn, ctx)]
    return "\n".join(lines)


# ── 3. 남은 돈으로 올릴 수 있는 것 ───────────────────────────────────────────
def _upgrade_order(ctx: _Ctx, slots: list[str]) -> list[str]:
    """용도 기준 등급과의 차이가 큰 슬롯부터(기준 미달이 먼저). RAM 은 뒤."""
    ideals = _ideal_tiers(ctx)

    def gap(slot: str) -> float:
        if slot not in ideals:
            return float("-inf")
        cur = ctx.by_variant.get(str(ctx.row(slot)["variant_id"]))
        tier = _metric(slot, cur.specs) if cur is not None else None
        return ideals[slot] - tier if tier is not None else float("-inf")
    return sorted(slots, key=gap, reverse=True)


def upgrade_options(conn, revision_id: UUID, budget: int | None = None, new_budget: int | None = None) -> str:
    """`budget`(기본: 예산 잔여) 안에서 CPU·GPU 성능 등급, RAM 용량을 올릴 수 있는 후보. 한 번에 한 부품씩 바꾼다고
    가정하고, 이 견적의 요구 사양과 지금 구성과의 호환(확정 비호환 없음)을 통과한 것만 낸다.
    `new_budget`(사용자가 말한 새 총예산)을 주면 새 예산 - 총액 안에서 찾고, '바꾼 뒤 잔여'도 새 예산으로 계산한다 —
    원래 예산으로 계산하면 후보 잔여가 음수로 나와 모델이 "새 예산 초과"로 옮긴다(10/7 베이스라인 비사실)."""
    ctx = _context(conn, revision_id)
    total = ctx.total()
    cap = ctx.budget_max                     # '바꾼 뒤 잔여'의 기준 예산
    if new_budget is not None:
        cap = new_budget
        budget = new_budget - total
        head = f"새 예산 {fmt_money(new_budget)} · 총액 {fmt_money(total)} · 잔여 {fmt_money(budget)} 안에서 올릴 수 있는 부품"
    elif budget is None:
        if not ctx.budget_max:
            return "예산 상한이 없는 견적이라 '남은 예산'을 계산할 수 없습니다. 쓸 수 있는 금액을 알려 주시면 그 안에서 찾습니다."
        budget = ctx.budget_max - total
        head = f"예산 {fmt_money(ctx.budget_max)} · 총액 {fmt_money(total)} · 잔여 {fmt_money(budget)} 안에서 올릴 수 있는 부품"
    else:
        head = f"추가 금액 {fmt_money(budget)} 안에서 올릴 수 있는 부품 (지금 총액 {fmt_money(total)})"
    if budget <= 0:
        return (f"예산 {fmt_money(cap)} 중 총액 {fmt_money(total)} — 남은 예산이 없습니다."
                " 더 쓸 수 있는 금액을 말씀해 주시면 그 안에서 찾습니다.")
    ideals = _ideal_tiers(ctx)
    present = [s for s in _UPGRADE_SLOTS if ctx.row(s) is not None]
    lines = [head + " (한 번에 한 부품씩, 요구 사양·호환 통과한 것만, 순서는 용도 기준 등급에 못 미치는 정도가 큰 부품부터):"]
    found = 0
    for slot in _upgrade_order(ctx, present):
        row = ctx.row(slot)
        cur = ctx.by_variant.get(str(row["variant_id"]))
        m0 = _metric(slot, cur.specs) if cur is not None else None
        if m0 is None:
            continue
        qty = _qty(row)
        ok = []
        for cand in ctx.pool.get(slot, []):
            m1 = _metric(slot, cand.specs)
            delta = (_price(cand) - _price(row)) * qty
            if m1 is None or m1 <= m0 or delta <= 0 or delta > budget:
                continue
            if _requirement_verdict(ctx, slot, cand)[0] == "Fail" or _new_failures(ctx, slot, cand):
                continue
            ok.append((m1, delta, cand))
        ideal = ideals.get(slot)
        basis = (f" · 이 용도 기준 등급 {ideal:g}" + (" (이미 채움)" if m0 >= ideal else " (못 미침)")) if ideal is not None else ""
        if not ok:
            lines.append(f"- {slot}: 지금 {cur.name} {_metric_text(slot, m0)}{basis} — 이 금액 안에서 올릴 후보 없음")
            continue
        found += 1
        step_metric = min(m for m, _, _ in ok)
        step = min((o for o in ok if o[0] == step_metric), key=lambda o: o[1])
        best_metric = max(m for m, _, _ in ok)
        best = min((o for o in ok if o[0] == best_metric), key=lambda o: o[1])
        picks = [("한 단계 위", step)] + ([("이 금액 안 가장 높은 등급", best)] if best is not step else [])
        lines.append(f"- {slot}: 지금 {cur.name} {_metric_text(slot, m0)}{basis}")
        for label, (m1, delta, cand) in picks:
            after = total + delta
            left = f", 바꾼 뒤 잔여 {fmt_money(cap - after)}" if cap else ""
            lines.append(f"    · {label}: {cand.name} {_metric_text(slot, m1)} · {fmt_money(_price(cand))}"
                         f" (추가 {fmt_money(delta)}, 바꾼 뒤 총액 {fmt_money(after)}{left}) · candidate_id={cand.variant_id}")
    if not found:
        lines.append("→ 이 금액 안에서 성능 등급·용량을 올릴 수 있는 후보가 없습니다. 더 쓸 수 있는 금액(예: 20만원)을 "
                     "말씀해 주시면 그 안에서 찾습니다.")
    lines.append("케이스·쿨러·파워·저장장치는 '더 좋다'를 정할 값이 없어 넣지 않았습니다.")
    return "\n".join(lines)


# ── 4. 줄일 수 있는 것 ──────────────────────────────────────────────────────
def _loss(slot: str, m0: float | None, m1: float | None) -> float:
    """바꿨을 때 잃는 성능 — CPU·GPU 는 등급 차, RAM 은 용량이 절반이 될 때마다 1. 값이 없는 슬롯(케이스·쿨러·파워·
    저장장치)은 0 — 요구 사양은 통과한 후보만 오므로 '잴 수 있는 손실이 없다'는 뜻이지 '같다'는 뜻은 아니다."""
    if m0 is None or m1 is None:
        return 0.0
    if slot == "RAM":
        import math
        return max(0.0, math.log2(m0 / m1)) if m1 > 0 else 99.0
    return max(0.0, m0 - m1)


def _only_performance_short(slot: str, reasons: list[str]) -> bool:
    """요구 사양 미달 이유가 성능 등급·용량·VRAM 뿐인가(CPU·GPU·RAM). "기준을 낮추면"은 이것만 뜻한다 — 500W 파워를
    "기준을 낮춘 절약"으로 내던 것(2026-10-02): 파워 용량 미달은 성능이 아니라 안정성 문제다."""
    return slot in _UPGRADE_SLOTS and all(
        r.split(":", 1)[0] in ("FAIL_PERF_BELOW", "FAIL_CAPACITY_BELOW", "FAIL_VRAM_BELOW") for r in reasons)


def _cheaper_options(ctx: _Ctx, slot: str, allow_fail: bool = False) -> list[tuple[int, float, object]]:
    """이 슬롯을 더 싸게 바꾸는 후보 (절약액, 손실, 후보) — 요구 사양을 확인해서 채우고(Pass) 지금 구성과 확정 비호환이
    없는 것만. 절약액과 손실의 파레토 앞쪽만 남긴다(같은 손실이면 더 많이 줄이는 것, 같은 절약이면 덜 잃는 것)."""
    row = ctx.row(slot)
    cur = ctx.by_variant.get(str(row["variant_id"]))
    m0 = _metric(slot, cur.specs) if cur is not None else None
    out = []
    for cand in ctx.pool.get(slot, []):
        saving = (_price(row) - _price(cand)) * _qty(row)
        if saving <= 0 or cand.variant_id == str(row["variant_id"]) or _new_failures(ctx, slot, cand):
            continue
        verdict, reasons = _requirement_verdict(ctx, slot, cand)
        if verdict == "Pending":
            continue                 # 스펙을 몰라 보류(Pending)된 후보는 절약안으로 내지 않는다
        if verdict == "Fail" and not (allow_fail and _only_performance_short(slot, reasons)):
            continue                 # 낮춰도 되는 기준은 성능 등급·용량뿐 — 파워 용량·효율·소켓 미달은 안 된다
        out.append((saving, _loss(slot, m0, _metric(slot, cand.specs)), cand))
    out.sort(key=lambda o: (o[1], -o[0]))
    front, best_saving = [], -1
    for o in out:                    # 손실 오름차순으로 보며 절약액이 커질 때만 남긴다
        if o[0] > best_saving:
            front.append(o)
            best_saving = o[0]
    return front


def _target_plan(ctx: _Ctx, options: dict, target: int, max_swaps: int = 3):
    """target 이상 줄이면서 **잃는 성능이 가장 적은** 조합. 같으면 바꾸는 부품이 적은 것, 그다음 목표에 가까운(덜 줄이는) 것.
    예전엔 절약액이 큰 것부터 더해 "10만원 줄여줘"에 CPU 를 등급 8→4 로 내려 18.6만원을 줄였다(2026-10-02 시연)."""
    from itertools import combinations, product
    from src.engine.stage4_optimize import _pc_known_failures
    base = ctx.chosen()
    baseline = _pc_known_failures(base, ctx.spec, ctx.rules)
    best = None
    slots = [sl for sl, opts in options.items() if opts]
    for k in range(1, min(max_swaps, len(slots)) + 1):
        for combo in combinations(slots, k):
            for picks in product(*(options[sl][:5] for sl in combo)):
                saving = sum(p[0] for p in picks)
                if saving < target:
                    continue
                key = (round(sum(p[1] for p in picks), 3), k, saving)
                if best is not None and key >= best[0]:
                    continue
                trial = {**base, **{sl: p[2] for sl, p in zip(combo, picks)}}
                if _pc_known_failures(trial, ctx.spec, ctx.rules) - baseline:
                    continue          # 바꾼 부품끼리 안 맞는다
                best = (key, list(zip(combo, picks)))
    return best


def savings_options(conn, revision_id: UUID, target: int | None = None) -> str:
    """요구 사양을 채우면서 더 싸게 바꿀 수 있는 부품. target 이 있으면 그 금액 이상 줄이면서 성능을 가장 적게 잃는
    조합(바꾼 부품끼리의 호환도 확인)을 먼저 낸다. 요구 사양을 낮춰야 줄어드는 것은 따로 ⚠ 로 적는다."""
    ctx = _context(conn, revision_id)
    total = ctx.total()
    options, relaxed = {}, []
    for row in ctx.picked:
        slot = row["slot"]
        options[slot] = _cheaper_options(ctx, slot)
        cur = ctx.by_variant.get(str(row["variant_id"]))
        m0 = _metric(slot, cur.specs) if cur is not None else None
        below = None
        for cand in ctx.pool.get(slot, []):
            saving = (_price(row) - _price(cand)) * _qty(row)
            m1 = _metric(slot, cand.specs)
            if saving <= 0 or m0 is None or m1 is None or m1 >= m0 or _new_failures(ctx, slot, cand):
                continue
            verdict, reasons = _requirement_verdict(ctx, slot, cand)
            if verdict == "Fail":
                # 성능 등급·용량을 낮추는 대신 싸지는 것 — 바로 아래 단계(가장 덜 내려가는 것) 중 가장 싼 것
                key = (-m1, -saving)
                if below is None or key < below[0]:
                    below = (key, saving, cand, reasons)
        if below is not None:
            relaxed.append((below[1], slot, row, below[2], m0, _metric(slot, below[2].specs), below[3]))
    relaxed.sort(key=lambda o: o[0], reverse=True)

    def swap_text(slot: str, saving: int, loss: float, cand) -> str:
        row = ctx.row(slot)
        cur = ctx.by_variant.get(str(row["variant_id"]))
        m0, m1 = (_metric(slot, cur.specs) if cur else None), _metric(slot, cand.specs)
        verdict, reasons = _requirement_verdict(ctx, slot, cand)
        perf = (f" · {_metric_text(slot, m0)} → {_metric_text(slot, m1)}" if m0 is not None or m1 is not None
                else " · 성능 값 없음" + ("(요구 사양은 채움)" if verdict == "Pass" else ""))
        warn = f" · ⚠ {_reason_text(reasons)}" if verdict == "Fail" else ""
        return (f"{slot}: {row['product_name']} {fmt_money(_price(row))} → {cand.name} {fmt_money(_price(cand))}"
                f" (절약 {fmt_money(saving)}){perf}{warn} · candidate_id={cand.variant_id}")

    lines = [f"지금 총액 {fmt_money(total)}" + (f" · 줄이고 싶은 금액 {fmt_money(target)}" if target else "")]
    if target:
        plan = _target_plan(ctx, options, target)
        if plan is not None:
            (loss, _, saving), swaps = plan
            lines.append(f"→ {fmt_money(target)} 이상 줄이면서 성능을 가장 적게 잃는 조합"
                         f"(요구 사양·호환 통과, 바꾼 부품끼리의 호환도 확인): 합계 절약 {fmt_money(saving)}, "
                         f"바꾼 뒤 총액 {fmt_money(total - saving)}"
                         + (f"{', 잔여 ' + fmt_money(ctx.budget_max - total + saving) if ctx.budget_max else ''}"))
            lines += [f"    · {swap_text(sl, p[0], p[1], p[2])}" for sl, p in swaps]
        else:
            reach = sum(max((o[0] for o in opts), default=0) for opts in options.values())
            lines.append(f"→ 요구 사양을 지키면서 {fmt_money(target)}을 줄이는 조합(부품 3개까지)은 없습니다"
                         f" — 슬롯마다 가장 싼 후보를 다 더해도 최대 {fmt_money(reach)}.")
            # 기준을 낮추면 되는가도 코드가 더한다 — 모델이 "둘 다 낮춰도 못 미친다"(153,380+85,520 > 200,000)고 틀렸다
            loose = {r["slot"]: _cheaper_options(ctx, r["slot"], allow_fail=True) for r in ctx.picked}
            relaxed_plan = _target_plan(ctx, loose, target)
            if relaxed_plan is not None:
                (_, _, saving), swaps = relaxed_plan
                lines.append(f"→ 요구 사양을 낮추면 {fmt_money(target)} 이상 줄일 수 있는 조합(성능을 가장 적게 잃는 것): "
                             f"합계 절약 {fmt_money(saving)}, 바꾼 뒤 총액 {fmt_money(total - saving)} — ⚠ 표시가 이 견적의 기준 미달")
                lines += [f"    · {swap_text(sl, p[0], p[1], p[2])}" for sl, p in swaps]
            else:
                lines.append(f"→ 요구 사양을 낮춰도 부품 3개까지 바꿔서는 {fmt_money(target)}을 줄일 수 없습니다.")
    singles = sorted(((opts[-1], sl) for sl, opts in options.items() if opts), key=lambda x: -x[0][0])
    if singles:
        lines.append("부품 하나만 바꿀 때 가장 많이 줄어드는 후보 (요구 사양 통과, 큰 순):")
        lines += [f"- {swap_text(sl, o[0], o[1], o[2])}" for o, sl in singles]
    elif not target:
        lines.append("이 견적의 요구 사양을 채우면서 더 싸게 바꿀 수 있는 부품이 없습니다.")
    if relaxed:
        lines.append("요구 사양을 한 단계 낮추면 (⚠ 이 견적의 조건이 정한 기준 아래로 내려감):")
        for saving, slot, row, cand, m0, m1, reasons in relaxed:
            lines.append(f"- {slot}: {row['product_name']} → {cand.name} {fmt_money(_price(cand))} (절약 {fmt_money(saving)})"
                         f" · {_metric_text(slot, m0)} → {_metric_text(slot, m1)} · ⚠ {_reason_text(reasons)}"
                         f" · candidate_id={cand.variant_id}")
    lines.append("아직 아무것도 바꾸지 않았습니다.")
    return "\n".join(lines)


# ── 5. 이 게임 돌아가나 ──────────────────────────────────────────────────────
def game_check(conn, revision_id: UUID, game: str) -> str:
    """게임 요구 사양 표(추천 엔진이 쓰는 game_titles)와 지금 구성의 성능 등급·용량을 비교한다."""
    from src.engine.stage2_requirement import game_title_label, game_title_status, load_computer_rules, match_games
    rules = load_computer_rules()
    keys, tiers, unknown = match_games(game, rules)
    if not keys:
        return (f"'{game}'은(는) 게임 요구 사양 표에 없어 비교할 수 없습니다. "
                "해당 게임의 공식 권장 사양을 확인해 주세요.")
    ctx = _context(conn, revision_id)

    def spec_of(slot: str) -> dict:
        row = ctx.row(slot)
        cand = ctx.by_variant.get(str(row["variant_id"])) if row else None
        if cand is not None:
            return cand.specs
        return ((ctx.spec.owned.get(slot) or {}).get("specs") or {})
    gpu, cpu = spec_of("GPU"), spec_of("CPU")
    ram_row = ctx.row("RAM")
    ram_gb = (spec_of("RAM").get("capacity_gb") or 0) * _qty(ram_row) if ram_row else None
    have = {"gpu": gpu.get("perf_tier"), "cpu": cpu.get("perf_tier"), "ram_gb": ram_gb or None, "vram_gb": gpu.get("vram_gb")}
    label = {"gpu": "GPU 성능 등급", "cpu": "CPU 성능 등급", "ram_gb": "RAM 용량(GB)", "vram_gb": "VRAM(GB)"}
    lines = []
    for key, need in zip(keys, tiers):
        status = game_title_status(key, rules)["status"]
        parts, short = [], []
        for k in ("gpu", "cpu", "ram_gb", "vram_gb"):
            if need.get(k) is None:
                continue
            h = have.get(k)
            if h is None:
                parts.append(f"{label[k]} 요구 {need[k]:g} / 지금 정보 없음")
                continue
            ok = float(h) >= float(need[k])
            parts.append(f"{label[k]} 요구 {need[k]:g} / 지금 {float(h):g}" + ("" if ok else " ⚠ 미달"))
            if not ok:
                short.append(label[k])
        verdict = (f"요구 기준 미달: {', '.join(short)}" if short else "표의 요구 기준을 모두 채움")
        src = "배급사 권장 사양 확인값" if status == "approved" else "권장 사양을 대략 옮긴 잠정값이라 공식 권장 사양 확인 필요"
        aliases = (rules["requirements"]["game_titles"].get(key) or {}).get("aliases") or []
        names = "/".join(dict.fromkeys(a for a in aliases if re.search(r"[가-힣]", a))) or game_title_label(key, rules)
        lines.append(f"{names}: {verdict} — " + " · ".join(parts) + f" ({src})")
    if unknown:
        lines.append(f"표에 없는 게임: {', '.join(unknown)}")
    lines.append("표의 기준은 권장 사양 수준이며 실제 프레임(fps)은 계산하지 않습니다.")
    return "\n".join(lines)


# ── 화면에 낼 때 ─────────────────────────────────────────────────────────────
_CANDIDATE_ID_RE = re.compile(r"\s*·\s*candidate_id=[0-9a-f-]+")


def for_user(text: str) -> str:
    """도구 결과를 사용자에게 그대로 보일 때(규칙 경로·수치 가드 대체 문장) 내부 id 를 뺀다."""
    return _CANDIDATE_ID_RE.sub("", text)


# ── 규칙 경로: 에이전트가 없거나 실패했을 때 묻는 말을 고른다 (P5) ──────────────────
_UPGRADE_WORDS = ("남은", "남았", "남는", "남아", "업그레이드", "더 쓰", "더 써", "더 투자")
_SAVING_WORDS = ("줄이", "줄여", "아끼", "아낄", "제일 싸", "가장 싸", "싸지", "싸게 하", "싸게 할")
_CHECK_WORDS = ("충분", "호환", "문제 없", "문제없", "사도 돼", "사도 될", "부족하")
_GAME_WORDS = ("돌아가", "돌아갈", "돌려", "돌릴", "구동", "할 수 있", "되나", "될까", "가능", "프레임", "fps")
_PARTICLES = re.compile(r"(은|는|이|가|을|를|도|로|으로|에서|에|랑|이랑)$")


def _games_in(text: str) -> list[str]:
    from src.engine.stage2_requirement import match_games
    found = []
    for tok in re.split(r"[\s,?!.]+", text):
        tok = _PARTICLES.sub("", tok)
        if tok and match_games(tok)[0]:
            found.append(tok)
    return found


def rule_reply(conn, revision_id: UUID, text: str, slot: str | None) -> str | None:
    """슬롯·방향이 없는 묻는 말(남은 예산·절약·점검·게임)을 키워드로 골라 계산 결과를 돌려준다. 해당 없으면 None.
    에이전트와 같은 함수를 부르니 숫자는 같다 — 문장만 덜 다듬어졌다."""
    from src.engine.slot_rules import _parse_won
    if not is_pc(conn, revision_id):
        return None
    games = _games_in(text) if any(w in text.lower() for w in _GAME_WORDS) else []
    if games:
        return game_check(conn, revision_id, ", ".join(games))
    if any(w in text for w in _CHECK_WORDS):
        return check_build(conn, revision_id)
    if slot is None and any(w in text for w in _SAVING_WORDS):
        return for_user(savings_options(conn, revision_id, _parse_won(text)))
    if slot is None and any(w in text for w in _UPGRADE_WORDS):
        return for_user(upgrade_options(conn, revision_id, _parse_won(text)))
    return None
