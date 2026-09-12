"""API 端到端测试：候选、计算、保存、不可变版本、导出、确定性。"""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_specs(client):
    r = client.get("/api/specs")
    assert r.status_code == 200
    assert r.json()["signature_pages"] == [8, 12, 16, 32]


def test_candidates(client, job_payload):
    r = client.post("/api/candidates", json=job_payload)
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"]
    first = body["candidates"][0]
    assert first["valid"] and first["recommended"]
    assert set(first["metrics"]) >= {
        "paper_area_mm2", "sheet_count", "fold_count", "blank_pages"
    }


def test_compute_deterministic(client, job_payload):
    r1 = client.post("/api/compute", json={"job": job_payload, "candidate_index": 0})
    r2 = client.post("/api/compute", json={"job": job_payload, "candidate_index": 0})
    assert r1.status_code == r2.status_code == 200
    assert r1.text == r2.text  # 同一输入重复计算结果一致
    plan = r1.json()
    assert plan["validation"]["ok"]
    sig = plan["signatures"][0]
    cell = sig["sheets"][0]["front"]["cells"][0]
    assert {"page", "rotation", "x", "y", "width", "height"} <= set(cell)


def test_compute_rejects_out_of_printable(client, job_payload):
    # 纸张小到连 8 页帖（需 312×436mm）都放不下 -> 所有候选无效
    job_payload["paper"]["width"] = 300
    job_payload["paper"]["height"] = 400
    r = client.post("/api/compute", json={"job": job_payload, "candidate_index": 0})
    assert r.status_code == 422
    codes = [e["code"] for e in r.json()["detail"]]
    assert "OUT_OF_PRINTABLE" in codes


def test_compute_explicit_signatures_also_validated(client, job_payload):
    # 显式折帖序列同样不能绕过越界校验
    job_payload["paper"]["width"] = 500
    job_payload["paper"]["height"] = 500
    req = {"job": job_payload, "signatures": [
        {"pages": 16, "style": "standard"},
        {"pages": 16, "style": "standard"},
        {"pages": 16, "style": "standard"},
        {"pages": 16, "style": "standard"},
    ]}
    r = client.post("/api/compute", json=req)
    assert r.status_code == 422
    codes = [e["code"] for e in r.json()["detail"]]
    assert "OUT_OF_PRINTABLE" in codes


def test_compute_rejects_grain(client, job_payload):
    job_payload["paper"]["grain"] = "horizontal"
    r = client.post("/api/compute", json={"job": job_payload, "candidate_index": 0})
    assert r.status_code == 422
    codes = [e["code"] for e in r.json()["detail"]]
    assert "GRAIN_MISMATCH" in codes


def test_save_idempotent_and_immutable(client, job_payload):
    req = {"job": job_payload, "candidate_index": 0, "name": "版本A"}
    r1 = client.post("/api/plans", json=req)
    assert r1.status_code == 201
    rec1 = r1.json()
    assert rec1["created"] is True
    r2 = client.post("/api/plans", json=req)
    rec2 = r2.json()
    assert rec2["created"] is False
    assert rec1["id"] == rec2["id"]  # 同一输入幂等

    # 不同选择 -> 同一任务的新版本
    r3 = client.post("/api/plans", json={"job": job_payload, "candidate_index": 1})
    rec3 = r3.json()
    assert rec3["created"] is True
    assert rec3["version"] == rec1["version"] + 1
    assert rec3["id"] != rec1["id"]

    # 不可变：无更新/删除接口
    assert client.put(f"/api/plans/{rec1['id']}", json={}).status_code == 405
    assert client.delete(f"/api/plans/{rec1['id']}").status_code == 405


def test_plan_get_export_steps_svg_verify(client, job_payload):
    rec = client.post("/api/plans", json={"job": job_payload, "candidate_index": 0}).json()
    pid = rec["id"]

    r = client.get(f"/api/plans/{pid}")
    assert r.status_code == 200
    assert r.json()["result"]["validation"]["ok"]

    r = client.get(f"/api/plans/{pid}/export")
    assert r.status_code == 200
    plates = r.json()["plates"]
    assert plates and {"side", "pages", "marks"} <= set(plates[0])

    r = client.get(f"/api/plans/{pid}/steps")
    assert r.status_code == 200
    steps = r.json()["signatures"][0]["folding"]
    assert steps["steps"] and steps["trims"]
    assert steps["steps"][0]["type"] == "fold"
    assert any(t["type"] == "trim" for t in steps["trims"])

    r = client.get(f"/api/plans/{pid}/svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.startswith("<svg")

    r = client.get(f"/api/plans/{pid}/svg", params={"sheet": 0, "side": "back"})
    assert r.status_code == 200
    assert r.text.startswith("<svg")

    r = client.get(f"/api/plans/{pid}/verify")
    assert r.status_code == 200
    assert r.json()["consistent"] is True

    # 列表
    r = client.get("/api/plans")
    assert any(p["id"] == pid for p in r.json())


def test_explicit_signatures_selection(client, job_payload):
    req = {
        "job": job_payload,
        "signatures": [
            {"pages": 32, "style": "standard", "sheets": 2},
            {"pages": 32, "style": "standard", "sheets": 2},
        ],
    }
    r = client.post("/api/compute", json=req)
    assert r.status_code == 200
    assert len(r.json()["signatures"]) == 2


def test_insufficient_signatures_rejected(client, job_payload):
    req = {"job": job_payload, "signatures": [{"pages": 16, "style": "standard"}]}
    r = client.post("/api/compute", json=req)
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "INSUFFICIENT_PAGES"


def test_bad_candidate_index(client, job_payload):
    r = client.post("/api/compute", json={"job": job_payload, "candidate_index": 999})
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "BAD_CANDIDATE"


def test_plan_404(client):
    assert client.get("/api/plans/plan-nonexistent").status_code == 404
    assert client.get("/api/plans/plan-nonexistent/svg").status_code == 404
