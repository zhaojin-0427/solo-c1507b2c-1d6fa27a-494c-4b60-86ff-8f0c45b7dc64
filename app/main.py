"""FastAPI 应用：印张拼版 API。"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .errors import DomainError
from .models import ComputeRequest, JobInput, SavePlanRequest
from .planner import generate_candidates
from .service import (
    canonical_json,
    compute_plan,
    job_fingerprint,
    resolve_selection,
    selection_fingerprint,
)
from .storage import PlanStore
from .svg import render_overview, render_sheet_side


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="印张拼版 API",
        version=__version__,
        description="供小批量书籍装订师核对折帖与配页的印张拼版服务",
    )
    app.state.store = PlanStore(db_path or os.environ.get("IMPOSITION_DB", "imposition.db"))

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors})

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
            input_json=canonical_json(req.job.model_dump(mode="json")),
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
        """制版用 JSON：每张印张正反面的页码、旋转与坐标。"""
        row = _load(plan_id)
        if row is None:
            return JSONResponse(status_code=404, content={"detail": "方案不存在"})
        result = json.loads(row["result_json"])
        plates = []
        for sig in result["signatures"]:
            for sheet in sig["sheets"]:
                for side in ("front", "back"):
                    plates.append(
                        {
                            "signature": sig["index"],
                            "sheet": sheet["index"],
                            "side": side,
                            "creep_offset_mm": sheet["creep_offset_mm"],
                            "marks": sheet["marks"],
                            "pages": sheet[side]["cells"],
                        }
                    )
        return {
            "id": row["id"],
            "version": row["version"],
            "input_hash": result["input_hash"],
            "units": "mm",
            "coordinate_origin": "paper top-left, x right, y down; back side viewed from back",
            "plates": plates,
        }

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

    return app


app = create_app()
