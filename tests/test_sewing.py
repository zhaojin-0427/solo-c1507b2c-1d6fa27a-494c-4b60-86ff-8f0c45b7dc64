"""锁线装订方案测试：候选搜索、创建选定、不可变性、来源冻结与各类拒绝。"""
import pytest


@pytest.fixture()
def plan_id(client, job_payload):
    r = client.post("/api/plans", json={"job": job_payload, "candidate_index": 0})
    assert r.status_code == 201
    return r.json()["id"]


def sew_params(plan_id, **over):
    base = {
        "plan_id": plan_id,
        "stitch": "chain",
        "head_margin_mm": 12,
        "tail_margin_mm": 12,
        "hole_count": {"min": 4, "max": 6},
        "min_spacing_mm": 12,
        "max_segment_length_mm": 500,
    }
    base.update(over)
    return base


def test_candidates_sorted_and_valid(client, plan_id):
    r = client.post("/api/sewing-plans/candidates", json=sew_params(plan_id))
    assert r.status_code == 200
    body = r.json()
    assert body["input_hash"]
    assert body["source"]["plan_id"] == plan_id
    cands = body["candidates"]
    assert cands
    keys = [
        (
            c["metrics"]["violations"],
            c["metrics"]["thread_changes"],
            c["metrics"]["total_thread_mm"],
            c["metrics"]["hole_deviation_mm"],
        )
        for c in cands
    ]
    assert keys == sorted(keys)
    head, tail = cands[0]["usable_range_mm"]
    for c in cands:
        holes = c["holes_mm"]
        assert c["hole_count"] == len(holes)
        assert all(head <= h <= tail for h in holes)
        assert all(
            b - a >= 12 - 1e-9 for a, b in zip(holes, holes[1:])
        )
        # 每帖都有进针/出针操作，线段累计用线单调递增
        for sig in c["signatures"]:
            types = [o["type"] for o in sig["operations"]]
            assert "needle_in" in types and "needle_out" in types
        cums = [s["cumulative_mm"] for s in c["segments"]]
        assert cums == sorted(cums)
        assert c["metrics"]["total_thread_mm"] == cums[-1]


def test_candidates_deterministic(client, plan_id):
    r1 = client.post("/api/sewing-plans/candidates", json=sew_params(plan_id))
    r2 = client.post("/api/sewing-plans/candidates", json=sew_params(plan_id))
    assert r1.text == r2.text


def test_save_idempotent_and_immutable(client, plan_id):
    req = sew_params(plan_id, candidate_index=0, name="锁线A")
    r1 = client.post("/api/sewing-plans", json=req)
    assert r1.status_code == 201
    rec1 = r1.json()
    assert rec1["created"] is True
    assert rec1["plan_id"] == plan_id
    assert rec1["input_hash"]

    r2 = client.post("/api/sewing-plans", json=req)
    rec2 = r2.json()
    assert rec2["created"] is False
    assert rec2["id"] == rec1["id"]  # 同一输入幂等

    # 选定不同候选 -> 同一来源的新版本
    r3 = client.post("/api/sewing-plans", json=sew_params(plan_id, candidate_index=1))
    rec3 = r3.json()
    assert rec3["created"] is True
    assert rec3["version"] == rec1["version"] + 1
    assert rec3["id"] != rec1["id"]

    # 不可变：无更新/删除接口
    assert client.put(f"/api/sewing-plans/{rec1['id']}", json={}).status_code == 405
    assert client.delete(f"/api/sewing-plans/{rec1['id']}").status_code == 405


def test_get_list_steps_svg_verify(client, plan_id):
    rec = client.post(
        "/api/sewing-plans", json=sew_params(plan_id)
    ).json()
    sid = rec["id"]

    r = client.get("/api/sewing-plans")
    assert any(p["id"] == sid for p in r.json())

    r = client.get(f"/api/sewing-plans/{sid}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["source"]["plan_id"] == plan_id
    assert detail["result"]["signatures"]
    assert detail["result"]["segments"]

    r = client.get(f"/api/sewing-plans/{sid}/steps")
    assert r.status_code == 200
    sigs = r.json()["signatures"]
    types = [o["type"] for s in sigs for o in s["operations"]]
    assert "needle_in" in types and "needle_out" in types
    assert "change_signature" in types and "finish" in types

    r = client.get(f"/api/sewing-plans/{sid}/svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.startswith("<svg")
    r = client.get(f"/api/sewing-plans/{sid}/svg", params={"signature": 0})
    assert r.status_code == 200 and r.text.startswith("<svg")
    assert client.get(
        f"/api/sewing-plans/{sid}/svg", params={"signature": 999}
    ).status_code == 404

    r = client.get(f"/api/sewing-plans/{sid}/verify")
    assert r.json()["consistent"] is True


def test_sewing_404(client):
    assert client.get("/api/sewing-plans/sew-nope").status_code == 404
    assert client.get("/api/sewing-plans/sew-nope/svg").status_code == 404
    assert client.get("/api/sewing-plans/sew-nope/steps").status_code == 404
    assert client.get("/api/sewing-plans/sew-nope/verify").status_code == 404


def test_plan_not_found(client):
    r = client.post("/api/sewing-plans/candidates", json=sew_params("plan-nope"))
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "PLAN_NOT_FOUND"
    r = client.post("/api/sewing-plans", json=sew_params("plan-nope"))
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "PLAN_NOT_FOUND"


def test_bad_candidate_index(client, plan_id):
    r = client.post("/api/sewing-plans", json=sew_params(plan_id, candidate_index=999))
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "BAD_CANDIDATE"


def test_locked_holes_in_layout(client, plan_id):
    params = sew_params(
        plan_id, signatures=[{"index": 0, "locked_holes_mm": [60.0, 150.0]}]
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 200
    for c in r.json()["candidates"]:
        assert 60.0 in c["holes_mm"] and 150.0 in c["holes_mm"]
        # 锁定孔在所属帖标记为锁定
        assert c["signatures"][0]["locked_holes_mm"] == [60.0, 150.0]


def test_locked_hole_out_of_bounds(client, plan_id):
    params = sew_params(plan_id, signatures=[{"index": 0, "locked_holes_mm": [5.0]}])
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "HOLE_OUT_OF_BOUNDS"


def test_locked_hole_in_forbidden_zone(client, plan_id):
    params = sew_params(
        plan_id,
        signatures=[
            {
                "index": 0,
                "locked_holes_mm": [60.0],
                "forbidden_zones": [{"start_mm": 50, "end_mm": 70}],
            }
        ],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "HOLE_IN_FORBIDDEN_ZONE"


def test_locked_damaged_conflict(client, plan_id):
    params = sew_params(
        plan_id,
        signatures=[{"index": 0, "locked_holes_mm": [60.0], "damaged_holes_mm": [60.0]}],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "LOCKED_DAMAGED_CONFLICT"


def test_required_hole_spacing(client, plan_id):
    params = sew_params(
        plan_id, signatures=[{"index": 0, "locked_holes_mm": [60.0, 65.0]}]
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "HOLE_SPACING"


def test_hole_count_infeasible(client, plan_id):
    params = sew_params(plan_id, min_spacing_mm=100, hole_count={"min": 4, "max": 6})
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "HOLE_COUNT_INFEASIBLE"


def test_margin_exceeds_spine(client, plan_id):
    params = sew_params(plan_id, head_margin_mm=150, tail_margin_mm=150)
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "MARGIN_EXCEEDS_SPINE"


def test_signature_index_out_of_range(client, plan_id):
    params = sew_params(plan_id, signatures=[{"index": 99, "locked_holes_mm": [60.0]}])
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "SIGNATURE_INDEX_OUT_OF_RANGE"


def test_damaged_hole_skipped(client, plan_id):
    # 帖0 锁定 60/150 使孔位固定，帖1 将 60 标为破损 -> 仅帖1 跳过该孔
    params = sew_params(
        plan_id,
        signatures=[
            {"index": 0, "locked_holes_mm": [60.0, 150.0]},
            {"index": 1, "damaged_holes_mm": [60.0]},
        ],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 200
    c = r.json()["candidates"][0]
    assert 60.0 in c["holes_mm"]
    sig0, sig1 = c["signatures"][0], c["signatures"][1]
    assert 60.0 in sig0["effective_holes_mm"]
    assert 60.0 not in sig1["effective_holes_mm"]
    assert sig1["skipped_holes_mm"] == [{"position_mm": 60.0, "reason": "damaged"}]


def test_forbidden_zone_skipped(client, plan_id):
    params = sew_params(
        plan_id,
        signatures=[
            {"index": 0, "locked_holes_mm": [60.0]},
            {"index": 1, "forbidden_zones": [{"start_mm": 55, "end_mm": 65}]},
        ],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 200
    c = r.json()["candidates"][0]
    assert 60.0 in c["signatures"][0]["effective_holes_mm"]
    assert 60.0 not in c["signatures"][1]["effective_holes_mm"]
    assert {sk["reason"] for sk in c["signatures"][1]["skipped_holes_mm"]} == {
        "forbidden_zone"
    }


def test_no_connectable_hole_rejected(client, plan_id):
    # 帖0 禁打前半段、帖1 禁打后半段 -> 两帖没有任何共同孔位
    params = sew_params(
        plan_id,
        hole_count={"min": 4, "max": 4},
        signatures=[
            {"index": 0, "forbidden_zones": [{"start_mm": 0, "end_mm": 105}]},
            {"index": 1, "forbidden_zones": [{"start_mm": 105, "end_mm": 210}]},
        ],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    codes = [e["code"] for e in r.json()["detail"]]
    assert "NO_CONNECTABLE_HOLE" in codes


def test_unclosed_path_and_thread_change(client, plan_id):
    # 帖0 锁定尾端孔 198，帖1 禁打区覆盖该孔 -> 边界孔错位，收线换线
    params = sew_params(
        plan_id,
        signatures=[
            {"index": 0, "locked_holes_mm": [198.0]},
            {"index": 1, "forbidden_zones": [{"start_mm": 195, "end_mm": 210}]},
        ],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 200
    c = r.json()["candidates"][0]
    assert c["metrics"]["thread_changes"] == 1
    assert c["unclosed_paths"] == [0]
    assert len(c["thread_paths"]) == 2
    assert c["thread_paths"][0]["closed"] is False
    assert c["thread_paths"][1]["closed"] is True
    ops0 = c["signatures"][0]["operations"]
    assert any(o["type"] == "finish" for o in ops0)
    change = [o for o in ops0 if o["type"] == "change_signature"][0]
    assert change["closed"] is False
    # 帖1 以新线起针
    assert c["signatures"][1]["operations"][0]["type"] == "needle_in"
    assert "起针" in c["signatures"][1]["operations"][0].get("note", "")


def test_closed_chain_by_default(client, plan_id):
    r = client.post("/api/sewing-plans/candidates", json=sew_params(plan_id))
    c = r.json()["candidates"][0]
    assert c["metrics"]["thread_changes"] == 0
    assert c["unclosed_paths"] == []
    assert len(c["thread_paths"]) == 1
    assert c["thread_paths"][0]["closed"] is True
    for sig in c["signatures"][:-1]:
        change = [o for o in sig["operations"] if o["type"] == "change_signature"][0]
        assert change["closed"] is True


def test_max_segment_length_violations(client, plan_id):
    params = sew_params(plan_id, max_segment_length_mm=20)
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 200
    c = r.json()["candidates"][0]
    assert c["metrics"]["violations"] > 0
    assert any(
        d["type"] == "segment_too_long"
        for d in c["metrics"]["violation_details"]
    )
    assert any(s["exceeds_max"] for s in c["segments"])


def test_tape_stitch_wraps(client, plan_id):
    params = sew_params(
        plan_id, stitch="tape", tapes=[{"position_mm": 105, "width_mm": 20}]
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 200
    for c in r.json()["candidates"]:
        # 锚孔必留
        assert 95.0 in c["holes_mm"] and 115.0 in c["holes_mm"]
        assert c["tapes"][0]["anchors_mm"] == [95.0, 115.0]
        # 每帖都有绕带操作，无未绕违规
        for sig in c["signatures"]:
            assert any(o["type"] == "wrap_tape" for o in sig["operations"])
        assert c["metrics"]["violations"] == 0
        wraps = [s for s in c["segments"] if s["kind"] == "wrap"]
        assert wraps and all(s["length_mm"] > 20 for s in wraps)


def test_tape_missed_wrap_violation(client, plan_id):
    # 帖1 禁打区盖住锚孔 -> 该帖无法绕带，计违规
    params = sew_params(
        plan_id,
        stitch="tape",
        tapes=[{"position_mm": 105, "width_mm": 20}],
        signatures=[
            {"index": 1, "forbidden_zones": [{"start_mm": 90, "end_mm": 100}]}
        ],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 200
    c = r.json()["candidates"][0]
    assert any(
        d["type"] == "tape_not_wrapped" and d["signature"] == 1
        for d in c["metrics"]["violation_details"]
    )
    assert c["metrics"]["violations"] >= 1


def test_tape_overlap_rejected(client, plan_id):
    params = sew_params(
        plan_id,
        stitch="tape",
        tapes=[
            {"position_mm": 100, "width_mm": 20},
            {"position_mm": 115, "width_mm": 20},
        ],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "TAPE_OVERLAP"


def test_tape_out_of_bounds(client, plan_id):
    params = sew_params(
        plan_id, stitch="tape", tapes=[{"position_mm": 5, "width_mm": 20}]
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "TAPE_OUT_OF_BOUNDS"


def test_tape_anchor_out_of_usable_range(client, plan_id):
    params = sew_params(
        plan_id,
        stitch="tape",
        head_margin_mm=100,
        tapes=[{"position_mm": 105, "width_mm": 20}],
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "HOLE_OUT_OF_BOUNDS"


def test_chain_with_tapes_rejected(client, plan_id):
    params = sew_params(
        plan_id, stitch="chain", tapes=[{"position_mm": 105, "width_mm": 20}]
    )
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "TAPES_NOT_ALLOWED"


def test_tape_stitch_requires_tapes(client, plan_id):
    params = sew_params(plan_id, stitch="tape")
    r = client.post("/api/sewing-plans/candidates", json=params)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "TAPES_REQUIRED"


def test_sewing_frozen_against_new_plan_versions(client, job_payload, plan_id):
    sew = client.post("/api/sewing-plans", json=sew_params(plan_id)).json()
    assert sew["plan_version"] == 1

    # 同一任务保存不同拼版候选 -> 拼版 v2，不得改写历史锁线方案
    r2 = client.post("/api/plans", json={"job": job_payload, "candidate_index": 1})
    assert r2.json()["version"] == 2

    detail = client.get(f"/api/sewing-plans/{sew['id']}").json()
    assert detail["source"]["plan_id"] == plan_id
    assert detail["source"]["plan_version"] == 1
    ver = client.get(f"/api/sewing-plans/{sew['id']}/verify").json()
    assert ver["consistent"] is True


def test_saved_result_matches_selected_candidate(client, plan_id):
    params = sew_params(plan_id)
    body = client.post("/api/sewing-plans/candidates", json=params).json()
    cands = body["candidates"]
    rec = client.post(
        "/api/sewing-plans", json={**params, "candidate_index": 1}
    ).json()
    detail = client.get(f"/api/sewing-plans/{rec['id']}").json()
    assert detail["result"] == cands[1]
    assert detail["input_hash"] == body["input_hash"]
