"""确定性与版本一致性测试。"""
from app.service import canonical_json, compute_plan, resolve_selection
from app.svg import render_overview


def test_recompute_identical(client, job_payload):
    """同一输入两次计算：JSON 与 SVG 完全一致。"""
    r1 = client.post("/api/compute", json={"job": job_payload, "candidate_index": 0})
    r2 = client.post("/api/compute", json={"job": job_payload, "candidate_index": 0})
    assert r1.text == r2.text

    rec = client.post("/api/plans", json={"job": job_payload, "candidate_index": 0}).json()
    svg1 = client.get(f"/api/plans/{rec['id']}/svg").text
    # 重新计算得到的 SVG 应与存储的一致
    from app.models import JobInput

    job = JobInput(**job_payload)
    plan = compute_plan(job, resolve_selection(job, 0, None))
    assert render_overview(plan) == svg1


def test_saved_versions_consistent(client, job_payload):
    """保存多个版本后，每个版本的重算校验都一致。"""
    for idx in (0, 1, 2):
        client.post("/api/plans", json={"job": job_payload, "candidate_index": idx})
    plans = client.get("/api/plans").json()
    assert len(plans) == 3
    assert [p["version"] for p in plans] == [1, 2, 3]
    for p in plans:
        r = client.get(f"/api/plans/{p['id']}/verify")
        assert r.json()["consistent"] is True


def test_canonical_json_stable():
    a = {"b": 1, "a": [1, 2], "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": [1, 2], "b": 1}
    assert canonical_json(a) == canonical_json(b)
