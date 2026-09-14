"""书脊配帖标（阶梯标）：按最终折帖次序把每帖的书脊标记反算到制版坐标。

模型约定：
- 每帖一枚矩形标记，位于折好书芯的书脊边上、最外层印张（帖内第 0 张）
  朝外的一面（正面或背面，由折法决定）；
- 标记位置沿书脊按帖序错开：第 i 帖（0 基）在
  第 i//per_column 列、列内第 i%per_column 枚，
  沿书脊起点距离 = start_offset + (i%per_column)*step，
  垂直书脊内缩 = (i//per_column)*column_spacing；
- 书脊长度：左右装订取成品页高，上下装订取成品页宽（与锁线模块一致）；
- 反算路径：折好状态的书脊边位置 -> 经最外层的翻面奇偶映射回平面页格
  -> 叠加该张爬移补偿（与 build_sheet 同一公式）-> 输出所在面制版坐标；
  背面坐标为"从背面看"视角，x 相对正面镜像（与页格坐标约定一致）。

冲突检测（均定位到具体帖号，首个冲突 = 按帖序的首个违规）：
- MARK_SPINE_OVERFLOW   标记超出书脊可用长度（两端扣除安全余量）；
- MARK_SAFETY_INTRUSION 标记外沿侵入成品安全区（job.safety_mm）；
- MARK_OVERLAP          任意两枚标记在书脊平面内重叠（含跨列、跨帖号）；
- MARK_HITS_REGISTER    标记与套准十字标记净距不足（安全余量 + 十字半径）。
"""
from __future__ import annotations

from .errors import err
from .folding import GRIDS, Layer, apply_fold, build_program
from .imposition import r3, spine_axis
from .models import JobInput, Rect, SignatureSpec

EPS = 1e-9
REGISTER_MARK_RADIUS_MM = 3.0  # 套准十字标记的半径（碰撞检测用）


def _outermost_layer(program: list, cols: int, rows: int) -> Layer:
    """正向符号折叠，返回折包最外层（承载帖首页的那一层）。"""
    state = {
        (r, c): [Layer(up="F", cell=(r, c))] for r in range(rows) for c in range(cols)
    }
    for f in program:
        state = apply_fold(state, f)
    stack = next(iter(state.values()))
    return stack[0]


def compute_collating(
    job: JobInput, signatures: list[SignatureSpec], printable: Rect
) -> tuple[dict, list[dict]]:
    """计算全书配帖标。返回 (结果字典, 冲突错误列表)。

    结果字典含：书脊长/可用范围、逐帖标记（制版坐标）、书脊阶梯图案、
    按书脊顺序汇总的核对数据与首个冲突；未配置时由调用方保证不进入。
    """
    cfg = job.collating_marks
    assert cfg is not None
    binding = job.binding.value
    axis = spine_axis(binding)
    spine_len = job.page.height if axis == "V" else job.page.width
    usable_lo = cfg.safety_mm
    usable_hi = spine_len - cfg.safety_mm
    cw, ch = job.page.width, job.page.height
    mw_cfg, mh_cfg = cfg.mark_width_mm, cfg.mark_height_mm

    marks: list[dict] = []
    errors: list[dict] = []
    first_conflict: dict | None = None
    placed: list[tuple[float, float, float, float]] = []  # 已放置标记的书脊平面矩形
    page_base = 0

    def conflict(code: str, message: str, sig_index: int) -> None:
        nonlocal first_conflict
        errors.append(err(code, message))
        if first_conflict is None:
            first_conflict = {
                "code": code,
                "message": message,
                "signature": sig_index,
                "mark_number": sig_index + 1,
            }

    for i, spec in enumerate(signatures):
        per_sheet = spec.pages // spec.sheets
        cols, rows = GRIDS[per_sheet]
        program = build_program(cols, rows, binding, spec.style.value)
        top = _outermost_layer(program, cols, rows)
        r, c = top.cell
        grid_w, grid_h = cols * cw, rows * ch
        if job.presswork_active():
            from . import presswork

            # 第 0 份书帖单元的平面原点（与印版构造一致）
            ox, oy, _, _ = presswork.half_origin(job, spec, printable)
        else:
            ox = printable.x + (printable.width - grid_w) / 2
            oy = printable.y + (printable.height - grid_h) / 2
        x0, y0 = ox + c * cw, oy + r * ch

        col = i // cfg.per_column
        d = cfg.start_offset_mm + (i % cfg.per_column) * cfg.step_mm
        perp = col * cfg.column_spacing_mm

        # 爬移：最外层张（s=0）的补偿量与方向（同 build_sheet 公式）
        creep = (spec.sheets - 1) * job.paper.thickness
        if axis == "V":
            sign = -1.0 if binding == "left" else 1.0
            dx = (-1.0 if top.pv else 1.0) * sign * creep
            dy = 0.0
            # 书脊边：左装订且未翻面（或右装订且翻面）-> 页格左边
            if (binding == "left") != top.pv:
                mx = x0 + perp
            else:
                mx = x0 + cw - perp - mw_cfg
            my = y0 + d if not top.ph else y0 + ch - d - mh_cfg
            mw, mh = mw_cfg, mh_cfg
            rot_front = 0 if not top.ph else 180
        else:
            sign = -1.0 if binding == "top" else 1.0
            dx = 0.0
            dy = (-1.0 if top.ph else 1.0) * sign * creep
            # 书脊边：天头装订且未翻面（或地脚装订且翻面）-> 页格上边
            if (binding == "top") != top.ph:
                my = y0 + perp
            else:
                my = y0 + ch - perp - mw_cfg
            mx = x0 + d if not top.pv else x0 + cw - d - mh_cfg
            mw, mh = mh_cfg, mw_cfg
            rot_front = 270 if not top.pv else 90

        side = "front" if top.up == "F" else "back"
        # 套用爬移后即为该面制版坐标。
        # 书版式背面 = 绕垂直轴看背面（x 镜像）；翻身版/天地翻坐标统一在
        # 共用印版第 1 过版纸框（正面视角）内，背面内容按翻纸轴镜像。
        mx += dx
        my += dy
        if job.presswork_active():
            from . import presswork

            axis = presswork.turn_axis(job)
            # (mx,my) 是折好状态书脊标记反算到抽象页格正面（F 面）的位置。
            # 若标记在 B 面：在单元0 局部正面坐标系内作轴向镜像
            # （与 build_plate_cells 中背面内容的正面像同一几何）。
            if side == "back":
                if axis == "V":
                    u0x = 2 * ox + grid_w - mx - mw
                    u0y = my
                else:
                    u0x = mx
                    u0y = 2 * oy + grid_h - my - mh
            else:
                u0x, u0y = mx, my
            base_x, base_y = u0x, u0y
            base_rot = (360 - rot_front) % 360 if side == "back" else rot_front
            out_x, out_rot = base_x, base_rot
            out_creep = {"dx": r3(dx), "dy": r3(dy)}
        elif side == "back":
            out_x = job.paper.width - mx - mw
            out_rot = (360 - rot_front) % 360
            out_creep = {"dx": r3(-dx), "dy": r3(dy)}
        else:
            out_x = mx
            out_rot = rot_front
            out_creep = {"dx": r3(dx), "dy": r3(dy)}

        page_base += spec.pages
        page_base_prev = page_base - spec.pages
        mark = {
            "signature": i,
            "mark_number": i + 1,
            "page_start": page_base_prev + 1,
            "page_end": page_base,
            "sheet": 0,
            "side": side,
            "column": col,
            "spine_start_mm": r3(d),
            "spine_end_mm": r3(d + mh_cfg),
            "x": r3(out_x),
            "y": r3(my),
            "width": r3(mw),
            "height": r3(mh),
            "rotation": out_rot,
            "creep": out_creep,
        }
        if job.presswork_active():
            from . import presswork

            # 共用印版上的两份配帖标：第 0 份位于单元0（正面视角坐标），
            # 第 1 份为沿翻纸轴镜像的孪生位置（两次过版各落到一份书帖书脊）。
            fx, fy = base_x, base_y
            fw, fh = mw, mh
            frot = base_rot
            if presswork.turn_axis(job) == "V":
                tx, ty = job.paper.width - fx - fw, fy
            else:
                tx, ty = fx, job.paper.height - fy - fh
            trot = (360 - frot) % 360
            mark["plate_positions"] = [
                {"copy": 0, "unit": 0, "x": r3(fx), "y": r3(fy),
                 "width": r3(fw), "height": r3(fh), "rotation": frot},
                {"copy": 1, "unit": 1, "x": r3(tx), "y": r3(ty),
                 "width": r3(fw), "height": r3(fh), "rotation": trot},
            ]
        marks.append(mark)

        # --- 冲突检测（按帖序，首个违规即首个冲突） ---
        if d < usable_lo - EPS or d + mh_cfg > usable_hi + EPS:
            conflict(
                "MARK_SPINE_OVERFLOW",
                f"帖{i + 1} 配帖标沿书脊 [{r3(d)}, {r3(d + mh_cfg)}]mm "
                f"超出书脊可用长度 [{r3(usable_lo)}, {r3(usable_hi)}]mm",
                i,
            )
        if perp + mw_cfg > job.safety_mm + EPS:
            conflict(
                "MARK_SAFETY_INTRUSION",
                f"帖{i + 1} 配帖标外沿距书脊 {r3(perp + mw_cfg)}mm "
                f"侵入成品安全区 {r3(job.safety_mm)}mm",
                i,
            )
        cur = (d, d + mh_cfg, perp, perp + mw_cfg)
        # 与所有已放置标记两两检查（同列步距不足、跨列列距不足均会相交）
        for j, prev in enumerate(placed):
            if (
                prev[0] < cur[1] - EPS
                and cur[0] < prev[1] - EPS
                and prev[2] < cur[3] - EPS
                and cur[2] < prev[3] - EPS
            ):
                conflict(
                    "MARK_OVERLAP",
                    f"帖{j + 1} 与帖{i + 1} 配帖标重叠（沿书脊 "
                    f"[{r3(prev[0])}, {r3(prev[1])}]mm 与 "
                    f"[{r3(cur[0])}, {r3(cur[1])}]mm 相交）",
                    i,
                )
        placed.append(cur)

        # 套准标记碰撞：在抽象页格正面坐标系下，标记矩形外扩
        # (安全余量+十字半径) 后含单元0 四角十字心（书版式背面按 x 镜像）。
        # 翻身版/天地翻共用印版的单元1 与单元0 关于翻纸轴对称，只需验单元0。
        off = job.bleed_mm + job.marks_margin_mm / 2
        reg = [
            (ox - off, oy - off),
            (ox + grid_w + off, oy - off),
            (ox - off, oy + grid_h + off),
            (ox + grid_w + off, oy + grid_h + off),
        ]
        if not job.presswork_active() and side == "back":
            reg = [(job.paper.width - rx, ry) for rx, ry in reg]
        k = cfg.safety_mm + REGISTER_MARK_RADIUS_MM
        chk_x, chk_y = (mx, my) if job.presswork_active() else (out_x, my)
        if any(
            chk_x - k - EPS <= rx <= chk_x + mw + k + EPS
            and chk_y - k - EPS <= ry <= chk_y + mh + k + EPS
            for rx, ry in reg
        ):
            conflict(
                "MARK_HITS_REGISTER",
                f"帖{i + 1} 配帖标与套准标记净距不足"
                f"（要求 ≥ {r3(cfg.safety_mm)}mm，十字半径 {REGISTER_MARK_RADIUS_MM}mm）",
                i,
            )

    spine_sorted = sorted(
        marks, key=lambda m: (m["spine_start_mm"], m["column"], m["mark_number"])
    )
    result = {
        "enabled": True,
        "spine_length_mm": r3(spine_len),
        "usable_range_mm": [r3(usable_lo), r3(usable_hi)],
        "marks": marks,
        "spine_pattern": [
            {
                "order": k,
                "column": m["column"],
                "spine_start_mm": m["spine_start_mm"],
                "spine_end_mm": m["spine_end_mm"],
                "mark_number": m["mark_number"],
                "signature": m["signature"],
            }
            for k, m in enumerate(spine_sorted)
        ],
        "spine_check": [
            {
                "order": k,
                "mark_number": m["mark_number"],
                "signature": m["signature"],
                "page_start": m["page_start"],
                "page_end": m["page_end"],
                "sheet": m["sheet"],
                "side": m["side"],
                "column": m["column"],
                "spine_start_mm": m["spine_start_mm"],
                "spine_end_mm": m["spine_end_mm"],
            }
            for k, m in enumerate(spine_sorted)
        ],
        "first_conflict": first_conflict,
    }
    return result, errors
