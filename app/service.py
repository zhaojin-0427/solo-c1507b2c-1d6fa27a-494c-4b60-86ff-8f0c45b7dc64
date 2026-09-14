"""编排层：候选解析、整案计算、确定性指纹。"""
from __future__ import annotations

import hashlib
import json

from .errors import DomainError, err
from .collating import compute_collating
from .imposition import (
    build_signature,
    check_spec,
    effective_printable,
    validate_plan_pages,
)
from . import presswork
from .models import JobInput, SignatureSpec
from .planner import generate_candidates, plan_metrics


def canonical_json(obj) -> str:
    """确定性 JSON（排序键、紧凑分隔符），用于指纹与存储。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def job_dump(job: JobInput) -> dict:
    """任务序列化：未配置项剔除对应键，保持旧响应与哈希不变。

    - 未配置配帖标时剔除 collating_marks；
    - 未启用翻身版/天地翻时剔除全部过版字段（presswork_mode 等）。
    """
    d = job.model_dump(mode="json")
    if d.get("collating_marks") is None:
        d.pop("collating_marks", None)
    if not job.presswork_active():
        for k in ("presswork_mode", "target_copies", "side_lay_edge",
                  "gutter_trim_mm"):
            d.pop(k, None)
    return d


def job_fingerprint(job: JobInput) -> str:
    return hashlib.sha256(canonical_json(job_dump(job)).encode()).hexdigest()


def selection_fingerprint(signatures: list[SignatureSpec]) -> str:
    sel = [
        {"pages": s.pages, "style": s.style.value, "sheets": s.sheets}
        for s in signatures
    ]
    return hashlib.sha256(canonical_json(sel).encode()).hexdigest()


def resolve_selection(
    job: JobInput,
    candidate_index: int | None,
    signatures: list[SignatureSpec] | None,
) -> list[SignatureSpec]:
    """解析用户选择：候选序号或显式折帖序列（缺省为推荐候选）。"""
    if signatures is not None:
        if not signatures:
            raise DomainError([err("EMPTY_SELECTION", "折帖序列不能为空")])
        total = sum(s.pages for s in signatures)
        if total < job.total_pages:
            raise DomainError(
                [
                    err(
                        "INSUFFICIENT_PAGES",
                        f"所选折帖共 {total} 页，不足以覆盖总页数 {job.total_pages}",
                    )
                ]
            )
        locked = list(job.locked_signatures)
        if signatures[: len(locked)] != locked:
            raise DomainError(
                [err("LOCKED_MISMATCH", "折帖序列必须以锁定帖为前缀")]
            )
        return list(signatures)
    candidates = generate_candidates(job)
    idx = candidate_index if candidate_index is not None else 0
    if idx < 0 or idx >= len(candidates):
        raise DomainError(
            [err("BAD_CANDIDATE", f"候选序号 {idx} 超出范围（共 {len(candidates)} 个）")]
        )
    chosen = candidates[idx]
    if not chosen["valid"]:
        # 候选无效（含配帖标冲突）：422 响应一并携带阶梯图案与首个冲突
        payload = (
            {"collating_marks": chosen["collating"]} if "collating" in chosen else None
        )
        raise DomainError(chosen["errors"], payload=payload)
    return [SignatureSpec(**s) for s in chosen["signatures"]]


def presswork_summary(job: JobInput, signatures_out: list[dict]) -> dict:
    """汇总翻身版/天地翻的过版信息：印版数、过版次数、用纸量、成品帖数、超印。

    每张全张一块共用印版、两次过版，沿中缝出两份书帖。目标册数为奇数时
    末批只需要一份，另一份为超印量（overrun_copies 列出）。
    """
    plates = []
    sheet_count = 0
    for sig in signatures_out:
        for sheet in sig["sheets"]:
            pw = sheet["presswork"]
            sheet_count += 1
            plate_id = f"S{sig['index'] + 1}-P{sheet['index'] + 1}"
            pw["plate_id"] = plate_id  # 回填共用印版号到逐张块
            plates.append(
                {
                    "plate_id": plate_id,
                    "signature": sig["index"],
                    "sheet": sheet["index"],
                    "mode": pw["mode"],
                    "turn_axis": pw["turn_axis"],
                    "shared_plate": True,
                    "flip_matrix": pw["flip_matrix"],
                    "cut_line": pw["cut_line"],
                    "passes": pw["passes"],
                }
            )
    target = job.target_copies if job.target_copies is not None else 1
    # 每张全张经两次过版出 2 份书帖；一次开印即整批过版（共 sheet_count 张全张）。
    # 目标为奇数时末批超印 1 份（overrun_copies 列出）。
    batches = (target + 1) // 2
    paper_sheets = batches * sheet_count
    printed_copies = batches * 2
    overrun = printed_copies - target
    # 成品帖数：每个书帖位置产出 printed_copies 份成品书帖
    signature_kinds = len(signatures_out)
    finished_signatures = min(printed_copies, target) * signature_kinds
    return {
        "mode": job.presswork_mode.value,
        "turn_axis": "vertical" if presswork.turn_axis(job) == "V" else "horizontal",
        "side_lay_edge_pass1": job.effective_side_lay(),
        "side_lay_edge_pass2": (
            job.effective_side_lay()
            if job.presswork_mode.value == "work_and_tumble"
            else {
                "left": "right", "right": "left",
                "top": "bottom", "bottom": "top",
            }[job.effective_side_lay()]
        ),
        "target_copies": target,
        "plate_count": sheet_count,
        "pass_count": paper_sheets * 2,
        "paper_sheets": paper_sheets,
        "paper_area_mm2": round(
            paper_sheets * job.paper.width * job.paper.height, 1
        ),
        "finished_signatures": finished_signatures,
        "printed_copies": min(printed_copies, target) if overrun == 0 else target,
        "overrun_copies": (
            [{"target": target, "printed": printed_copies, "extra": overrun}]
            if overrun else []
        ),
        "plates": plates,
    }


def compute_plan(job: JobInput, signatures: list[SignatureSpec]) -> dict:
    """计算完整拼版方案。任何拒绝条件（越界/倒页/重页/缺页/纸纹）抛 DomainError。"""
    printable = effective_printable(job)

    # 静态检查：越出可印区域 / 纸纹不合（显式选择的序列同样校验）
    spec_errors: list[dict] = []
    seen_msg = set()
    for spec in signatures:
        for e in check_spec(job, spec):
            if e["message"] not in seen_msg:
                seen_msg.add(e["message"])
                spec_errors.append(e)
    if spec_errors:
        raise DomainError(spec_errors)

    signatures_out = []
    cursor = 0
    for i, spec in enumerate(signatures):
        sig = build_signature(job, spec, i, cursor, printable)
        signatures_out.append(sig)
        cursor += spec.pages

    page_errors = validate_plan_pages(job, signatures_out, job.total_pages)
    if page_errors:
        raise DomainError(page_errors)

    # 书脊配帖标：冲突（越出书脊/侵入安全区/重叠/碰套准标）定位帖号并拒绝，
    # 422 响应一并携带阶梯图案、每帖位置与首个冲突
    collating = None
    if job.collating_marks is not None:
        collating, coll_errors = compute_collating(job, signatures, printable)
        if coll_errors:
            raise DomainError(coll_errors, payload={"collating_marks": collating})

    warnings = [w for sig in signatures_out for w in sig["warnings"]]
    metrics = plan_metrics(job, signatures)
    result = {
        "job": job_dump(job),
        "signatures": signatures_out,
        "totals": metrics,
        "validation": {"ok": True, "errors": [], "warnings": warnings},
    }
    if collating is not None:
        result["collating_marks"] = collating
    if job.presswork_active():
        result["presswork"] = presswork_summary(job, signatures_out)
    result["input_hash"] = hashlib.sha256(
        (
            job_fingerprint(job) + ":" + selection_fingerprint(signatures)
        ).encode()
    ).hexdigest()
    return result
