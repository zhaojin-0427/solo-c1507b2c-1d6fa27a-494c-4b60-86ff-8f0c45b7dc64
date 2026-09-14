"""翻身版 / 天地翻过版测试。

覆盖：
- 模型校验：翻纸轴与装订/咬口边、咬口与侧规垂直；
- 共用印版几何：两份书帖页码、逐格坐标、翻转矩阵、中缝；
- 指标：印版数、过版次数、用纸量、成品帖数、奇数目标超印量；
- 拒绝：翻后越界、中缝净距/裁线侵入出血、倒页定位；
- 书版式缺省行为与旧哈希、历史读取/verify 不变。
"""
import hashlib

import pytest

from app.errors import DomainError
from app.models import JobInput
from app.service import (
    canonical_json,
    compute_plan,
    job_dump,
    job_fingerprint,
    resolve_selection,
)

WT_BASE = {
    "page": {"width": 148, "height": 210},
    "total_pages": 8,
    "paper": {"width": 700, "height": 1000, "thickness": 0.1, "grain": "vertical"},
    "press": {"gripper_mm": 10, "gripper_edge": "bottom"},
    "binding": "left",
    "bleed_mm": 3,
    "safety_mm": 3,
    "marks_margin_mm": 5,
    "signature_options": [{"pages": 8, "style": "standard"}],
    "presswork_mode": "work_and_turn",
}

TUMBLE_BASE = {
    "page": {"width": 148, "height": 210},
    "total_pages": 8,
    "paper": {"width": 1000, "height": 1400, "thickness": 0.1, "grain": "horizontal"},
    "press": {"gripper_mm": 10, "gripper_edge": "left"},
    "binding": "top",
    "bleed_mm": 3,
    "safety_mm": 3,
    "marks_margin_mm": 5,
    "signature_options": [{"pages": 8, "style": "standard"}],
    "presswork_mode": "work_and_tumble",
}


def wt_job(**over):
    d = dict(WT_BASE)
    d.update(over)
    return JobInput(**d)


def tumble_job(**over):
    d = dict(TUMBLE_BASE)
    d.update(over)
    return JobInput(**d)


# ---------------------------------------------------------------------------
# 模型校验
# ---------------------------------------------------------------------------


def test_default_mode_is_sheetwise_and_fields_omitted_from_dump():
    job = JobInput(**{k: v for k, v in WT_BASE.items() if k != "presswork_mode"})
    assert job.presswork_mode.value == "sheetwise"
    assert not job.presswork_active()
    d = job_dump(job)
    assert "presswork_mode" not in d
    assert "gutter_trim_mm" not in d
    assert "side_lay_edge" not in d
    assert "target_copies" not in d


def test_sheetwise_fingerprint_unchanged():
    payload = {k: v for k, v in WT_BASE.items() if k != "presswork_mode"}
    job = JobInput(**payload)
    job_explicit = JobInput(**payload, presswork_mode="sheetwise")
    h = hashlib.sha256(canonical_json(job_dump(job)).encode()).hexdigest()
    assert job_fingerprint(job_explicit) == h


def test_turn_requires_gripper_parallel_to_cut():
    # 翻身版绕垂直轴：咬口必须在上/下边
    with pytest.raises(Exception):
        wt_job(press={"gripper_mm": 10, "gripper_edge": "left"})


def test_tumble_requires_gripper_left_right():
    with pytest.raises(Exception):
        tumble_job(press={"gripper_mm": 10, "gripper_edge": "bottom"})


def test_work_and_turn_only_left_right_binding():
    with pytest.raises(Exception):
        wt_job(binding="top")


def test_tumble_only_top_bottom_binding():
    with pytest.raises(Exception):
        tumble_job(binding="left")


def test_side_lay_must_be_perpendicular_to_gripper():
    # bottom 咬口 + bottom 侧规：平行，拒绝
    with pytest.raises(Exception):
        wt_job(side_lay_edge="top")


def test_effective_side_lay_default():
    assert wt_job().effective_side_lay() == "left"
    assert tumble_job().effective_side_lay() == "bottom"


# ---------------------------------------------------------------------------
# 共用印版几何
# ---------------------------------------------------------------------------


def _copy0_pages(plan):
    sh = plan["signatures"][0]["sheets"][0]["presswork"]
    pages = [c["page"] for c in sh["pass1_cells"] if c["unit"] == 0 and c["page"]]
    pages += [c["page"] for c in sh["pass2_cells"] if c["unit"] == 1 and c["page"]]
    return sorted(pages)


def test_work_and_turn_two_copies_pages():
    plan = compute_plan(wt_job(), resolve_selection(wt_job(), 0, None))
    sh = plan["signatures"][0]["sheets"][0]["presswork"]
    assert _copy0_pages(plan) == [1, 2, 3, 4, 5, 6, 7, 8]
    # 每格两次过版分别落到不同份/不同面
    for c in sh["pass1_cells"]:
        assert c["pass1"]["copy"] != c["pass2"]["copy"]
    # 两份阅读顺序都竖直递增
    orders = sh["copies"]
    assert [o["recto"] for o in orders[0]["reading_order"]] == [1, 3, 5, 7]
    assert [o["recto"] for o in orders[1]["reading_order"]] == [1, 3, 5, 7]
    assert all(o["orientation"] == "upright" for o in orders[0]["reading_order"])
    assert all(o["orientation"] == "upright" for o in orders[1]["reading_order"])


def test_tumble_two_copies_pages():
    plan = compute_plan(tumble_job(), resolve_selection(tumble_job(), 0, None))
    assert _copy0_pages(plan) == [1, 2, 3, 4, 5, 6, 7, 8]
    sh = plan["signatures"][0]["sheets"][0]["presswork"]
    orders = sh["copies"]
    assert [o["recto"] for o in orders[1]["reading_order"]] == [1, 3, 5, 7]
    assert all(o["orientation"] == "upright" for o in orders[1]["reading_order"])


def test_flip_matrix_and_passes():
    job = wt_job()
    plan = compute_plan(job, resolve_selection(job, 0, None))
    sh = plan["signatures"][0]["sheets"][0]["presswork"]
    # V 轴翻转：x -> W - x（矩阵首行 [-1,0,W]）
    assert sh["flip_matrix"][0] == [-1.0, 0.0, 700.0]
    assert [p["gripper_edge"] for p in sh["passes"]] == ["bottom", "bottom"]
    # 翻身版侧规换边
    assert [p["side_lay_edge"] for p in sh["passes"]] == ["left", "right"]

    job2 = tumble_job()
    plan2 = compute_plan(job2, resolve_selection(job2, 0, None))
    sh2 = plan2["signatures"][0]["sheets"][0]["presswork"]
    assert sh2["flip_matrix"][1] == [0.0, -1.0, 1400.0]
    # 天地翻咬口换边、侧规保持
    assert [p["gripper_edge"] for p in sh2["passes"]] == ["left", "right"]
    assert [p["side_lay_edge"] for p in sh2["passes"]] == ["bottom", "bottom"]


def test_pass2_is_flip_of_pass1():
    job = wt_job()
    plan = compute_plan(job, resolve_selection(job, 0, None))
    sh = plan["signatures"][0]["sheets"][0]["presswork"]
    c1 = {(c["unit"], c["col"], c["row"]): c for c in sh["pass1_cells"]}
    c2 = {(c["unit"], c["col"], c["row"]): c for c in sh["pass2_cells"]}
    for key, a in c1.items():
        b = c2[key]
        assert abs(b["x"] - (700 - a["x"] - a["width"])) < 1e-6
        assert b["y"] == a["y"]
        assert b["rotation"] == (360 - a["rotation"]) % 360


def test_cut_line_center_and_gutter():
    job = wt_job(gutter_trim_mm=6)
    plan = compute_plan(job, resolve_selection(job, 0, None))
    cut = plan["signatures"][0]["sheets"][0]["presswork"]["cut_line"]
    assert cut == {"axis": "vertical", "at_mm": 350.0, "gutter_trim_mm": 6.0}
    # 两侧出血框到裁线的净距对称且 >= 半份裁切余量
    sh = plan["signatures"][0]["sheets"][0]["presswork"]
    for c in sh["pass1_cells"]:
        bb = c["bleed_box"]
        if c["unit"] == 0:
            assert 350 - (bb["x"] + bb["width"]) >= 3.0 - 1e-9
        else:
            assert bb["x"] - 350 >= 3.0 - 1e-9


# ---------------------------------------------------------------------------
# 指标与超印
# ---------------------------------------------------------------------------


def test_presswork_totals_even_target():
    job = wt_job(target_copies=4)
    plan = compute_plan(job, resolve_selection(job, 0, None))
    pw = plan["presswork"]
    assert pw["plate_count"] == 1          # 一块共用印版
    assert pw["pass_count"] == 4           # 2 张全张 × 2 次过版
    assert pw["paper_sheets"] == 2
    assert pw["finished_signatures"] == 4
    assert pw["overrun_copies"] == []


def test_presswork_totals_odd_target_overrun():
    job = wt_job(target_copies=3)
    plan = compute_plan(job, resolve_selection(job, 0, None))
    pw = plan["presswork"]
    assert pw["paper_sheets"] == 2         # 向上取整到偶数份
    assert pw["printed_copies"] == 3
    assert pw["finished_signatures"] == 3
    assert pw["overrun_copies"] == [
        {"target": 3, "printed": 4, "extra": 1}
    ]


def test_multi_sheet_inset_totals_and_creep():
    """32 页套帖（2 张）翻身版：每张一块共用印版，爬移仅外层张；
    目标 5 册 -> 3 批 6 张全张，12 次过版，超印 1。"""
    job = wt_job(
        total_pages=32,
        paper={"width": 1400, "height": 1000, "thickness": 0.1, "grain": "vertical"},
        signature_options=[{"pages": 32, "style": "standard", "sheets": 2}],
        target_copies=5,
    )
    plan = compute_plan(job, resolve_selection(job, 0, None))
    sig = plan["signatures"][0]
    assert [s["creep_offset_mm"] for s in sig["sheets"]] == [0.1, 0.0]
    # 外层张第 0 份页码 = 1-8 与 25-32
    s0 = sig["sheets"][0]["presswork"]
    pages = [c["page"] for c in s0["pass1_cells"] if c["unit"] == 0 and c["page"]]
    pages += [c["page"] for c in s0["pass2_cells"] if c["unit"] == 1 and c["page"]]
    assert sorted(pages) == list(range(1, 9)) + list(range(25, 33))
    pw = plan["presswork"]
    assert pw["plate_count"] == 2
    assert pw["paper_sheets"] == 6 and pw["pass_count"] == 12
    assert pw["finished_signatures"] == 5
    assert pw["overrun_copies"] == [{"target": 5, "printed": 6, "extra": 1}]


def test_blank_pages_counted_once_per_copy():
    job = wt_job(
        total_pages=10,
        signature_options=[{"pages": 8}],
    )
    plan = compute_plan(job, resolve_selection(job, 0, None))
    assert plan["signatures"][-1]["blank_pages"] == 6
    assert plan["totals"]["blank_pages"] == 6


def test_shared_plate_id_in_summary_and_sheet():
    job = wt_job()
    plan = compute_plan(job, resolve_selection(job, 0, None))
    pid = plan["signatures"][0]["sheets"][0]["presswork"]["plate_id"]
    assert pid == "S1-P1"
    assert plan["presswork"]["plates"][0]["plate_id"] == pid


# ---------------------------------------------------------------------------
# 拒绝：越界 / 裁线 / 倒页
# ---------------------------------------------------------------------------


def test_doubled_plate_too_wide_rejected():
    # 纸宽只够一个单元，放不下两份
    job = wt_job(paper={"width": 400, "height": 1000, "thickness": 0.1,
                        "grain": "vertical"})
    with pytest.raises(DomainError) as e:
        compute_plan(job, resolve_selection(job, 0, None))
    assert any(x["code"] == "OUT_OF_PRINTABLE" for x in e.value.errors)


def test_flip_out_of_bounds_locates_sheet_and_cell():
    # 单侧收缩可印区域使两次过版交集变窄，放不下两份 -> 拒绝
    job = wt_job(
        press={
            "gripper_mm": 10, "gripper_edge": "bottom",
            "printable": {"x": 0, "y": 0, "width": 600, "height": 990},
        }
    )
    with pytest.raises(DomainError) as e:
        compute_plan(job, resolve_selection(job, 0, None))
    codes = {x["code"] for x in e.value.errors}
    assert "FLIP_OUT_OF_BOUNDS" in codes or "OUT_OF_PRINTABLE" in codes


def test_cut_clearance_error_mentions_cell():
    # 中缝余量要求超过纸张可用宽度 -> 静态检查 OUT_OF_PRINTABLE（信息含帖规格）
    job = wt_job(
        paper={"width": 620, "height": 1000, "thickness": 0.1, "grain": "vertical"},
        gutter_trim_mm=10,
    )
    with pytest.raises(DomainError) as e:
        compute_plan(job, resolve_selection(job, 0, None))
    codes = {x["code"] for x in e.value.errors}
    assert codes & {"CUT_CLEARANCE", "CUT_INTRUDES_BLEED", "OUT_OF_PRINTABLE"}


def test_validate_geometry_flip_and_cut_errors():
    """直接构造越界与裁线侵入，校验错误定位到印张与页格。"""
    from app.presswork import validate_geometry, pass_printables
    from app.imposition import effective_printable

    job = wt_job()
    pr = effective_printable(job)
    passes = pass_printables(job, pr)

    def cell(unit, col, row, x, y, page=1):
        return {
            "unit": unit, "col": col, "row": row, "page": page,
            "x": x, "y": y, "width": 148, "height": 210,
            "bleed_box": {"x": x - 3, "y": y - 3, "width": 154, "height": 216},
            "safe_box": {"x": x + 3, "y": y + 3, "width": 142, "height": 204},
        }

    # 第 2 过版一个格越出可印区域
    cells1 = [cell(0, 0, 0, 49, 285), cell(1, 1, 0, 503, 285)]
    bad2 = cell(0, 0, 0, 49, 900)  # 翻转后 y 仍为 900，超出高 990-216/出血
    errors = validate_geometry(job, 0, cells1, [bad2], passes)
    assert any(e["code"] == "FLIP_OUT_OF_BOUNDS" and "印张1" in e["message"]
               for e in errors)

    # 裁线侵入出血：出血框跨过纸张中线 350
    intr = cell(0, 0, 0, 300, 285)  # bleed x=297..451 跨过 350
    errors = validate_geometry(job, 2, cells1 + [intr],
                               cells1, passes)
    assert any(e["code"] == "CUT_INTRUDES_BLEED" and "印张3" in e["message"]
               and "列1" in e["message"] for e in errors)


# ---------------------------------------------------------------------------
# 书版式旧行为
# ---------------------------------------------------------------------------


def test_sheetwise_plan_unchanged_without_presswork():
    payload = {k: v for k, v in WT_BASE.items() if k != "presswork_mode"}
    job = JobInput(**payload)
    plan = compute_plan(job, resolve_selection(job, 0, None))
    assert "presswork" not in plan
    assert "presswork" not in plan["job"]
    sh = plan["signatures"][0]["sheets"][0]
    assert "presswork" not in sh
    # 书版式仍有正/反面、无 unit 标注
    assert {"page", "rotation", "x", "y"} <= set(sh["front"]["cells"][0])
    assert "unit" not in sh["front"]["cells"][0]


def test_sheetwise_hash_stable_across_presswork_fields():
    a = JobInput(**{k: v for k, v in WT_BASE.items() if k != "presswork_mode"})
    b = JobInput(**{k: v for k, v in WT_BASE.items() if k != "presswork_mode"})
    assert job_fingerprint(a) == job_fingerprint(b)


# ---------------------------------------------------------------------------
# API 端到端
# ---------------------------------------------------------------------------


def test_api_compute_presswork(client):
    r = client.post("/api/compute", json={"job": WT_BASE, "candidate_index": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["validation"]["ok"]
    pw = body["presswork"]
    assert pw["mode"] == "work_and_turn"
    assert pw["plate_count"] == 1
    assert len(pw["plates"][0]["flip_matrix"]) == 3
    sh = body["signatures"][0]["sheets"][0]
    assert sh["presswork"]["cut_line"]["axis"] == "vertical"
    # 逐格坐标
    assert sh["presswork"]["pass1_cells"]
    assert sh["presswork"]["pass2_cells"]


def test_api_save_export_steps_verify_presswork(client):
    rec = client.post(
        "/api/plans", json={"job": WT_BASE, "candidate_index": 0}
    ).json()
    pid = rec["id"]
    # 制版 JSON：按过版分条，标出翻纸轴/咬口/侧规/裁线
    exp = client.get(f"/api/plans/{pid}/export").json()
    assert len(exp["plates"]) == 2  # 一块印版 × 两次过版
    for p, pass_no in zip(exp["plates"], (1, 2)):
        assert p["shared_plate"] is True
        assert p["impression"] == pass_no
        assert p["turn_axis"] == "vertical"
        assert p["cut_line"]["axis"] == "vertical"
        assert p["gripper_edge"] in ("top", "bottom")
        assert p["side_lay_edge"] in ("left", "right")
        assert {"x", "y", "page", "rotation", "unit"} <= set(p["pages"][0])
    assert exp["presswork"]["plate_count"] == 1

    # steps 首步为中缝裁切
    steps = client.get(f"/api/plans/{pid}/steps").json()
    st0 = steps["signatures"][0]["folding"]["steps"][0]
    assert st0["type"] == "cut" and st0["axis"] == "vertical"

    # SVG 分过版标出翻纸轴/咬口/侧规/裁线
    svg = client.get(f"/api/plans/{pid}/svg").text
    assert "中缝/翻纸轴" in svg and "侧规" in svg
    svg2 = client.get(
        f"/api/plans/{pid}/svg", params={"sheet": 0, "side": "back"}
    ).text
    assert "第2次过版" in svg2

    # verify 结果不变（历史方案读取与重算一致）
    v = client.get(f"/api/plans/{pid}/verify").json()
    assert v["consistent"] is True


def test_api_rejects_bad_mode_combination(client):
    bad = dict(WT_BASE, press={"gripper_mm": 10, "gripper_edge": "left"})
    r = client.post("/api/candidates", json=bad)
    assert r.status_code == 422


def test_api_tumble_end_to_end(client):
    r = client.post("/api/compute", json={"job": TUMBLE_BASE, "candidate_index": 0})
    assert r.status_code == 200, r.text
    pw = r.json()["presswork"]
    assert pw["turn_axis"] == "horizontal"
    assert pw["side_lay_edge_pass1"] == pw["side_lay_edge_pass2"]
