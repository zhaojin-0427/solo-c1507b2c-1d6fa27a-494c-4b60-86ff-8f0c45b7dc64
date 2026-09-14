"""拼版几何：坐标计算、爬移补偿、折叠/裁切工序模拟与方案校验。

坐标约定：原点为纸张左上角，x 向右，y 向下，单位 mm。
正面 = 从印张正面看；背面坐标为"从背面直接看"的视角（绕垂直轴翻转，
x 相对正面镜像）。旋转角度为顺时针度数（0/90/180/270），指页头方向。
"""
from __future__ import annotations

from .errors import DomainError, err
from .folding import (
    GRIDS,
    Fold,
    _in_moving,
    _mirror_pos,
    build_program,
    generate_layout,
    inset_leaf_slots,
    make_leaf_pages,
    verify_layout,
)
from . import presswork
from .models import JobInput, Rect, SignatureSpec

EPS = 1e-9

ROT_FROM_HEAD = {(0, -1): 0, (1, 0): 90, (0, 1): 180, (-1, 0): 270}

EDGE_NAMES = {"top": "天头", "bottom": "地脚", "left": "左侧", "right": "右侧"}
MOVING_TEXT = {
    "left": "将左侧折向右侧",
    "right": "将右侧折向左侧",
    "top": "将上侧折向下侧",
    "bottom": "将下侧折向上侧",
}


def r3(v: float) -> float:
    """保留 3 位小数并消除 -0.0，保证输出确定性。"""
    v = round(float(v), 3)
    return 0.0 if v == 0 else v


# ---------------------------------------------------------------------------
# 可印区域
# ---------------------------------------------------------------------------


def effective_printable(job: JobInput) -> Rect:
    """有效可印区域 = 印刷机可用区域 ∩ 纸张，再扣除咬口。"""
    paper = job.paper
    pr = job.press.printable or Rect(x=0, y=0, width=paper.width, height=paper.height)
    if (
        pr.x < -EPS
        or pr.y < -EPS
        or pr.x + pr.width > paper.width + EPS
        or pr.y + pr.height > paper.height + EPS
    ):
        raise DomainError(
            [err("PRINTABLE_OUT_OF_PAPER", "印刷机可用区域超出纸张范围")]
        )
    g = job.press.gripper_mm
    edge = job.press.gripper_edge.value
    x, y, w, h = pr.x, pr.y, pr.width, pr.height
    if edge == "bottom":
        h -= g
    elif edge == "top":
        y += g
        h -= g
    elif edge == "left":
        x += g
        w -= g
    else:
        w -= g
    if w <= EPS or h <= EPS:
        raise DomainError(
            [err("PRINTABLE_OUT_OF_PAPER", "扣除咬口后可印区域为空")]
        )
    return Rect(x=r3(x), y=r3(y), width=r3(w), height=r3(h))


def spine_axis(binding: str) -> str:
    """书脊折线轴向：左右装订 -> 垂直折线；上下装订 -> 水平折线。"""
    return "V" if binding in ("left", "right") else "H"


# ---------------------------------------------------------------------------
# 折帖规格静态检查（越出可印区域 / 纸纹不合）
# ---------------------------------------------------------------------------


def check_spec(job: JobInput, spec: SignatureSpec) -> list[dict]:
    """检查某折帖规格在该任务下是否可行，返回错误列表（空 = 可行）。"""
    errors: list[dict] = []
    per_sheet = spec.pages // spec.sheets
    cols, rows = GRIDS[per_sheet]
    grid_w = cols * job.page.width
    grid_h = rows * job.page.height
    margin = job.bleed_mm + job.marks_margin_mm
    pr = effective_printable(job)
    if job.presswork_active():
        # 一帖两本：两份成品网格 + 中缝（两份出血 + 裁切余量）+ 外侧标记余量，
        # 须落在两次过版可印区域交集（关于纸张中心对称）
        axis = presswork.turn_axis(job)
        pr = presswork.combined_printable(job, pr)
        gutter = 2 * job.bleed_mm + job.gutter_trim_mm
        margin = job.bleed_mm + job.marks_margin_mm
        if axis == "V":
            need_w = 2 * grid_w + gutter + 2 * margin
            need_h = grid_h + 2 * margin
        else:
            need_w = grid_w + 2 * margin
            need_h = 2 * grid_h + gutter + 2 * margin
        label = "翻身版" if axis == "V" else "天地翻"
    else:
        need_w = grid_w + 2 * margin
        need_h = grid_h + 2 * margin
        label = ""
    if need_w > pr.width + EPS or need_h > pr.height + EPS:
        errors.append(
            err(
                "OUT_OF_PRINTABLE",
                f"{label}{spec.pages} 页帖（{spec.style.value}）所需区域 "
                f"{r3(need_w)}×{r3(need_h)}mm 超出可印区域 "
                f"{r3(pr.width)}×{r3(pr.height)}mm",
            )
        )
    need_grain = "vertical" if spine_axis(job.binding.value) == "V" else "horizontal"
    if job.paper.grain.value != need_grain:
        errors.append(
            err(
                "GRAIN_MISMATCH",
                f"纸纹方向 {job.paper.grain.value} 与书脊方向不一致"
                f"（要求 {need_grain}，即纸纹平行于书脊）",
            )
        )
    return errors


# ---------------------------------------------------------------------------
# 折包模拟：折步、 packet 尺寸、各边折口状态（用于裁切工序与爬移方向）
# ---------------------------------------------------------------------------


def _or_edge(a: dict, b: dict) -> dict:
    fold = a["fold"] or b["fold"]
    line = a["line"] if a["fold"] else b["line"]
    return {"fold": fold, "line": line}


def _reflect_edge(edge: dict, line: float) -> dict:
    if not edge["fold"]:
        return {"fold": False, "line": None}
    return {"fold": True, "line": 2 * line - edge["line"]}


def simulate_packet(program: list[Fold], cols: int, rows: int):
    """正向模拟折包：返回 (每步 packet 矩形, 最终各边状态)。

    各边状态: {"fold": 是否有折口, "line": 形成该边的折线在原始网格中的位置(格)}。
    """
    positions = {(r, c) for r in range(rows) for c in range(cols)}
    edges = {s: {"fold": False, "line": None} for s in ("left", "right", "top", "bottom")}
    steps = []
    for f in program:
        moving = {p for p in positions if _in_moving(p, f)}
        positions = (positions - moving) | {_mirror_pos(p, f) for p in moving}
        e = dict(edges)
        if f.axis == "V":
            if f.moving == "left":
                e["left"] = {"fold": True, "line": float(f.line)}
                e["right"] = _or_edge(edges["right"], _reflect_edge(edges["left"], f.line))
            else:
                e["right"] = {"fold": True, "line": float(f.line)}
                e["left"] = _or_edge(edges["left"], _reflect_edge(edges["right"], f.line))
        else:
            if f.moving == "top":
                e["top"] = {"fold": True, "line": float(f.line)}
                e["bottom"] = _or_edge(edges["bottom"], _reflect_edge(edges["top"], f.line))
            else:
                e["bottom"] = {"fold": True, "line": float(f.line)}
                e["top"] = _or_edge(edges["top"], _reflect_edge(edges["bottom"], f.line))
        edges = e
        rs = [p[0] for p in positions]
        cs = [p[1] for p in positions]
        steps.append(
            {
                "fold": f,
                "rect": (min(cs), min(rs), max(cs) + 1, max(rs) + 1),
            }
        )
    return steps, edges


# ---------------------------------------------------------------------------
# 单张纸拼版
# ---------------------------------------------------------------------------


def _cell_boxes(x, y, w, h, bleed, safety, dx, dy):
    return {
        "bleed_box": {
            "x": r3(x - bleed + dx),
            "y": r3(y - bleed + dy),
            "width": r3(w + 2 * bleed),
            "height": r3(h + 2 * bleed),
        },
        "safe_box": {
            "x": r3(x + safety + dx),
            "y": r3(y + safety + dy),
            "width": r3(w - 2 * safety),
            "height": r3(h - 2 * safety),
        },
    }


def build_sheet(
    job: JobInput,
    spec: SignatureSpec,
    sheet_index: int,
    leaf_pages: list[tuple],
    program: list[Fold],
    printable: Rect,
    creep_offset: float,
) -> dict:
    """生成一张印张的正反面拼版（含爬移补偿后的坐标）。

    leaf_pages: 本张纸各叶的 (奇数页, 偶数页)，由 build_signature 按
    单张顺序或套帖叶序（inset_leaf_slots）分配。

    翻身版/天地翻（job.presswork_active()）：全张只有一块共用印版，
    两次过版；front/back 分别为第 1/第 2 过版装纸纸框视图，另附 presswork
    块给出翻纸轴、装纸方向、翻转矩阵、共用印版号、中缝与逐格坐标。
    """
    per_sheet = spec.pages // spec.sheets
    cols, rows = GRIDS[per_sheet]
    cw, ch = job.page.width, job.page.height
    grid_w, grid_h = cols * cw, rows * ch

    layout = generate_layout(cols, rows, program, leaf_pages)
    ver = verify_layout(layout, program, leaf_pages)
    if not ver["ok"]:
        raise DomainError(
            [err("UPSIDE_DOWN", f"印张 {sheet_index + 1} 存在倒页或页序错误")]
        )

    if job.presswork_active():
        return _build_presswork_sheet(
            job, spec, sheet_index, leaf_pages, program, printable,
            creep_offset, layout, ver, cols, rows, cw, ch, grid_w, grid_h,
        )

    margin = job.bleed_mm + job.marks_margin_mm
    ox = printable.x + (printable.width - grid_w) / 2
    oy = printable.y + (printable.height - grid_h) / 2

    binding = job.binding.value
    axis = spine_axis(binding)
    front_cells = []
    back_cells = []
    for (r, c), layer in sorted(layout.items()):
        x = ox + c * cw
        y = oy + r * ch
        # 爬移补偿：折好状态下朝书脊方向平移 creep_offset，
        # 经该层经历的翻面奇偶映射回平面坐标。
        if axis == "V":
            sign = -1.0 if binding == "left" else 1.0
            dx = (-1.0 if layer.pv else 1.0) * sign * creep_offset
            dy = 0.0
        else:
            sign = -1.0 if binding == "top" else 1.0
            dx = 0.0
            dy = (-1.0 if layer.ph else 1.0) * sign * creep_offset
        boxes = _cell_boxes(x, y, cw, ch, job.bleed_mm, job.safety_mm, dx, dy)
        front_cells.append(
            {
                "page": layer.front.page,
                "rotation": ROT_FROM_HEAD[layer.front.head],
                "col": c,
                "row": r,
                "x": r3(x),
                "y": r3(y),
                "width": r3(cw),
                "height": r3(ch),
                "creep": {"dx": r3(dx), "dy": r3(dy)},
                **boxes,
            }
        )
        # 背面：绕垂直轴翻转观察，x 镜像、旋转镜像、爬移 dx 反向
        rot_back = (360 - ROT_FROM_HEAD[layer.back.head]) % 360
        back_cells.append(
            {
                "page": layer.back.page,
                "rotation": rot_back,
                "col": c,
                "row": r,
                "x": r3(job.paper.width - x - cw),
                "y": r3(y),
                "width": r3(cw),
                "height": r3(ch),
                "creep": {"dx": r3(-dx), "dy": r3(dy)},
                "bleed_box": {
                    "x": r3(job.paper.width - boxes["bleed_box"]["x"] - boxes["bleed_box"]["width"]),
                    "y": boxes["bleed_box"]["y"],
                    "width": boxes["bleed_box"]["width"],
                    "height": boxes["bleed_box"]["height"],
                },
                "safe_box": {
                    "x": r3(job.paper.width - boxes["safe_box"]["x"] - boxes["safe_box"]["width"]),
                    "y": boxes["safe_box"]["y"],
                    "width": boxes["safe_box"]["width"],
                    "height": boxes["safe_box"]["height"],
                },
            }
        )

    off = job.bleed_mm + job.marks_margin_mm / 2
    marks = [
        {"x": r3(ox - off), "y": r3(oy - off), "kind": "register_cross"},
        {"x": r3(ox + grid_w + off), "y": r3(oy - off), "kind": "register_cross"},
        {"x": r3(ox - off), "y": r3(oy + grid_h + off), "kind": "register_cross"},
        {"x": r3(ox + grid_w + off), "y": r3(oy + grid_h + off), "kind": "register_cross"},
    ]
    fold_lines = [
        {
            "step": i + 1,
            "axis": f.axis,
            "at_mm": r3(ox + f.line * cw) if f.axis == "V" else r3(oy + f.line * ch),
        }
        for i, f in enumerate(program)
    ]
    return {
        "index": sheet_index,
        "creep_offset_mm": r3(creep_offset),
        "grid": {"cols": cols, "rows": rows},
        "front": {"cells": front_cells},
        "back": {"cells": back_cells},
        "marks": marks,
        "fold_lines": fold_lines,
        "reading_order": ver["reading_order"],
    }


def _build_presswork_sheet(
    job, spec, sheet_index, leaf_pages, program, printable,
    creep_offset, layout, ver, cols, rows, cw, ch, grid_w, grid_h,
) -> dict:
    """翻身版/天地翻单张：一块共用印版，两次过版，沿中缝裁开一帖两本。"""
    sig_index = sheet_index  # 仅用于错误信息时由调用方覆盖，这里取张序
    axis = presswork.turn_axis(job)
    ox, oy, hw, hh = presswork.half_origin(job, spec, printable)
    cells1 = presswork.build_plate_cells(job, spec, layout, creep_offset, ox, oy)
    cells2 = presswork.pass2_cells(job, cells1)
    marks1, marks2 = presswork.plate_marks(job, ox, oy, hw, hh)
    passes = presswork.pass_printables(job, printable)
    geom_errors = presswork.validate_geometry(
        job, sheet_index, cells1, cells2, passes
    )
    if geom_errors:
        raise DomainError(geom_errors)
    copies = presswork.verify_copies(job, spec, layout, leaf_pages)
    if copies["errors"]:
        raise DomainError(copies["errors"])
    fold_lines = presswork.plate_fold_lines(job, program, ox, oy)
    cut = presswork.cut_line(job)
    mode = job.presswork_mode.value
    return {
        "index": sheet_index,
        "creep_offset_mm": r3(creep_offset),
        "grid": {"cols": 2 * cols if axis == "V" else cols,
                 "rows": rows if axis == "V" else 2 * rows},
        # front = 第 1 过版视图，back = 第 2 过版装纸视图
        "front": {"cells": cells1},
        "back": {"cells": cells2},
        "marks": marks1,
        "back_marks": marks2,
        "fold_lines": fold_lines,
        "reading_order": copies["copy0"],
        "presswork": {
            "mode": mode,
            "turn_axis": "vertical" if axis == "V" else "horizontal",
            "shared_plate": True,
            "plate_id": f"P{sheet_index + 1}",
            "flip_matrix": presswork.flip_matrix(job),
            "cut_line": cut,
            "half_origin": {"x": r3(ox), "y": r3(oy),
                            "width": r3(hw), "height": r3(hh)},
            "passes": passes,
            "pass1_cells": cells1,
            "pass2_cells": cells2,
            "pass1_marks": marks1,
            "pass2_marks": marks2,
            "copies": [
                {"copy": 0, "reading_order": copies["copy0"]},
                {"copy": 1, "reading_order": copies["copy1"]},
            ],
        },
    }


# ---------------------------------------------------------------------------
# 折帖
# ---------------------------------------------------------------------------


def build_signature(
    job: JobInput,
    spec: SignatureSpec,
    sig_index: int,
    page_base: int,
    printable: Rect,
) -> dict:
    per_sheet = spec.pages // spec.sheets
    cols, rows = GRIDS[per_sheet]
    program = build_program(cols, rows, job.binding.value, spec.style.value)
    cw, ch = job.page.width, job.page.height
    grid_w, grid_h = cols * cw, rows * ch

    active = job.presswork_active()
    axis = presswork.turn_axis(job) if active else None
    if active:
        # 第 0 份书帖单元原点（与 _build_presswork_sheet 一致）
        ox, oy, _, _ = presswork.half_origin(job, spec, printable)
    else:
        ox = printable.x + (printable.width - grid_w) / 2
        oy = printable.y + (printable.height - grid_h) / 2

    # 逐张生成（爬移：最外层张不动，内层逐张向书脊补偿 纸厚×层深）
    # 整帖叶页码表 -> 各张纸的叶位（单张帖为顺序叶，套帖为 inset 叶序）
    all_leaves = make_leaf_pages(spec.pages // 2, page_base, job.total_pages)
    slots = inset_leaf_slots(spec.sheets, per_sheet // 2)
    sheets = []
    blanks = 0
    for s in range(spec.sheets):
        offset = (spec.sheets - 1 - s) * job.paper.thickness
        sheet = build_sheet(
            job, spec, s, [all_leaves[i] for i in slots[s]], program, printable, offset
        )
        if active:
            # 一份书帖的空白页：只统计第 0 份各抽象页格的 F 面（unit0 第 1 过版）
            # 与 B 面（unit1 第 2 过版落第 0 份正面），与书版式正/反面一一对应
            blanks += sum(
                1
                for cell in sheet["presswork"]["pass1_cells"]
                if cell["pass1"]["copy"] == 0 and cell["page"] is None
            )
            blanks += sum(
                1
                for cell in sheet["presswork"]["pass2_cells"]
                if cell["pass1"]["copy"] == 1 and cell["page"] is None
            )
        else:
            blanks += sum(
                1
                for side in ("front", "back")
                for cell in sheet[side]["cells"]
                if cell["page"] is None
            )
        sheets.append(sheet)

    # 折叠 / 裁切工序（同一帖各张相同；坐标取第 0 份书帖单元）
    raw_steps, edges = simulate_packet(program, cols, rows)
    fold_steps = []
    step_no = 0
    if active:
        # 两次过版后先沿中缝裁开（一帖两本），再各自折叠
        step_no += 1
        cut = presswork.cut_line(job)
        fold_steps.append(
            {
                "step": step_no,
                "type": "cut",
                "axis": cut["axis"],
                "line_mm": cut["at_mm"],
                "gutter_trim_mm": cut["gutter_trim_mm"],
                "action": (
                    "沿垂直中缝裁开，得到两份相同书帖"
                    if cut["axis"] == "vertical"
                    else "沿水平中缝裁开，得到两份相同书帖"
                ),
            }
        )
    for i, st in enumerate(raw_steps, 1):
        f: Fold = st["fold"]
        c0, r0, c1, r1 = st["rect"]
        step_no += 1
        fold_steps.append(
            {
                "step": step_no,
                "type": "fold",
                "axis": "vertical" if f.axis == "V" else "horizontal",
                "action": MOVING_TEXT[f.moving],
                "line_mm": r3(ox + f.line * cw) if f.axis == "V" else r3(oy + f.line * ch),
                "packet_mm": {
                    "x": r3(ox + c0 * cw),
                    "y": r3(oy + r0 * ch),
                    "width": r3((c1 - c0) * cw),
                    "height": r3((r1 - r0) * ch),
                },
            }
        )
    trims = []
    n = step_no
    binding = job.binding.value
    for side in ("top", "right", "bottom", "left"):
        if side == binding:
            continue  # 书脊边不裁切
        opens = edges[side]["fold"]
        name = EDGE_NAMES[side]
        if binding in ("left", "right") and side in ("left", "right"):
            name = "前口"
        n += 1
        trims.append(
            {
                "step": n,
                "type": "trim",
                "edge": side,
                "opens_fold": opens,
                "action": (f"裁切{name}并打开折口" if opens else f"裁切{name}至成品尺寸"),
            }
        )

    # 爬移后的出血与安全边界检查（逐张）
    warnings = []
    for s, sheet in enumerate(sheets):
        offset = sheet["creep_offset_mm"]
        if offset <= EPS:
            continue
        if job.bleed_mm - offset < -EPS:
            warnings.append(
                f"帖{sig_index + 1} 印张{s + 1}：爬移补偿 {r3(offset)}mm 超过出血 "
                f"{r3(job.bleed_mm)}mm，前口出血不足"
            )
        if job.safety_mm - offset < -EPS:
            warnings.append(
                f"帖{sig_index + 1} 印张{s + 1}：爬移补偿 {r3(offset)}mm 侵蚀书脊侧"
                f"安全边界 {r3(job.safety_mm)}mm"
            )

    return {
        "index": sig_index,
        "spec": {
            "pages": spec.pages,
            "style": spec.style.value,
            "sheets": spec.sheets,
        },
        "page_start": page_base + 1,
        "page_end": page_base + spec.pages,
        "blank_pages": blanks,
        "sheets": sheets,
        "folding": {"steps": fold_steps, "trims": trims},
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 整案校验（重页 / 缺页）
# ---------------------------------------------------------------------------


def validate_plan_pages(
    job: JobInput | None, signatures: list[dict], total_pages: int
) -> list[dict]:
    """汇总全案页码，检查重页与缺页。返回错误列表。

    翻身版/天地翻同一印版两次过版各印一份书帖，只按第 0 份的页码统计：
    unit0 第 1 过版落第 0 份正面，unit1 第 2 过版落第 0 份正面（实为其背面）。
    """
    seen: dict[int, int] = {}
    for sig in signatures:
        for sheet in sig["sheets"]:
            if job is not None and job.presswork_active():
                # 第 0 份：第 1 过版取 unit0 正面；其背面在第 2 过版时由 unit1 落上
                for cell in sheet["presswork"]["pass1_cells"]:
                    if cell["unit"] == 0 and cell["page"] is not None:
                        seen[cell["page"]] = seen.get(cell["page"], 0) + 1
                for cell in sheet["presswork"]["pass2_cells"]:
                    if cell["unit"] == 1 and cell["page"] is not None:
                        seen[cell["page"]] = seen.get(cell["page"], 0) + 1
                continue
            for side in ("front", "back"):
                for cell in sheet[side]["cells"]:
                    p = cell["page"]
                    if p is not None:
                        seen[p] = seen.get(p, 0) + 1
    errors = []
    missing = [p for p in range(1, total_pages + 1) if p not in seen]
    duplicated = sorted(p for p, n in seen.items() if n > 1)
    if missing:
        errors.append(err("MISSING_PAGE", f"缺页: {missing}"))
    if duplicated:
        errors.append(err("DUPLICATE_PAGE", f"重页: {duplicated}"))
    return errors
