"""配页规划测试：候选生成、锁定重排、指标比较。"""
import pytest

from app.errors import DomainError
from app.models import JobInput
from app.planner import generate_candidates, plan_metrics


def make_job(**over):
    base = {
        "job_name": "测试",
        "page": {"width": 148, "height": 210},
        "total_pages": 100,
        "paper": {"width": 700, "height": 1000, "thickness": 0.1, "grain": "vertical"},
        "press": {"gripper_mm": 10, "gripper_edge": "bottom"},
        "binding": "left",
        "signature_options": [
            {"pages": 16, "style": "standard"},
            {"pages": 8, "style": "standard"},
            {"pages": 32, "style": "standard", "sheets": 2},
        ],
    }
    base.update(over)
    return JobInput(**base)


def test_candidates_cover_and_minimal():
    job = make_job()
    cands = generate_candidates(job)
    assert cands
    for c in cands:
        total = sum(s["pages"] for s in c["signatures"])
        assert total >= job.total_pages
        blanks = total - job.total_pages
        assert blanks == c["metrics"]["blank_pages"]
        # 最小覆盖：空白页小于方案中最小的帖 -> 空白全部落在末帖
        assert blanks < min(s["pages"] for s in c["signatures"])


def test_candidates_sorted_and_deterministic():
    job = make_job()
    c1 = generate_candidates(job)
    c2 = generate_candidates(job)
    assert c1 == c2
    keys = [
        (c["metrics"]["blank_pages"], c["metrics"]["paper_area_mm2"],
         c["metrics"]["sheet_count"], c["metrics"]["fold_count"])
        for c in c1
    ]
    assert keys == sorted(keys)
    assert c1[0]["recommended"] is True


def test_metrics_values():
    job = make_job()
    cands = generate_candidates(job)
    first = cands[0]
    m = first["metrics"]
    sheets = sum(s["sheets"] for s in first["signatures"])
    assert m["sheet_count"] == sheets
    assert m["paper_area_mm2"] == pytest.approx(sheets * 700 * 1000)
    assert m["fold_count"] > 0


def test_locked_signatures_prefix_replan():
    """锁定前 2 帖后，剩余页段重新组合，所有候选都以锁定帖开头。"""
    job = make_job(
        total_pages=100,
        locked_signatures=[
            {"pages": 32, "style": "standard", "sheets": 2},
            {"pages": 16, "style": "standard"},
        ],
    )
    cands = generate_candidates(job)
    assert cands
    for c in cands:
        first_two = c["signatures"][:2]
        assert [s["pages"] for s in first_two] == [32, 16]
        total = sum(s["pages"] for s in c["signatures"])
        assert total >= 100


def test_locked_exceeds_total_rejected():
    with pytest.raises(Exception):
        make_job(total_pages=20, locked_signatures=[{"pages": 32, "style": "standard"}])


def test_mixed_sizes_and_blank_in_last():
    job = make_job(total_pages=90)
    cands = generate_candidates(job)
    best = cands[0]
    assert best["valid"]
    blanks = best["metrics"]["blank_pages"]
    last_pages = best["signatures"][-1]["pages"]
    assert blanks < last_pages  # 空白集中在末帖


def test_plan_metrics_blank_count():
    job = make_job(total_pages=90)
    cands = generate_candidates(job)
    for c in cands:
        total = sum(s["pages"] for s in c["signatures"])
        assert c["metrics"]["blank_pages"] == total - 90
