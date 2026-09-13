"""拼版几何测试：可印区域、纸纹、爬移补偿、出血/安全边界检查。"""
import pytest

from app.errors import DomainError
from app.imposition import check_spec, effective_printable
from app.models import JobInput
from app.service import compute_plan, resolve_selection


def make_job(**over):
    base = {
        "job_name": "测试",
        "page": {"width": 148, "height": 210},
        "total_pages": 32,
        "paper": {"width": 700, "height": 1000, "thickness": 0.1, "grain": "vertical"},
        "press": {"gripper_mm": 10, "gripper_edge": "bottom"},
        "binding": "left",
        "bleed_mm": 3,
        "safety_mm": 3,
        "marks_margin_mm": 5,
        "signature_options": [{"pages": 16, "style": "standard"}],
    }
    base.update(over)
    return JobInput(**base)


def test_effective_printable_gripper():
    job = make_job()
    pr = effective_printable(job)
    assert (pr.x, pr.y, pr.width, pr.height) == (0, 0, 700, 990)


def test_printable_out_of_paper_rejected():
    job = make_job(press={"printable": {"x": 0, "y": 0, "width": 800, "height": 1000}})
    with pytest.raises(DomainError) as e:
        effective_printable(job)
    assert e.value.errors[0]["code"] == "PRINTABLE_OUT_OF_PAPER"


def test_out_of_printable_rejected():
    """纸张太小放不下 16 页帖 -> 越出可印区域。"""
    job = make_job(paper={"width": 400, "height": 500, "thickness": 0.1, "grain": "vertical"})
    errors = check_spec(job, job.signature_options[0])
    assert any(e["code"] == "OUT_OF_PRINTABLE" for e in errors)
    with pytest.raises(DomainError):
        compute_plan(job, resolve_selection(job, 0, None))


def test_grain_mismatch_rejected():
    """左装订要求纸纹竖向，横向纸纹应被拒绝。"""
    job = make_job(paper={"width": 700, "height": 1000, "thickness": 0.1, "grain": "horizontal"})
    errors = check_spec(job, job.signature_options[0])
    assert any(e["code"] == "GRAIN_MISMATCH" for e in errors)
    with pytest.raises(DomainError):
        compute_plan(job, resolve_selection(job, 0, None))


def test_grain_ok_for_top_binding():
    """天头装订要求纸纹横向。"""
    job = make_job(
        binding="top",
        paper={"width": 1000, "height": 700, "thickness": 0.1, "grain": "horizontal"},
    )
    errors = check_spec(job, job.signature_options[0])
    assert errors == []


def test_cells_within_printable():
    job = make_job()
    plan = compute_plan(job, resolve_selection(job, 0, None))
    pr = effective_printable(job)
    for sig in plan["signatures"]:
        for sheet in sig["sheets"]:
            for cell in sheet["front"]["cells"]:
                bb = cell["bleed_box"]
                assert bb["x"] >= pr.x - 1e-6
                assert bb["y"] >= pr.y - 1e-6
                assert bb["x"] + bb["width"] <= pr.x + pr.width + 1e-6
                assert bb["y"] + bb["height"] <= pr.y + pr.height + 1e-6


def test_creep_offsets_per_sheet():
    """32 页帖 2 张套帖：外层补偿 0.1mm，内层 0；方向朝书脊。"""
    job = make_job(
        total_pages=64,
        paper={"width": 700, "height": 1000, "thickness": 0.1, "grain": "vertical"},
        signature_options=[{"pages": 32, "style": "standard", "sheets": 2}],
    )
    plan = compute_plan(job, resolve_selection(job, 0, None))
    sig = plan["signatures"][0]
    assert [s["creep_offset_mm"] for s in sig["sheets"]] == [0.1, 0.0]
    # 外层张的页格存在非零爬移向量，且左右两半方向相反（都朝书脊线）
    creeps = [c["creep"]["dx"] for c in sig["sheets"][0]["front"]["cells"]]
    assert any(d > 0 for d in creeps) and any(d < 0 for d in creeps)
    assert all(abs(abs(d) - 0.1) < 1e-9 for d in creeps)
    # 内层张无爬移
    assert all(
        c["creep"]["dx"] == 0 and c["creep"]["dy"] == 0
        for c in sig["sheets"][1]["front"]["cells"]
    )


def test_creep_bleed_warning():
    """出血小于爬移量时给出出血不足警告。"""
    job = make_job(
        total_pages=64,
        bleed_mm=0.05,
        paper={"width": 700, "height": 1000, "thickness": 0.1, "grain": "vertical"},
        signature_options=[{"pages": 32, "style": "standard", "sheets": 2}],
    )
    plan = compute_plan(job, resolve_selection(job, 0, None))
    assert any("出血不足" in w for w in plan["validation"]["warnings"])


def test_inset_sheet_pagination():
    """32 页帖 2 张套帖：外层张承载 1-8、25-32 页，内层张承载 9-24 页。"""
    job = make_job(
        total_pages=64,
        signature_options=[{"pages": 32, "style": "standard", "sheets": 2}],
    )
    plan = compute_plan(job, resolve_selection(job, 0, None))
    sig = plan["signatures"][0]

    def pages_of(sheet):
        return sorted(
            c["page"]
            for side in ("front", "back")
            for c in sheet[side]["cells"]
        )

    assert pages_of(sig["sheets"][0]) == [1, 2, 3, 4, 5, 6, 7, 8,
                                          25, 26, 27, 28, 29, 30, 31, 32]
    assert pages_of(sig["sheets"][1]) == list(range(9, 25))


def test_blank_pages_in_last_signature():
    """末帖空白页：34 页 = 16+16+8，末帖 8 页中 6 页空白。"""
    job = make_job(total_pages=34, signature_options=[
        {"pages": 16, "style": "standard"},
        {"pages": 8, "style": "standard"},
    ])
    plan = compute_plan(job, resolve_selection(job, 0, None))
    last = plan["signatures"][-1]
    assert last["spec"]["pages"] == 8
    assert last["blank_pages"] == 6  # 40 - 34
    pages = [
        c["page"]
        for s in last["sheets"]
        for side in ("front", "back")
        for c in s[side]["cells"]
    ]
    assert sorted(p for p in pages if p is not None) == [33, 34]
    assert plan["totals"]["blank_pages"] == 6
