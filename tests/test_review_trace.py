"""[5] 의 리뷰 관측이 저장 경로(reasoning_log · explanation_text)까지 실리는지.

DB 경로가 review_line_by_slot·caveats 를 읽지 않아 감점만 되고 문장은 사라지던 것을 막는다.
순수 함수만 검증한다 — DB 없이 돈다.
"""
import pytest

from src.services.review_service import (
    REVIEW_TRACE_STEP,
    explanation_text_with_caveats,
    review_trace_step,
)

NONE_LINE = "리뷰 관측 없음 (리뷰 수 문턱 미만이거나 데이터 기간 밖)"


def test_trace_step_carries_observed_slots():
    step = review_trace_step({
        "CPU": NONE_LINE,
        "메인보드": "리뷰 173건 관측 — 대조군 중앙값 대비 특이 없음",
        "케이스": "리뷰 449건 관측 — 7일 몰림 18% (부류 중앙값 5.5%)",
    })
    assert step is not None
    assert step["step"] == REVIEW_TRACE_STEP
    assert "2/3 슬롯" in step["title"]
    assert "메인보드" in step["detail"] and "케이스" in step["detail"]
    assert "CPU" not in step["detail"]          # 관측 없는 슬롯은 싣지 않는다


@pytest.mark.parametrize("lines", [
    {},                                         # 슬롯 자체가 없음
    {"CPU": NONE_LINE, "GPU": NONE_LINE},       # 전부 관측 없음
    None,
])
def test_trace_step_absent_when_nothing_observed(lines):
    """"리뷰를 봤지만 깨끗했다" 와 "볼 리뷰가 없었다" 를 같게 보이지 않게 한다."""
    assert review_trace_step(lines) is None


def test_explanation_text_appends_caveats():
    text = explanation_text_with_caveats(
        ["CPU — 조건 충족, 1순위", "GPU — 조건 충족, 1순위"],
        ["케이스 리뷰 관측: 7일 몰림이 부류 중앙값의 2배를 넘습니다"],
    )
    assert text.startswith("- CPU — 조건 충족, 1순위")
    assert "확인이 필요한 것:" in text
    assert "케이스 리뷰 관측" in text


def test_explanation_text_without_caveats_is_reasons_only():
    text = explanation_text_with_caveats(["CPU — 조건 충족"], [])
    assert text == "- CPU — 조건 충족"
    assert "확인이 필요한 것" not in text
