"""锁线装订：孔位搜索与走线计算。

模型约定：
- 全书共用一份基础孔位（同一打孔模板）。系统在必留孔（各帖锁定孔与
  支撑带锚孔）约束下搜索未锁定孔位，使孔位相对均匀理想网格的改动最小；
- 每帖有效孔 = 基础孔位 − 禁打区内孔 − 破损孔；
- 走线方向逐帖交替（头→尾、尾→头），帖内进针/出针交替；
  帖间边界孔重合时以锁链结闭合连接，错位时收线换线（形成未闭合路径）；
- 线段 = 相邻两孔之间（或锁链结/起针收线留头）的一段线；
  单段长度超过 max_segment_length_mm 计为违规；
- 绕带式：支撑带两锚孔为相邻有效孔时该帖绕带一次，线长附加 2×带宽；
  锚孔被跳过（禁打区/破损）导致未绕带，计为违规。

候选按 (违规数, 换线次数, 总线长, 孔位改动, 孔数, 孔位) 排序。
"""
from __future__ import annotations

import hashlib
import json

from .errors import DomainError, err
from .imposition import r3
from .service import canonical_json
from .sewing_models import SewingParams

POS_TOL = 0.5  # 孔位匹配容差 mm
GRID_STEP = 0.5  # 孔位微调步长 mm
KETTLE_EXTRA_MM = 8.0  # 锁链结附加线长 mm
TIE_EXTRA_MM = 30.0  # 起针/收线留头附加线长 mm
MAX_CANDIDATES = 20  # 候选方案上限
EPS = 1e-9


# ---------------------------------------------------------------------------
# 来源快照与指纹
# ---------------------------------------------------------------------------


def snapshot_from_plan(row) -> dict:
    """从拼版方案记录冻结锁线来源快照（后续拼版版本不影响已冻结快照）。"""
    result = json.loads(row["result_json"])
    return {
        "plan_id": row["id"],
        "plan_version": row["version"],
        "result_hash": hashlib.sha256(row["result_json"].encode()).hexdigest(),
        "job": result["job"],
        "signatures": [
            {
                "index": s["index"],
                "page_start": s["page_start"],
                "page_end": s["page_end"],
                "pages": s["spec"]["pages"],
            }
            for s in result["signatures"]
        ],
    }


def spine_length(job: dict) -> float:
    """书脊长度：左右装订取成品页高，上下装订取成品页宽。"""
    if job["binding"] in ("left", "right"):
        return job["page"]["height"]
    return job["page"]["width"]


def sewing_fingerprint(snapshot: dict, params: SewingParams) -> str:
    """输入哈希：来源快照标识 + 锁线参数。"""
    payload = {
        "source": {
            "plan_id": snapshot["plan_id"],
            "plan_version": snapshot["plan_version"],
            "result_hash": snapshot["result_hash"],
        },
        "params": params.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


# ---------------------------------------------------------------------------
# 公共校验（拒绝创建条件）
# ---------------------------------------------------------------------------


def _validate(snapshot: dict, params: SewingParams):
    """校验来源与参数。返回 (书脊长, 可用头, 可用尾, 逐帖标记, 支撑带, 锚孔, 必留孔, 孔数上限)。"""
    errors = []
    job = snapshot["job"]
    spine = r3(spine_length(job))
    head = r3(params.head_margin_mm)
    tail = r3(spine - params.tail_margin_mm)
    if tail - head <= 0:
        raise DomainError(
            [err("MARGIN_EXCEEDS_SPINE", f"头尾留量之和超出书脊长度 {spine}mm")]
        )

    if params.stitch.value == "chain" and params.tapes:
        errors.append(
            err("TAPES_NOT_ALLOWED", "链式缝法不使用支撑带，请移除 tapes 或改用绕带式")
        )
    if params.stitch.value == "tape" and not params.tapes:
        errors.append(err("TAPES_REQUIRED", "绕带式缝法至少需要一条支撑带"))

    n_sig = len(snapshot["signatures"])
    overrides = {o.index: o for o in params.signatures}
    for o in params.signatures:
        if o.index >= n_sig:
            errors.append(
                err(
                    "SIGNATURE_INDEX_OUT_OF_RANGE",
                    f"帖序号 {o.index} 超出范围（共 {n_sig} 帖）",
                )
            )
        for z in o.forbidden_zones:
            if z.end_mm > spine + EPS:
                errors.append(
                    err(
                        "FORBIDDEN_ZONE_OUT_OF_BOUNDS",
                        f"帖{o.index + 1} 禁打区 [{z.start_mm}, {z.end_mm}] "
                        f"超出书脊长度 {spine}mm",
                    )
                )

    tapes = sorted(params.tapes, key=lambda t: (t.position_mm, t.width_mm))
    anchors = [
        (r3(t.position_mm - t.width_mm / 2), r3(t.position_mm + t.width_mm / 2))
        for t in tapes
    ]
    for i, (a1, a2) in enumerate(anchors):
        if a1 < -EPS or a2 > spine + EPS:
            errors.append(
                err(
                    "TAPE_OUT_OF_BOUNDS",
                    f"支撑带{i + 1} [{a1}, {a2}] 超出书脊范围 [0, {spine}]",
                )
            )
        for j in range(i + 1, len(anchors)):
            b1, b2 = anchors[j]
            if a1 < b2 - EPS and b1 < a2 - EPS:
                errors.append(
                    err("TAPE_OVERLAP", f"支撑带{i + 1} 与支撑带{j + 1} 重叠")
                )

    # 必留孔 = 各帖锁定孔 ∪ 支撑带锚孔
    required: set[float] = set()
    for a1, a2 in anchors:
        for a in (a1, a2):
            if a < head - EPS or a > tail + EPS:
                errors.append(
                    err(
                        "HOLE_OUT_OF_BOUNDS",
                        f"支撑带锚孔 {a}mm 越出可用范围 [{head}, {tail}]mm",
                    )
                )
            required.add(a)
    for o in params.signatures:
        for p in o.locked_holes_mm:
            p = r3(p)
            if p < head - EPS or p > tail + EPS:
                errors.append(
                    err(
                        "HOLE_OUT_OF_BOUNDS",
                        f"帖{o.index + 1} 锁定孔 {p}mm 越出可用范围 [{head}, {tail}]mm",
                    )
                )
            if any(z.start_mm - EPS <= p <= z.end_mm + EPS for z in o.forbidden_zones):
                errors.append(
                    err(
                        "HOLE_IN_FORBIDDEN_ZONE",
                        f"帖{o.index + 1} 锁定孔 {p}mm 落入禁打区",
                    )
                )
            if any(abs(p - d) <= POS_TOL for d in o.damaged_holes_mm):
                errors.append(
                    err(
                        "LOCKED_DAMAGED_CONFLICT",
                        f"帖{o.index + 1} 孔位 {p}mm 既锁定又标为破损",
                    )
                )
            required.add(p)
        for d in o.damaged_holes_mm:
            if d < -EPS or d > spine + EPS:
                errors.append(
                    err(
                        "HOLE_OUT_OF_BOUNDS",
                        f"帖{o.index + 1} 破损孔 {r3(d)}mm 超出书脊范围 [0, {spine}]",
                    )
                )

    req_sorted = sorted(required)
    for a, b in zip(req_sorted, req_sorted[1:]):
        if b - a < params.min_spacing_mm - EPS:
            errors.append(
                err(
                    "HOLE_SPACING",
                    f"必留孔 {a}mm 与 {b}mm 的间距小于最小孔距 {params.min_spacing_mm}mm",
                )
            )

    span = tail - head
    fit_max = int(span / params.min_spacing_mm) + 1
    eff_max = min(params.hole_count.max, fit_max)
    if eff_max < params.hole_count.min:
        errors.append(
            err(
                "HOLE_COUNT_INFEASIBLE",
                f"可用范围 {r3(span)}mm 按最小孔距 {params.min_spacing_mm}mm "
                f"最多布置 {fit_max} 孔，少于孔数下限 {params.hole_count.min}",
            )
        )
    elif len(req_sorted) > eff_max:
        errors.append(
            err(
                "HOLE_COUNT_INFEASIBLE",
                f"必留孔 {len(req_sorted)} 个超过可布置上限 {eff_max}",
            )
        )
    if errors:
        raise DomainError(errors)
    return spine, head, tail, overrides, tapes, anchors, req_sorted, eff_max


# ---------------------------------------------------------------------------
# 孔位布局搜索
# ---------------------------------------------------------------------------


def _nudge(
    g: float,
    chosen: list[float],
    min_spacing: float,
    head: float,
    tail: float,
    avoid_intervals: list[tuple],
    avoid_points: list[float],
) -> float | None:
    """为自由孔在 g 附近找可行位置：满足边界与孔距，尽量避开禁打区/破损孔。

    避让为软约束：两轮扫描，第一轮避开避让区，第二轮只守边界与孔距。
    """

    def ok(x: float, use_avoid: bool) -> bool:
        if x < head - EPS or x > tail + EPS:
            return False
        for c in chosen:
            if abs(x - c) < min_spacing - EPS:
                return False
        if use_avoid:
            for a, b in avoid_intervals:
                if a - EPS <= x <= b + EPS:
                    return False
            for d in avoid_points:
                if abs(x - d) <= POS_TOL:
                    return False
        return True

    span = tail - head
    for use_avoid in (True, False):
        if ok(g, use_avoid):
            return r3(g)
        k = 1
        while k * GRID_STEP <= span + GRID_STEP:
            for x in (g - k * GRID_STEP, g + k * GRID_STEP):
                if ok(x, use_avoid):
                    return r3(x)
            k += 1
    return None


def _layout_for_count(
    n: int,
    required: list[float],
    head: float,
    tail: float,
    min_spacing: float,
    avoid_intervals: list[tuple],
    avoid_points: list[float],
) -> tuple[list[float], float] | None:
    """布置 n 个孔：包含全部必留孔，自由孔贴近均匀理想网格。

    返回 (孔位列表, 孔位改动量 mm)；不可行返回 None。
    孔位改动 = 各孔与理想网格对应点距离之和。
    """
    span = tail - head
    grid = [r3(head + span * i / (n - 1)) for i in range(n)]
    m = len(required)
    if m > n:
        return None
    # 必留孔与网格点的保序最优匹配（动态规划）
    INF = float("inf")
    dp = [[INF] * (n + 1) for _ in range(m + 1)]
    for j in range(n + 1):
        dp[0][j] = 0.0
    for i in range(1, m + 1):
        for j in range(i, n + 1):
            skip = dp[i][j - 1]
            use = dp[i - 1][j - 1] + abs(required[i - 1] - grid[j - 1])
            dp[i][j] = skip if skip <= use else use
    if dp[m][n] == INF:
        return None
    matched: set[int] = set()
    i, j = m, n
    while i > 0:
        use = dp[i - 1][j - 1] + abs(required[i - 1] - grid[j - 1])
        if dp[i][j] == use:
            matched.add(j - 1)
            i -= 1
        j -= 1
    chosen = list(required)
    for k, g in enumerate(grid):
        if k in matched:
            continue
        pos = _nudge(g, chosen, min_spacing, head, tail, avoid_intervals, avoid_points)
        if pos is None:
            return None
        chosen.append(pos)
    chosen = sorted(chosen)
    for a, b in zip(chosen, chosen[1:]):
        if b - a < min_spacing - EPS:
            return None
    deviation = r3(sum(abs(c - g) for c, g in zip(chosen, grid)))
    return [r3(c) for c in chosen], deviation


# ---------------------------------------------------------------------------
# 走线计算
# ---------------------------------------------------------------------------


def _route(
    snapshot: dict,
    params: SewingParams,
    holes: list[float],
    deviation: float,
    tapes: list,
    anchors: list[tuple],
    overrides: dict,
    spine: float,
    head: float,
    tail: float,
) -> dict:
    """对给定基础孔位计算全书走线：逐帖操作、线段、线路与指标。"""
    n_sig = len(snapshot["signatures"])

    # 第一遍：各帖有效孔与跳过孔
    effs: list[list[float]] = []
    skips: list[list[dict]] = []
    warnings: list[str] = []
    for i in range(n_sig):
        o = overrides.get(i)
        eff, skipped = [], []
        for h in holes:
            if o and any(
                z.start_mm - EPS <= h <= z.end_mm + EPS for z in o.forbidden_zones
            ):
                skipped.append({"position_mm": h, "reason": "forbidden_zone"})
            elif o and any(abs(h - d) <= POS_TOL for d in o.damaged_holes_mm):
                skipped.append({"position_mm": h, "reason": "damaged"})
            else:
                eff.append(h)
        if len(eff) < 2:
            raise DomainError(
                [
                    err(
                        "SIGNATURE_HOLES_INSUFFICIENT",
                        f"帖{i + 1} 有效孔不足 2 个，无法走线",
                    )
                ]
            )
        if o:
            for d in o.damaged_holes_mm:
                if not any(abs(h - d) <= POS_TOL for h in holes):
                    warnings.append(f"帖{i + 1} 破损孔 {r3(d)}mm 未命中任何孔位")
        effs.append(eff)
        skips.append(skipped)

    # 第二遍：走线（方向逐帖交替，帖内进出交替）
    segments: list[dict] = []
    paths: list[dict] = []
    unclosed: list[int] = []
    violations = 0
    violation_details: list[dict] = []
    cum = 0.0
    seq = 1
    piece_index = -1
    piece: dict | None = None
    piece_len = 0.0
    sig_out: list[dict] = []

    def add_segment(piece_idx, sig_i, kind, side, a, b, length, exceeds=False,
                    wrap_tape=None, note=None):
        nonlocal cum
        cum = r3(cum + length)
        seg = {
            "piece": piece_idx,
            "signature": sig_i,
            "kind": kind,
            "side": side,
            "from_mm": r3(a),
            "to_mm": r3(b),
            "length_mm": r3(length),
            "cumulative_mm": cum,
            "exceeds_max": exceeds,
        }
        if wrap_tape is not None:
            seg["wrap_tape"] = wrap_tape
        if note:
            seg["note"] = note
        segments.append(seg)

    P: list[float] = []
    for i in range(n_sig):
        sinfo = snapshot["signatures"][i]
        o = overrides.get(i)
        eff = effs[i]
        direction = "head_to_tail" if i % 2 == 0 else "tail_to_head"
        P = eff if i % 2 == 0 else eff[::-1]
        ops: list[dict] = []
        if piece is None:
            piece_index += 1
            piece = {"index": piece_index,
                     "start": {"signature": i, "hole_mm": P[0]}}
            piece_len = 0.0
            add_segment(piece_index, i, "tie", "outside", P[0], P[0],
                        TIE_EXTRA_MM, note="起针留头")
            piece_len += TIE_EXTRA_MM
            ops.append({"seq": seq, "type": "needle_in", "action": "进针",
                        "signature": i, "hole_mm": P[0], "note": "起针（新线）"})
        else:
            ops.append({"seq": seq, "type": "needle_in", "action": "进针",
                        "signature": i, "hole_mm": P[0]})
        seq += 1
        side = "inside"
        wrapped: set[int] = set()
        for j in range(1, len(P)):
            prev, cur = P[j - 1], P[j]
            run = abs(cur - prev)
            wrap = None
            for ti, (a1, a2) in enumerate(anchors):
                if (abs(prev - a1) <= POS_TOL and abs(cur - a2) <= POS_TOL) or (
                    abs(prev - a2) <= POS_TOL and abs(cur - a1) <= POS_TOL
                ):
                    wrap = ti
                    break
            if wrap is not None:
                wrapped.add(wrap)
                ops.append({"seq": seq, "type": "wrap_tape", "action": "绕带",
                            "signature": i, "tape": wrap,
                            "at_mm": tapes[wrap].position_mm})
                seq += 1
                run += 2 * tapes[wrap].width_mm
            exceeds = run > params.max_segment_length_mm + EPS
            if exceeds:
                violations += 1
                violation_details.append(
                    {"type": "segment_too_long", "signature": i,
                     "from_mm": prev, "to_mm": cur, "length_mm": r3(run),
                     "max_mm": params.max_segment_length_mm}
                )
            add_segment(piece_index, i, "wrap" if wrap is not None else "run",
                        side, prev, cur, run, exceeds=exceeds, wrap_tape=wrap)
            piece_len += run
            if side == "inside":
                ops.append({"seq": seq, "type": "needle_out", "action": "出针",
                            "signature": i, "hole_mm": cur})
                side = "outside"
            else:
                ops.append({"seq": seq, "type": "needle_in", "action": "进针",
                            "signature": i, "hole_mm": cur})
                side = "inside"
            seq += 1
        if len(P) % 2 == 1:
            # 奇数孔：末孔同孔回针，把线带出帖外
            ops.append({"seq": seq, "type": "needle_out", "action": "出针",
                        "signature": i, "hole_mm": P[-1], "note": "同孔回针"})
            seq += 1
        for ti in range(len(anchors)):
            if ti not in wrapped:
                violations += 1
                violation_details.append(
                    {"type": "tape_not_wrapped", "signature": i, "tape": ti}
                )
                warnings.append(f"帖{i + 1} 未绕支撑带{ti + 1}")
        # 帖间连接：边界孔重合则锁链结闭合，错位则收线换线
        if i < n_sig - 1:
            b_cur = P[-1]
            nxt = effs[i + 1]
            b_next = nxt[0] if (i + 1) % 2 == 0 else nxt[-1]
            if not any(
                abs(h1 - h2) <= POS_TOL for h1 in eff for h2 in nxt
            ):
                raise DomainError(
                    [err("NO_CONNECTABLE_HOLE", f"帖{i + 1} 与帖{i + 2} 没有可连接孔")]
                )
            if abs(b_cur - b_next) <= POS_TOL:
                add_segment(piece_index, i, "kettle", "outside", b_cur, b_next,
                            KETTLE_EXTRA_MM, note="锁链结")
                piece_len += KETTLE_EXTRA_MM
                ops.append({"seq": seq, "type": "change_signature",
                            "action": "换帖", "signature": i, "to_signature": i + 1,
                            "hole_mm": b_cur, "closed": True, "note": "锁链结连接"})
            else:
                add_segment(piece_index, i, "tie", "outside", b_cur, b_cur,
                            TIE_EXTRA_MM, note="收线留尾")
                piece_len += TIE_EXTRA_MM
                ops.append({"seq": seq, "type": "finish", "action": "收线",
                            "signature": i, "hole_mm": b_cur,
                            "note": "边界孔错位，收线打结"})
                seq += 1
                piece["end"] = {"signature": i, "hole_mm": b_cur}
                piece["length_mm"] = r3(piece_len)
                piece["closed"] = False
                paths.append(piece)
                unclosed.append(piece_index)
                piece = None
                ops.append({"seq": seq, "type": "change_signature",
                            "action": "换帖", "signature": i, "to_signature": i + 1,
                            "hole_mm": b_next, "closed": False, "note": "换线"})
            seq += 1
        sig_out.append(
            {
                "index": i,
                "page_start": sinfo["page_start"],
                "page_end": sinfo["page_end"],
                "direction": direction,
                "effective_holes_mm": eff,
                "skipped_holes_mm": skips[i],
                "locked_holes_mm": sorted(r3(p) for p in (o.locked_holes_mm if o else [])),
                "forbidden_zones_mm": [
                    {"start_mm": z.start_mm, "end_mm": z.end_mm}
                    for z in (o.forbidden_zones if o else [])
                ],
                "operations": ops,
            }
        )

    # 末帖收线
    add_segment(piece_index, n_sig - 1, "tie", "outside", P[-1], P[-1],
                TIE_EXTRA_MM, note="收线留尾")
    piece_len += TIE_EXTRA_MM
    sig_out[-1]["operations"].append(
        {"seq": seq, "type": "finish", "action": "收线", "signature": n_sig - 1,
         "hole_mm": P[-1], "note": "末帖收线打结"}
    )
    piece["end"] = {"signature": n_sig - 1, "hole_mm": P[-1]}
    piece["length_mm"] = r3(piece_len)
    piece["closed"] = True
    paths.append(piece)

    return {
        "index": 0,  # 占位，候选排序后重排
        "stitch": params.stitch.value,
        "spine_length_mm": spine,
        "usable_range_mm": [head, tail],
        "holes_mm": list(holes),
        "hole_count": len(holes),
        "tapes": [
            {"index": ti, "position_mm": t.position_mm, "width_mm": t.width_mm,
             "anchors_mm": [a1, a2]}
            for ti, (t, (a1, a2)) in enumerate(zip(tapes, anchors))
        ],
        "signatures": sig_out,
        "segments": segments,
        "thread_paths": paths,
        "unclosed_paths": unclosed,
        "metrics": {
            "violations": violations,
            "violation_details": violation_details,
            "thread_changes": len(unclosed),
            "total_thread_mm": cum,
            "hole_deviation_mm": deviation,
            "hole_count": len(holes),
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 候选搜索
# ---------------------------------------------------------------------------


def compute_candidates(snapshot: dict, params: SewingParams) -> list[dict]:
    """搜索未锁定孔位与走线，返回排序后的候选方案列表。

    排序键：违规数、换线次数、总线长、孔位改动、孔数、孔位。
    所有候选均不可行时抛出 DomainError 拒绝创建。
    """
    (spine, head, tail, overrides, tapes, anchors, required, eff_max) = _validate(
        snapshot, params
    )

    # 软避让：禁打区与支撑带内部（自由孔尽量避开），破损孔位置
    avoid_intervals = list(anchors)
    avoid_points: list[float] = []
    for o in params.signatures:
        for z in o.forbidden_zones:
            avoid_intervals.append((z.start_mm, z.end_mm))
        avoid_points.extend(o.damaged_holes_mm)

    candidates: list[dict] = []
    failures: list[dict] = []
    lo = max(params.hole_count.min, len(required))
    for n in range(lo, eff_max + 1):
        lay = _layout_for_count(
            n, required, head, tail, params.min_spacing_mm,
            avoid_intervals, avoid_points,
        )
        if lay is None:
            continue
        holes, deviation = lay
        try:
            candidates.append(
                _route(snapshot, params, holes, deviation, tapes, anchors,
                       overrides, spine, head, tail)
            )
        except DomainError as e:
            failures.extend(e.errors)
    if not candidates:
        if failures:
            seen, errs = set(), []
            for e in failures:
                if e["message"] not in seen:
                    seen.add(e["message"])
                    errs.append(e)
            raise DomainError(errs)
        raise DomainError(
            [err("NO_FEASIBLE_LAYOUT", "给定约束下找不到可行的孔位布局")]
        )

    candidates.sort(
        key=lambda c: (
            c["metrics"]["violations"],
            c["metrics"]["thread_changes"],
            c["metrics"]["total_thread_mm"],
            c["metrics"]["hole_deviation_mm"],
            c["metrics"]["hole_count"],
            c["holes_mm"],
        )
    )
    for i, c in enumerate(candidates):
        c["index"] = i
    return candidates[:MAX_CANDIDATES]
