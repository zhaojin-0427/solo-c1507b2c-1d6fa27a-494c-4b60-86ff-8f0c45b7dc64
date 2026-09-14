"""FastAPI 应用：印张拼版 API。"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .errors import DomainError, err
from .models import ComputeRequest, JobInput, SavePlanRequest
from .planner import generate_candidates
from .service import (
    canonical_json,
    compute_plan,
    job_dump,
    job_fingerprint,
    resolve_selection,
    selection_fingerprint,
)
from .sewing import (
    compute_candidates as sewing_candidates,
    sewing_fingerprint,
    snapshot_from_plan,
)
from .sewing_models import SaveSewingPlanRequest, SewingParams
from .sewing_svg import render_all_templates, render_signature_template
from .storage import PlanStore, SewingStore
from .svg import render_overview, render_sheet_side


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="印张拼版 API",
        version=__version__,
        description="供小批量书籍装订师核对折帖与配页的印张拼版服务",
    )
    db = db_path or os.environ.get("IMPOSITION_DB", "imposition.db")
    app.state.store = PlanStore(db)
    app.state.sewing_store = SewingStore(db)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        content = {"detail": exc.errors}
        if exc.payload:
            content.update(exc.payload)
        return JSONResponse(status_code=422, content=content)

    # ------------------------------------------------------------------
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/specs")
    def specs() -> dict:
        from .folding import GRIDS, STYLES

        return {
            "signature_pages": [8, 12, 16, 32],
            "grids": {str(k): {"cols": v[0], "rows": v[1]} for k, v in GRIDS.items()},
            "styles": {str(k): sorted(v) for k, v in STYLES.items()},
            "binding_edges": ["left", "right", "top", "bottom"],
            "units": "mm",
        }

    @app.post("/api/candidates")
    def candidates(job: JobInput) -> dict:
        """生成候选配页方案并按指标比较。"""
        cands = generate_candidates(job)
        return {
            "job_hash": job_fingerprint(job),
            "locked_pages": sum(s.pages for s in job.locked_signatures),
            "remaining_pages": job.total_pages
            - sum(s.pages for s in job.locked_signatures),
            "candidates": cands,
        }

    @app.post("/api/compute")
    def compute(req: ComputeRequest) -> dict:
        """计算完整拼版方案（不保存）。"""
        signatures = resolve_selection(req.job, req.candidate_index, req.signatures)
        return compute_plan(req.job, signatures)

    # ------------------------------------------------------------------
    @app.post("/api/plans", status_code=201)
    def save_plan(req: SavePlanRequest) -> dict:
        """将选定方案保存为不可变版本（同一输入重复保存幂等）。"""
        signatures = resolve_selection(req.job, req.candidate_index, req.signatures)
        result = compute_plan(req.job, signatures)
        svg = render_overview(result)
        record = app.state.store.save(
            name=req.name or req.job.job_name,
            job_hash=job_fingerprint(req.job),
            selection_hash=selection_fingerprint(signatures),
            input_json=canonical_json(job_dump(req.job)),
            selection_json=canonical_json(
                [
                    {"pages": s.pages, "style": s.style.value, "sheets": s.sheets}
                    for s in signatures
                ]
            ),
            result_json=canonical_json(result),
            svg=svg,
            metrics_json=canonical_json(result["totals"]),
        )
        record["metrics"] = json.loads(record["metrics"])
        return record

    @app.get("/api/plans")
    def list_plans() -> list[dict]:
        return app.state.store.list()

    def _load(plan_id: str):
        row = app.state.store.get(plan_id)
        if row is None:
            return None
        return row

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: str) -> dict:
        row = _load(plan_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "方案不存在"})
        return {
            "id": row["id"],
            "version": row["version"],
            "name": row["name"],
            "created_at": row["created_at"],
            "result": json.loads(row["result_json"]),
        }

    @app.get("/api/plans/{plan_id}/svg")
    def plan_svg(
        plan_id: str,
        sheet: int | None = Query(default=None),
        side: str = Query(default="front", pattern="^(front|back)$"),
    ) -> Response:
        """SVG 预览：默认整案；指定 sheet（全局印张序号）时输出单张单面。"""
        row = _load(plan_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "方案不存在"})
        if sheet is None:
            return Response(content=row["svg"], media_type="image/svg+xml")
        result = json.loads(row["result_json"])
        flat = [
            (si, sh)
            for si, sig in enumerate(result["signatures"])
            for sh in range(len(sig["sheets"]))
        ]
        if sheet < 0 or sheet >= len(flat):
            return JSONResponse(status_code=404, content={"detail": "印张序号超出范围"})
        si, sh = flat[sheet]
        svg = render_sheet_side(result, si, sh, side)
        return Response(content=svg, media_type="image/svg+xml")

    @app.get("/api/plans/{plan_id}/steps")
    def plan_steps(plan_id: str) -> dict:
        """逐步折叠裁切工序。"""
        row = _load(plan_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "方案不存在"})
        result = json.loads(row["result_json"])
        return {
            "id": row["id"],
            "signatures": [
                {
                    "index": sig["index"],
                    "spec": sig["spec"],
                    "folding": sig["folding"],
                }
                for sig in result["signatures"]
            ],
        }

    @app.get("/api/plans/{plan_id}/export")
    def plan_export(plan_id: str) -> dict:
        """制版用 JSON：每张印张正反面的页码、旋转与坐标（含配帖标）。"""
        row = _load(plan_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "方案不存在"})
        result = json.loads(row["result_json"])
        collating = result.get("collating_marks")
        plates = []
        for sig in result["signatures"]:
            for sheet in sig["sheets"]:
                for side in ("front", "back"):
                    plate = {
                        "signature": sig["index"],
                        "sheet": sheet["index"],
                        "side": side,
                        "creep_offset_mm": sheet["creep_offset_mm"],
                        "marks": sheet["marks"],
                        "pages": sheet[side]["cells"],
                    }
                    if collating is not None:
                        plate["collating_marks"] = [
                            m
                            for m in collating["marks"]
                            if m["signature"] == sig["index"]
                            and m["sheet"] == sheet["index"]
                            and m["side"] == side
                        ]
                    plates.append(plate)
        out = {
            "id": row["id"],
            "version": row["version"],
            "input_hash": result["input_hash"],
            "units": "mm",
            "coordinate_origin": "paper top-left, x right, y down; back side viewed from back",
            "plates": plates,
        }
        if collating is not None:
            out["collating_marks"] = {
                "spine_length_mm": collating["spine_length_mm"],
                "usable_range_mm": collating["usable_range_mm"],
                "spine_pattern": collating["spine_pattern"],
                "spine_check": collating["spine_check"],
            }
        return out

    @app.get("/api/plans/{plan_id}/verify")
    def plan_verify(plan_id: str) -> dict:
        """重算校验：同一输入与版本重复计算结果必须一致。"""
        row = _load(plan_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "方案不存在"})
        job = JobInput(**json.loads(row["input_json"]))
        from .models import SignatureSpec

        signatures = [SignatureSpec(**s) for s in json.loads(row["selection_json"])]
        recomputed = canonical_json(compute_plan(job, signatures))
        consistent = recomputed == row["result_json"]
        return {"id": row["id"], "version": row["version"], "consistent": consistent}

    # ------------------------------------------------------------------
    # 锁线装订方案：引用不可变拼版方案，搜索孔位与走线，冻结快照保存
    # ------------------------------------------------------------------
    def _source_snapshot(plan_id: str) -> dict:
        row = app.state.store.get(plan_id)
        if row is None:
            raise DomainError(
                [err("PLAN_NOT_FOUND", f"来源拼版方案 {plan_id} 不存在")]
            )
        return snapshot_from_plan(row)

    @app.post("/api/sewing-plans/candidates")
    def sewing_candidates_endpoint(params: SewingParams) -> dict:
        """搜索锁线孔位与走线候选，按违规数、换线次数、总线长、孔位改动排序。"""
        snapshot = _source_snapshot(params.plan_id)
        cands = sewing_candidates(snapshot, params)
        return {
            "input_hash": sewing_fingerprint(snapshot, params),
            "source": {
                "plan_id": snapshot["plan_id"],
                "plan_version": snapshot["plan_version"],
                "signature_count": len(snapshot["signatures"]),
                "spine_length_mm": cands[0]["spine_length_mm"],
            },
            "candidates": cands,
        }

    @app.post("/api/sewing-plans", status_code=201)
    def save_sewing_plan(req: SaveSewingPlanRequest) -> dict:
        """选定候选并保存为不可变锁线方案（冻结来源快照与参数，幂等）。"""
        params = req.to_params()
        snapshot = _source_snapshot(params.plan_id)
        cands = sewing_candidates(snapshot, params)
        if req.candidate_index >= len(cands):
            raise DomainError(
                [
                    err(
                        "BAD_CANDIDATE",
                        f"候选序号 {req.candidate_index} 超出范围（共 {len(cands)} 个）",
                    )
                ]
            )
        chosen = cands[req.candidate_index]
        record = app.state.sewing_store.save(
            name=req.name or f"锁线-{snapshot['plan_id']}",
            plan_id=snapshot["plan_id"],
            plan_version=snapshot["plan_version"],
            input_hash=sewing_fingerprint(snapshot, params),
            request_json=canonical_json(params.model_dump(mode="json")),
            snapshot_json=canonical_json(snapshot),
            result_json=canonical_json(chosen),
            metrics_json=canonical_json(chosen["metrics"]),
        )
        record["metrics"] = json.loads(record["metrics"])
        return record

    @app.get("/api/sewing-plans")
    def list_sewing_plans() -> list[dict]:
        return app.state.sewing_store.list()

    def _load_sewing(sew_id: str):
        return app.state.sewing_store.get(sew_id)

    @app.get("/api/sewing-plans/{sew_id}")
    def get_sewing_plan(sew_id: str) -> dict:
        row = _load_sewing(sew_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "锁线方案不存在"})
        return {
            "id": row["id"],
            "version": row["version"],
            "name": row["name"],
            "created_at": row["created_at"],
            "input_hash": row["input_hash"],
            "source": json.loads(row["snapshot_json"]),
            "result": json.loads(row["result_json"]),
        }

    @app.get("/api/sewing-plans/{sew_id}/svg")
    def sewing_plan_svg(
        sew_id: str,
        signature: int | None = Query(default=None),
    ) -> Response:
        """打孔模板 SVG：默认逐帖整案；指定 signature（帖序号）时输出单帖。"""
        row = _load_sewing(sew_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "锁线方案不存在"})
        result = json.loads(row["result_json"])
        if signature is None:
            return Response(
                content=render_all_templates(result), media_type="image/svg+xml"
            )
        if signature < 0 or signature >= len(result["signatures"]):
            return JSONResponse(status_code=404, content={"detail": "帖序号超出范围"})
        return Response(
            content=render_signature_template(result, signature),
            media_type="image/svg+xml",
        )

    @app.get("/api/sewing-plans/{sew_id}/steps")
    def sewing_plan_steps(sew_id: str) -> dict:
        """逐帖操作顺序：进针、出针、绕带、换帖与收线。"""
        row = _load_sewing(sew_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "锁线方案不存在"})
        result = json.loads(row["result_json"])
        return {
            "id": row["id"],
            "signatures": [
                {
                    "index": sig["index"],
                    "direction": sig["direction"],
                    "operations": sig["operations"],
                }
                for sig in result["signatures"]
            ],
            "thread_paths": result["thread_paths"],
            "unclosed_paths": result["unclosed_paths"],
        }

    @app.get("/api/sewing-plans/{sew_id}/verify")
    def sewing_plan_verify(sew_id: str) -> dict:
        """重算校验：按冻结的来源快照与参数重算，结果必须一致。"""
        row = _load_sewing(sew_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "锁线方案不存在"})
        params = SewingParams(**json.loads(row["request_json"]))
        snapshot = json.loads(row["snapshot_json"])
        cands = sewing_candidates(snapshot, params)
        consistent = any(
            canonical_json(c) == row["result_json"] for c in cands
        )
        return {"id": row["id"], "version": row["version"], "consistent": consistent}

    return app


app = create_app()
