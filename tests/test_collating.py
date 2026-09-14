"""书脊配帖标测试：几何反算、阶梯图案、冲突定位、锁定前缀编号、
候选/计算/保存/导出/SVG 集成与未配置时的旧行为保持。"""
import pytest

from app.models import JobInput, SignatureSpec
from app.service import compute_plan

CM = {
    "mark_width_mm": 4,
    "mark_height_mm": 5,
    "start_offset_mm": 20,
    "step_mm": 12,
    "per_column": 10,
    "column_spacing_mm": 5,
    "safety_mm": 3,
}


@pytest.fixture()
def coll_job(job_payload):
    job = dict(job_payload)
    job["safety_mm"] = 6
    job["collating_marks"] = dict(CM)
    return job


def _plan(job_dict, sigs=None):
    job = JobInput(**job_dict)
    if sigs is None:
        from app.service import resolve_selection

        sigs = resolve_selection(job, 0, None)
    return compute_plan(job, sigs)


# ---------------------------------------------------------------------------
# 未配置时旧行为不变
# ---------------------------------------------------------------------------


def test_unconfigured_responses_unchanged(client, job_payload):
    r = client.post("/api/compute", json={"job": job_payload, "candidate_index": 0})
    assert r.status_code == 200
    body = r.json()
    assert "collating_marks" not in body
    assert "collating_marks" not in body["job"]  # 旧响应的 job 不含此键
    # 显式传 null 与不传完全等价（响应与哈希）
    explicit_null = dict(job_payload, collating_marks=None)
    r2 = client.post("/api/compute", json={"job": explicit_null, "candidate_index": 0})
    assert r2.text == r.text
    # job_hash 基于剔除该键后的序列化，与旧版一致
    import hashlib

    from app.service import canonical_json

    r3 = client.post("/api/candidates", json=job_payload)
    assert r3.json()["job_hash"] == hashlib.sha256(
        canonical_json(body["job"]).encode()
    ).hexdigest()
    r = client.post("/api/candidates", json=job_payload)
    assert "collating" not in r.json()["candidates"][0]
    rec = client.post(
        "/api/plans", json={"job": job_payload, "candidate_index": 0}
    ).json()
    exp = client.get(f"/api/plans/{rec['id']}/export").json()
    assert "collating_marks" not in exp
    assert "collating_marks" not in exp["plates"][0]
    svg = client.get(f"/api/plans/{rec['id']}/svg").text
    assert "配帖标" not in svg


def test_config_validation(client, coll_job):
    bad = dict(coll_job, collating_marks=dict(CM, step_mm=0))
    r = client.post("/api/compute", json={"job": bad, "candidate_index": 0})
    assert r.status_code == 422
    bad = dict(coll_job, collating_marks=dict(CM, per_column=0))
    r = client.post("/api/compute", json={"job": bad, "candidate_index": 0})
    assert r.status_code == 422
    bad = dict(coll_job, collating_marks=dict(CM, mark_width_mm=-1))
    r = client.post("/api/compute", json={"job": bad, "candidate_index": 0})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 几何：最外层印张、正反面、书脊边对齐、爬移
# ---------------------------------------------------------------------------


def test_mark_on_outermost_sheet_spine_edge(coll_job):
    """标记落在最外层印张（sheet 0）承载帖首页的页格书脊边上。"""
    plan = _plan(coll_job)
    coll = plan["collating_marks"]
    assert coll["spine_length_mm"] == 210.0
    assert coll["usable_range_mm"] == [3.0, 207.0]
    assert coll["first_conflict"] is None
    for m in coll["marks"]:
        assert m["sheet"] == 0  # 最外层印张
        sig = plan["signatures"][m["signature"]]
        assert m["page_start"] == sig["page_start"]
        assert m["page_end"] == sig["page_end"]
        # 承载帖首页（page_start）的页格
        cells = sig["sheets"][0][m["side"]]["cells"]
        cell = [c for c in cells if c["page"] == sig["page_start"]][0]
        cr = cell["creep"]
        eps = 1e-9
        # 标记在页格内部（含爬移），且与书脊侧边对齐
        assert m["x"] >= cell["x"] + cr["dx"] - eps
        assert m["y"] >= cell["y"] + cr["dy"] - eps
        assert m["x"] + m["width"] <= cell["x"] + cr["dx"] + cell["width"] + eps
        assert m["y"] + m["height"] <= cell["y"] + cr["dy"] + cell["height"] + eps
        touch_left = abs(m["x"] - (cell["x"] + cr["dx"])) < eps
        touch_right = abs(m["x"] + m["width"] - (cell["x"] + cr["dx"] + cell["width"])) < eps
        assert touch_left or touch_right  # 左右装订：书脊边为页格左/右边
        assert m["creep"] == cr  # 爬移与页格一致
        # 左右装订：标记旋转与所在面页格旋转一致（0/180）
        assert m["rotation"] == cell["rotation"]


def test_spine_positions_step_and_columns(coll_job):
    """沿书脊起始偏移 + 步距定位；超出每列帖数另起一列并内缩列间距。"""
    coll_job["collating_marks"]["per_column"] = 2
    coll_job["safety_mm"] = 12
    plan = _plan(coll_job)
    marks = plan["collating_marks"]["marks"]
    assert [(m["mark_number"], m["column"], m["spine_start_mm"]) for m in marks] == [
        (1, 0, 20.0),
        (2, 0, 32.0),
        (3, 1, 20.0),
        (4, 1, 32.0),
    ]
    # 阶梯图案按书脊顺序汇总
    pattern = plan["collating_marks"]["spine_pattern"]
    assert [p["order"] for p in pattern] == list(range(len(pattern)))
    assert [p["mark_number"] for p in pattern] == [1, 3, 2, 4]
    starts = [p["spine_start_mm"] for p in pattern]
    assert starts == sorted(starts)
    # 核对数据与阶梯图案同序
    check = plan["collating_marks"]["spine_check"]
    assert [c["mark_number"] for c in check] == [1, 3, 2, 4]
    assert check[0]["page_start"] == 1 and check[0]["side"] == "back"


def test_creep_applied_to_mark(coll_job):
    """32 页 2 张套帖：最外层爬移 0.1mm 同样作用于标记。"""
    coll_job["total_pages"] = 64
    coll_job["signature_options"] = [{"pages": 32, "style": "standard", "sheets": 2}]
    plan = _plan(coll_job, [SignatureSpec(pages=32, style="standard", sheets=2)] * 2)
    m = plan["collating_marks"]["marks"][0]
    sig = plan["signatures"][0]
    assert sig["sheets"][0]["creep_offset_mm"] == 0.1
    cell = [c for c in sig["sheets"][0][m["side"]]["cells"] if c["page"] == 1][0]
    assert m["creep"] == cell["creep"]
    assert abs(m["creep"]["dx"]) == 0.1


def test_top_binding_marks(coll_job):
    """天头装订：书脊长取页宽，标记沿水平书脊排布，宽高互换。"""
    coll_job["binding"] = "top"
    coll_job["paper"] = {
        "width": 1000,
        "height": 700,
        "thickness": 0.1,
        "grain": "horizontal",
    }
    plan = _plan(coll_job)
    coll = plan["collating_marks"]
    assert coll["spine_length_mm"] == 148.0
    for m in coll["marks"]:
        assert m["width"] == CM["mark_height_mm"]  # 沿书脊方向水平
        assert m["height"] == CM["mark_width_mm"]
        assert m["rotation"] in (90, 270)
        sig = plan["signatures"][m["signature"]]
        cells = sig["sheets"][0][m["side"]]["cells"]
        cell = [c for c in cells if c["page"] == sig["page_start"]][0]
        cr = cell["creep"]
        eps = 1e-9
        touch_top = abs(m["y"] - (cell["y"] + cr["dy"])) < eps
        touch_bottom = abs(m["y"] + m["height"] - (cell["y"] + cr["dy"] + cell["height"])) < eps
        assert touch_top or touch_bottom  # 上下装订：书脊边为页格上/下边


# ---------------------------------------------------------------------------
# 冲突检测（定位具体帖号）
# ---------------------------------------------------------------------------


def test_spine_overflow_locates_signature(client, coll_job):
    coll_job["collating_marks"]["start_offset_mm"] = 200
    r = client.post("/api/compute", json={"job": coll_job, "candidate_index": 0})
    assert r.status_code == 422
    first = r.json()["detail"][0]
    assert first["code"] == "MARK_SPINE_OVERFLOW"
    assert "帖2" in first["message"]  # 20+12=212 起第一枚越界的是第 2 帖


def test_safety_intrusion_locates_signature(client, coll_job):
    coll_job["collating_marks"]["per_column"] = 1  # 每帖一列，列距累计侵入安全区
    coll_job["collating_marks"]["column_spacing_mm"] = 3
    r = client.post("/api/compute", json={"job": coll_job, "candidate_index": 0})
    assert r.status_code == 422
    first = r.json()["detail"][0]
    assert first["code"] == "MARK_SAFETY_INTRUSION"
    assert "帖2" in first["message"]  # 第 2 帖起外沿 3+4=7 > 安全区 6


def test_overlap_locates_signature_pair(client, coll_job):
    coll_job["collating_marks"]["step_mm"] = 3  # 步距 < 标记高 -> 相邻重叠
    r = client.post("/api/compute", json={"job": coll_job, "candidate_index": 0})
    assert r.status_code == 422
    first = r.json()["detail"][0]
    assert first["code"] == "MARK_OVERLAP"
    assert "帖1" in first["message"] and "帖2" in first["message"]
    # 保存同样被拒绝
    r = client.post("/api/plans", json={"job": coll_job, "candidate_index": 0})
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "MARK_OVERLAP"


def test_overlap_checked_across_all_pairs(client, coll_job):
    """跨列跨帖号：列间距 < 标记宽时帖1(列0)与帖3(列1)相交 -> 同样定位。"""
    coll_job["safety_mm"] = 8  # 列1 外沿 2+4=6，不触发安全区侵入
    coll_job["collating_marks"]["per_column"] = 2
    coll_job["collating_marks"]["column_spacing_mm"] = 2  # < 标记宽 4
    sigs = [{"pages": 16, "style": "standard"}] * 4
    r = client.post("/api/compute", json={"job": coll_job, "signatures": sigs})
    assert r.status_code == 422
    first = r.json()["detail"][0]
    assert first["code"] == "MARK_OVERLAP"
    assert "帖1" in first["message"] and "帖3" in first["message"]
    # 帖2与帖4 同样相交，全部报告
    msgs = [e["message"] for e in r.json()["detail"] if e["code"] == "MARK_OVERLAP"]
    assert any("帖2" in m and "帖4" in m for m in msgs)


def test_conflict_response_includes_collating(client, coll_job):
    """冲突时 422 响应一并返回阶梯图案、每帖位置与首个冲突。"""
    coll_job["collating_marks"]["step_mm"] = 3
    r = client.post("/api/compute", json={"job": coll_job, "candidate_index": 0})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"][0]["code"] == "MARK_OVERLAP"
    coll = body["collating_marks"]
    assert len(coll["marks"]) == 4  # 每帖位置
    assert coll["spine_pattern"]  # 书脊阶梯图案
    assert coll["spine_check"]
    fc = coll["first_conflict"]
    assert fc["code"] == "MARK_OVERLAP" and fc["mark_number"] == 2
    # 显式折帖序列路径同样携带
    sigs = [{"pages": 16, "style": "standard"}] * 4
    r = client.post("/api/compute", json={"job": coll_job, "signatures": sigs})
    assert r.status_code == 422
    assert "collating_marks" in r.json()
    # 保存请求同样携带且不落库
    r = client.post("/api/plans", json={"job": coll_job, "candidate_index": 0})
    assert r.status_code == 422
    assert "collating_marks" in r.json()
    assert client.get("/api/plans").json() == []


def test_register_mark_collision(client, coll_job):
    """标记贴近页格角部的套准十字且净距不足 -> 定位帖号。"""
    coll_job["bleed_mm"] = 0
    coll_job["marks_margin_mm"] = 2  # 套准十字距页格角 1mm
    coll_job["signature_options"] = [{"pages": 12, "style": "roll"}]
    coll_job["collating_marks"] = dict(CM, start_offset_mm=205, safety_mm=0)
    sigs = [{"pages": 12, "style": "roll"}] * 6
    r = client.post("/api/compute", json={"job": coll_job, "signatures": sigs})
    assert r.status_code == 422
    codes = [e["code"] for e in r.json()["detail"]]
    assert "MARK_HITS_REGISTER" in codes
    assert "帖1" in r.json()["detail"][0]["message"]


def test_candidates_report_first_conflict(client, coll_job):
    """候选接口不拒绝，但在候选内返回阶梯数据与首个冲突并标记无效。"""
    coll_job["collating_marks"]["step_mm"] = 3
    r = client.post("/api/candidates", json=coll_job)
    assert r.status_code == 200
    cand = r.json()["candidates"][0]
    assert cand["valid"] is False
    coll = cand["collating"]
    assert coll["marks"] and coll["spine_pattern"]
    fc = coll["first_conflict"]
    assert fc["code"] == "MARK_OVERLAP"
    assert fc["mark_number"] == 2  # 首个冲突定位到帖2
    assert any(e["code"] == "MARK_OVERLAP" for e in cand["errors"])


# ---------------------------------------------------------------------------
# 锁定前缀编号
# ---------------------------------------------------------------------------


def test_locked_prefix_keeps_numbering(client, coll_job):
    """锁定帖保留原编号与原位置；重排剩余折帖只续排后续标记。"""
    coll_job["locked_signatures"] = [{"pages": 16, "style": "standard"}]
    r = client.post("/api/candidates", json=coll_job)
    assert r.status_code == 200
    cands = [c for c in r.json()["candidates"] if c["valid"]]
    assert len(cands) >= 2
    first = cands[0]["collating"]["marks"]
    for c in cands[1:]:
        marks = c["collating"]["marks"]
        # 锁定帖（帖1）标记完全一致
        assert marks[0] == first[0]
        assert marks[0]["mark_number"] == 1
        # 后续标记续排编号
        assert [m["mark_number"] for m in marks] == list(range(1, len(marks) + 1))
        assert marks[1]["mark_number"] == 2
        assert marks[1]["spine_start_mm"] == first[1]["spine_start_mm"]


# ---------------------------------------------------------------------------
# 保存 / 导出 / SVG / verify
# ---------------------------------------------------------------------------


def test_save_export_svg_verify(client, coll_job):
    rec = client.post("/api/plans", json={"job": coll_job, "candidate_index": 0})
    assert rec.status_code == 201
    pid = rec.json()["id"]

    # 制版 JSON：每枚标记的帖号、页码、印张序号、正反面、坐标、旋转
    exp = client.get(f"/api/plans/{pid}/export").json()
    plates_with = [p for p in exp["plates"] if p.get("collating_marks")]
    assert len(plates_with) == 4  # 4 帖各一枚
    for p in plates_with:
        m = p["collating_marks"][0]
        assert m["signature"] == p["signature"]
        assert m["sheet"] == p["sheet"] == 0
        assert m["side"] == p["side"]
        assert {
            "mark_number",
            "page_start",
            "page_end",
            "x",
            "y",
            "width",
            "height",
            "rotation",
        } <= set(m)
    # 按书脊顺序汇总的核对数据
    check = exp["collating_marks"]["spine_check"]
    assert [c["mark_number"] for c in check] == [1, 2, 3, 4]
    assert exp["collating_marks"]["spine_pattern"]

    # SVG：整案含阶梯面板，单张背面含帖号标记
    svg = client.get(f"/api/plans/{pid}/svg").text
    assert "书脊配帖标阶梯" in svg
    assert "帖1" in svg and "帖4" in svg
    svg_back = client.get(f"/api/plans/{pid}/svg", params={"sheet": 0, "side": "back"}).text
    assert "帖1" in svg_back

    # verify 重算一致
    r = client.get(f"/api/plans/{pid}/verify")
    assert r.json()["consistent"] is True

    # 幂等：同一输入重复保存返回同一版本
    rec2 = client.post("/api/plans", json={"job": coll_job, "candidate_index": 0}).json()
    assert rec2["created"] is False and rec2["id"] == pid


def test_compute_deterministic_with_marks(client, coll_job):
    r1 = client.post("/api/compute", json={"job": coll_job, "candidate_index": 0})
    r2 = client.post("/api/compute", json={"job": coll_job, "candidate_index": 0})
    assert r1.text == r2.text
    # 配置进入任务指纹：改配置 -> 不同 input_hash
    other = dict(coll_job)
    other["collating_marks"] = dict(CM, step_mm=13)
    r3 = client.post("/api/compute", json={"job": other, "candidate_index": 0})
    assert r3.json()["input_hash"] != r1.json()["input_hash"]
