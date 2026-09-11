"""配页规划：混合折帖规格生成候选方案，并按用纸面积、印张数、
折叠次数、空白页数量比较。

规则：
- 锁定帖为书首前缀，剩余页段重新组合；
- 候选为"最小覆盖"：去掉任何一帖都不足以覆盖剩余页数
  （因此空白页必然小于方案中最小的帖，落在末帖）；
- 帖序约定：锁定帖在前，其余按页数从大到小，空白页集中在最后的末帖。
"""
from __future__ import annotations

from functools import lru_cache

from .errors import DomainError, err
from .folding import GRIDS, build_program
from .imposition import check_spec, effective_printable
from .models import JobInput, SignatureSpec

MAX_CANDIDATES = 1000


def _folds_per_sheet(pages: int, style: str, binding: str) -> int:
    per_sheet_grid = GRIDS[pages]
    cols, rows = per_sheet_grid
    return len(build_program(cols, rows, binding, style))


@lru_cache(maxsize=256)
def _folds_cached(pages: int, style: str, binding: str) -> int:
    return _folds_per_sheet(pages, style, binding)


def spec_folds(spec: SignatureSpec, binding: str) -> int:
    """一帖的总折叠次数 = 每张折数 × 张数。"""
    per_sheet = spec.pages // spec.sheets
    return _folds_cached(per_sheet, spec.style.value, binding) * spec.sheets


def _minimal_covers(remaining: int, sizes: list[int]) -> list[list[int]]:
    """所有最小覆盖（多重集）：总页数 ≥ remaining 且去掉任一帖即不足。"""
    sizes = sorted(set(sizes))
    results: list[list[int]] = []

    def rec(i: int, rem: int, chosen: list[int]) -> None:
        if len(results) > MAX_CANDIDATES * 4:
            return
        if rem <= 0:
            results.append(list(chosen))
            return
        if i == len(sizes):
            return
        p = sizes[i]
        for k in range(rem // p + 2):
            rec(i + 1, rem - k * p, chosen + [p] * k)

    rec(0, remaining, [])
    return [
        c for c in results if c and sum(c) - remaining < min(c)
    ][:MAX_CANDIDATES]


def plan_metrics(job: JobInput, signatures: list[SignatureSpec]) -> dict:
    """方案指标：用纸面积、印张数、折叠次数、空白页数。"""
    sheet_count = sum(s.sheets for s in signatures)
    fold_count = sum(spec_folds(s, job.binding.value) for s in signatures)
    total_sig_pages = sum(s.pages for s in signatures)
    return {
        "paper_area_mm2": round(sheet_count * job.paper.width * job.paper.height, 1),
        "sheet_count": sheet_count,
        "fold_count": fold_count,
        "blank_pages": total_sig_pages - job.total_pages,
        "signature_count": len(signatures),
    }


def _order_planned(planned: list[SignatureSpec]) -> list[SignatureSpec]:
    """计划内折帖排序：页数从大到小（空白页落在最小的末帖）。"""
    return sorted(planned, key=lambda s: (-s.pages, s.sheets, s.style.value))


def generate_candidates(job: JobInput) -> list[dict]:
    """生成候选方案列表（含指标与可行性），按推荐度排序。"""
    locked = list(job.locked_signatures)
    locked_pages = sum(s.pages for s in locked)
    remaining = job.total_pages - locked_pages
    if remaining < 0:
        raise DomainError(
            [err("LOCKED_EXCEEDS_TOTAL", "锁定折帖页数超过总页数")]
        )

    by_pages: dict[int, SignatureSpec] = {}
    for opt in job.signature_options:
        by_pages.setdefault(opt.pages, opt)
    combos: list[list[SignatureSpec]] = []
    if remaining == 0:
        combos = [[]]
    else:
        for cover in _minimal_covers(remaining, list(by_pages)):
            combos.append([by_pages[p] for p in cover])

    printable = effective_printable(job)
    candidates = []
    for planned in combos:
        signatures = locked + _order_planned(planned)
        errors: list[dict] = []
        seen_msg = set()
        for spec in signatures:
            for e in check_spec(job, spec):
                if e["message"] not in seen_msg:
                    seen_msg.add(e["message"])
                    errors.append(e)
        candidates.append(
            {
                "signatures": [
                    {"pages": s.pages, "style": s.style.value, "sheets": s.sheets}
                    for s in signatures
                ],
                "metrics": plan_metrics(job, signatures),
                "valid": not errors,
                "errors": errors,
            }
        )

    def sort_key(c: dict):
        m = c["metrics"]
        canonical = ",".join(
            f"{s['pages']}{s['style'][0]}x{s['sheets']}" for s in c["signatures"]
        )
        return (
            0 if c["valid"] else 1,
            m["blank_pages"],
            m["paper_area_mm2"],
            m["sheet_count"],
            m["fold_count"],
            canonical,
        )

    candidates.sort(key=sort_key)
    for i, c in enumerate(candidates):
        c["index"] = i
        c["recommended"] = i == 0 and c["valid"]
    return candidates
