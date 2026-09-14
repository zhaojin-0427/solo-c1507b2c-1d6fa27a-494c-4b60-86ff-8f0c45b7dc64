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
from .models import JobInput, SignatureSpec
from .planner import generate_candidates, plan_metrics


def canonical_json(obj) -> str:
    """确定性 JSON（排序键、紧凑分隔符），用于指纹与存储。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def job_fingerprint(job: JobInput) -> str:
    return hashlib.sha256(canonical_json(job.model_dump(mode="json")).encode()).hexdigest()


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
        raise DomainError(chosen["errors"])
    return [SignatureSpec(**s) for s in chosen["signatures"]]


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

    page_errors = validate_plan_pages(signatures_out, job.total_pages)
    if page_errors:
        raise DomainError(page_errors)

    # 书脊配帖标：冲突（越出书脊/侵入安全区/重叠/碰套准标）定位帖号并拒绝
    collating = None
    if job.collating_marks is not None:
        collating, coll_errors = compute_collating(job, signatures, printable)
        if coll_errors:
            raise DomainError(coll_errors)

    warnings = [w for sig in signatures_out for w in sig["warnings"]]
    metrics = plan_metrics(job, signatures)
    result = {
        "job": job.model_dump(mode="json"),
        "signatures": signatures_out,
        "totals": metrics,
        "validation": {"ok": True, "errors": [], "warnings": warnings},
    }
    if collating is not None:
        result["collating_marks"] = collating
    result["input_hash"] = hashlib.sha256(
        (
            job_fingerprint(job) + ":" + selection_fingerprint(signatures)
        ).encode()
    ).hexdigest()
    return result
