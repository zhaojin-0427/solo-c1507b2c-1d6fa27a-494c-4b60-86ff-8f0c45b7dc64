"""翻身版（work-and-turn）与天地翻（work-and-tumble）过版几何。

书版式（sheetwise）正反各一块印版；翻身版/天地翻全张只有一块共用印版，
一次装纸印两次（两次过版），沿翻纸轴方向的中缝裁开后得到两份相同、
可独立折叠的书帖（一帖两本）。

翻纸轴（物理 180° 翻转）：
- work_and_turn  绕垂直轴（平行 y）翻转：咬口边保持（上下边），侧规左右换边，
  中缝竖切；仅适用于左/右装订（书脊平行翻纸轴）；
- work_and_tumble 绕水平轴（平行 x）翻转：咬口上下/左右换边，侧规边保持，
  中缝横切；仅适用于天头/地脚装订。

共用印版的两个单元：
- 单元0：书版式正面拼版（抽象页格的 F 面）；
- 单元1：书版式背面拼版，在印版上沿翻纸轴镜像放置（B 面在正面视角中的像）。
两次过版后：单元0 第 1 次印第 0 份正面、第 2 次印第 1 份背面；
单元1 第 1 次印第 1 份背面、第 2 次印第 0 份正面。

第 1 份（copy 0）局部坐标内的页格与书版式完全一致，用原折法程序校验；
第 2 份（copy 1）的局部坐标是单元局部镜像（V 轴镜像列、H 轴镜像行），
正面页头沿翻纸轴方向取反，用书脊边对调后的折法程序校验阅读顺序与倒页。
"""
from __future__ import annotations

from .errors import DomainError, err
from .folding import (
    GRIDS,
    Face,
    Layer,
    apply_fold,
    build_program,
)
from .models import JobInput, Rect, SignatureSpec

EPS = 1e-9
ROT_FROM_HEAD = {(0, -1): 0, (1, 0): 90, (0, 1): 180, (-1, 0): 270}


def r3(v: float) -> float:
    v = round(float(v), 3)
    return 0.0 if v == 0 else v


def spine_axis(binding: str) -> str:
    return "V" if binding in ("left", "right") else "H"

OPPOSITE_BINDING = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
OPPOSITE_EDGE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


# ---------------------------------------------------------------------------
# 基础参数
# ---------------------------------------------------------------------------


def turn_axis(job: JobInput) -> str:
    """翻纸轴：'V' 垂直轴（翻身版）/ 'H' 水平轴（天地翻）。"""
    return "V" if job.presswork_mode.value == "work_and_turn" else "H"


def doubled_grid(job: JobInput, spec: SignatureSpec) -> tuple[int, int]:
    """共用印版的全网格（列, 行）：沿翻纸轴方向并两份。"""
    per_sheet = spec.pages // spec.sheets
    cols, rows = GRIDS[per_sheet]
    return (2 * cols, rows) if turn_axis(job) == "V" else (cols, 2 * rows)


def half_origin(
    job: JobInput, spec: SignatureSpec, printable: Rect
) -> tuple[float, float, float, float]:
    """单元0 原点与两份出血框之间的中缝布局。

    返回 (ox, oy, grid_w, grid_h)。全印版关于纸张中心对称，保证两次过版
    套印对齐，单元整体居中于两次过版可印区域的交集：单元0 出血框外边与
    单元1 出血框外边之间净距 = gutter_trim_mm。
    """
    per_sheet = spec.pages // spec.sheets
    cols, rows = GRIDS[per_sheet]
    cw, ch = job.page.width, job.page.height
    grid_w, grid_h = cols * cw, rows * ch
    g = job.gutter_trim_mm
    b = job.bleed_mm
    paper = job.paper
    comb = combined_printable(job, printable)
    # 全印版（沿翻纸轴）：单元0 含出血框 + 中缝净刀距 + 单元1 含出血框。
    # 外侧出血框外另留套准标记余量（由 check_spec 校验）。
    margin = job.bleed_mm + job.marks_margin_mm
    if turn_axis(job) == "V":
        block_w = 2 * grid_w + 2 * b + g
        need_w = block_w + 2 * margin
        if need_w > paper.width + EPS or need_w > comb.width + EPS:
            raise DomainError(
                [err("OUT_OF_PRINTABLE",
                     f"翻身版全印版所需宽 {r3(need_w)}mm 超出两次过版可印区域 "
                     f"{r3(comb.width)}mm（纸宽 {paper.width}mm）")]
            )
        ox = (paper.width - block_w) / 2
        oy = comb.y + (comb.height - grid_h) / 2
    else:
        block_h = 2 * grid_h + 2 * b + g
        need_h = block_h + 2 * margin
        if need_h > paper.height + EPS or need_h > comb.height + EPS:
            raise DomainError(
                [err("OUT_OF_PRINTABLE",
                     f"天地翻全印版所需高 {r3(need_h)}mm 超出两次过版可印区域 "
                     f"{r3(comb.height)}mm（纸高 {paper.height}mm）")]
            )
        ox = comb.x + (comb.width - grid_w) / 2
        oy = (paper.height - block_h) / 2
    return ox, oy, grid_w, grid_h


def cut_line(job: JobInput) -> dict:
    """中缝裁切线（沿翻纸轴方向，位于纸张正中）。"""
    if turn_axis(job) == "V":
        return {
            "axis": "vertical",
            "at_mm": r3(job.paper.width / 2),
            "gutter_trim_mm": r3(job.gutter_trim_mm),
        }
    return {
        "axis": "horizontal",
        "at_mm": r3(job.paper.height / 2),
        "gutter_trim_mm": r3(job.gutter_trim_mm),
    }


def flip_matrix(job: JobInput) -> list[list[float]]:
    """两次过版间的纸张翻转矩阵（齐次坐标，第 1 过版纸框 -> 第 2 过版纸框）。"""
    if turn_axis(job) == "V":
        return [[-1.0, 0.0, r3(job.paper.width)], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    return [[1.0, 0.0, 0.0], [0.0, -1.0, r3(job.paper.height)], [0.0, 0.0, 1.0]]


def flip_point(job: JobInput, x: float, y: float, w: float, h: float) -> tuple[float, float]:
    """第 1 过版纸框内的矩形左上点翻转到第 2 过版纸框后的位置。"""
    if turn_axis(job) == "V":
        return job.paper.width - x - w, y
    return x, job.paper.height - y - h


def flip_rotation(rot: int) -> int:
    """纸面内镜像（任一轴 180° 翻转的投影像）：旋转角 r -> 360-r。"""
    return (360 - rot) % 360


# ---------------------------------------------------------------------------
# 可印区域：两次过版
# ---------------------------------------------------------------------------


def combined_printable(job: JobInput, printable: Rect) -> Rect:
    """两次过版可印区域的交集（对称中心关于纸张中心）。

    共用印版上的每个页格在第 1、2 过版各印一次，整版必须同时落在
    两次可印区域内。翻身版（V 轴）交左右；天地翻（H 轴）交上下。
    """
    pr = printable
    if turn_axis(job) == "V":
        lo = max(pr.x, job.paper.width - pr.x - pr.width)
        hi = min(pr.x + pr.width, job.paper.width - pr.x)
        return Rect(x=r3(lo), y=pr.y, width=r3(hi - lo), height=pr.height)
    lo = max(pr.y, job.paper.height - pr.y - pr.height)
    hi = min(pr.y + pr.height, job.paper.height - pr.y)
    return Rect(x=pr.x, y=r3(lo), width=pr.width, height=r3(hi - lo))


def pass_printables(job: JobInput, printable: Rect) -> list[dict]:
    """两次过版各自的咬口边、侧规边与有效可印区域。

    翻身版：咬口边不变，侧规左右换边；天地翻：咬口换到对边，侧规边不变。
    第 2 过版可印区域由第 1 过版区域经翻转矩阵映射。
    """
    axis = turn_axis(job)
    g_edge = job.press.gripper_edge.value
    side = job.effective_side_lay()
    p1 = {
        "pass": 1,
        "gripper_edge": g_edge,
        "side_lay_edge": side,
        "printable": printable.model_dump(),
    }
    if axis == "V":
        g2, s2 = g_edge, OPPOSITE_EDGE[side]
    else:
        g2, s2 = OPPOSITE_EDGE[g_edge], side
    pr = printable
    if axis == "V":
        pr2 = Rect(
            x=r3(job.paper.width - pr.x - pr.width),
            y=pr.y,
            width=pr.width,
            height=pr.height,
        )
    else:
        pr2 = Rect(
            x=pr.x,
            y=r3(job.paper.height - pr.y - pr.height),
            width=pr.width,
            height=pr.height,
        )
    p2 = {
        "pass": 2,
        "gripper_edge": g2,
        "side_lay_edge": s2,
        "printable": pr2.model_dump(),
    }
    return [p1, p2]


# ---------------------------------------------------------------------------
# 共用印版页格
# ---------------------------------------------------------------------------


def _boxes(x, y, w, h, bleed, safety, dx, dy):
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


def build_plate_cells(
    job: JobInput,
    spec: SignatureSpec,
    layout: dict,
    creep_offset: float,
    ox: float,
    oy: float,
) -> list[dict]:
    """生成共用印版第 1 过版纸框内的全部页格（每抽象页格两个单元）。

    每个页格标注：
    - unit: 印版单元 0/1（左或上=0，右或下=1）；
    - pass1 / pass2: 两次过版各自落到哪一份的哪一面；
    - creep: 第 1 过版纸框内的爬移向量（第 2 过版在 landing 中给出）。
    """
    axis = turn_axis(job)
    binding = job.binding.value
    per_sheet = spec.pages // spec.sheets
    cols, rows = GRIDS[per_sheet]
    cw, ch = job.page.width, job.page.height

    def creep_for(layer: Layer) -> tuple[float, float]:
        if spine_axis(binding) == "V":
            sign = -1.0 if binding == "left" else 1.0
            return (-1.0 if layer.pv else 1.0) * sign * creep_offset, 0.0
        sign = -1.0 if binding == "top" else 1.0
        return 0.0, (-1.0 if layer.ph else 1.0) * sign * creep_offset

    cells: list[dict] = []
    for (r, c), layer in sorted(layout.items()):
        dx, dy = creep_for(layer)
        x0, y0 = ox + c * cw, oy + r * ch
        boxes = _boxes(x0, y0, cw, ch, job.bleed_mm, job.safety_mm, dx, dy)
        cells.append(
            {
                "unit": 0,
                "col": c,
                "row": r,
                "page": layer.front.page,
                "rotation": ROT_FROM_HEAD[layer.front.head],
                "x": r3(x0),
                "y": r3(y0),
                "width": r3(cw),
                "height": r3(ch),
                "creep": {"dx": r3(dx), "dy": r3(dy)},
                # 第 1 过版：单元0 印到第 0 份正面；翻转后第 2 过版印到第 1 份背面
                "pass1": {"copy": 0, "face": "front"},
                "pass2": {"copy": 1, "face": "back"},
                **boxes,
            }
        )
        # 单元1：B 面内容在正面视角中的像（页头沿翻纸轴方向取反）
        hx, hy = layer.back.head
        twin_head = (-hx, hy) if axis == "V" else (hx, -hy)
        dx2, dy2 = (-dx, dy) if axis == "V" else (dx, -dy)
        xt, yt = flip_point(job, x0, y0, cw, ch)
        boxes2 = _boxes(xt, yt, cw, ch, job.bleed_mm, job.safety_mm, dx2, dy2)
        cells.append(
            {
                "unit": 1,
                "col": 2 * cols - 1 - c if axis == "V" else c,
                "row": r if axis == "V" else 2 * rows - 1 - r,
                "page": layer.back.page,
                "rotation": ROT_FROM_HEAD[twin_head],
                "x": r3(xt),
                "y": r3(yt),
                "width": r3(cw),
                "height": r3(ch),
                "creep": {"dx": r3(dx2), "dy": r3(dy2)},
                "pass1": {"copy": 1, "face": "back"},
                "pass2": {"copy": 0, "face": "front"},
                **boxes2,
            }
        )
    return cells


def pass2_cells(job: JobInput, cells: list[dict]) -> list[dict]:
    """把共用印版页格映射到第 2 过版装纸纸框（翻转后的落点坐标与朝向）。"""
    out = []
    for cell in cells:
        x2, y2 = flip_point(job, cell["x"], cell["y"], cell["width"], cell["height"])
        dx, dy = cell["creep"]["dx"], cell["creep"]["dy"]
        dx2, dy2 = (-dx, dy) if turn_axis(job) == "V" else (dx, -dy)
        bb = cell["bleed_box"]
        sb = cell["safe_box"]
        bx2, by2 = flip_point(
            job, bb["x"], bb["y"], bb["width"], bb["height"]
        )
        sx2, sy2 = flip_point(
            job, sb["x"], sb["y"], sb["width"], sb["height"]
        )
        out.append(
            {
                "unit": cell["unit"],
                "col": cell["col"],
                "row": cell["row"],
                "page": cell["page"],
                "rotation": flip_rotation(cell["rotation"]),
                "x": r3(x2),
                "y": r3(y2),
                "width": cell["width"],
                "height": cell["height"],
                "creep": {"dx": r3(dx2), "dy": r3(dy2)},
                "pass1": cell["pass2"],
                "pass2": cell["pass1"],
                "bleed_box": {"x": r3(bx2), "y": r3(by2),
                              "width": bb["width"], "height": bb["height"]},
                "safe_box": {"x": r3(sx2), "y": r3(sy2),
                             "width": sb["width"], "height": sb["height"]},
            }
        )
    return out


# ---------------------------------------------------------------------------
# 两份书帖的折叠校验（倒页/页序）
# ---------------------------------------------------------------------------


def verify_copies(
    job: JobInput,
    spec: SignatureSpec,
    layout: dict,
    leaf_pages: list[tuple],
) -> dict:
    """分别在两份书帖的局部坐标内正向折叠，校验阅读顺序与页头朝向。

    返回 {"copy0": reading_order, "copy1": reading_order, "errors": [...]}。
    错误定位到印版单元与页格（col/row）及页码。
    """
    axis = turn_axis(job)
    per_sheet = spec.pages // spec.sheets
    cols, rows = GRIDS[per_sheet]
    program = build_program(cols, rows, job.binding.value, spec.style.value)
    program2 = build_program(
        cols, rows, OPPOSITE_BINDING[job.binding.value], spec.style.value
    )

    # copy 0：与书版式同一布局、同一折法程序
    st0 = {pos: [layer] for pos, layer in layout.items()}
    for f in program:
        st0 = apply_fold(st0, f)
    stack0 = next(iter(st0.values()))

    # copy 1：局部坐标沿翻纸轴镜像。
    # V 轴（左右装订）：正面页头 x 取反、背面不变，用书脊对调折法程序；
    # H 轴（上下装订）：正背面页头均不变，仅局部行序镜像（折叠模拟验证）。
    g1: dict = {}
    for (r, c), layer in layout.items():
        lr, lc = (r, cols - 1 - c) if axis == "V" else (rows - 1 - r, c)
        if axis == "V":
            fx, fy = layer.front.head
            front_face = Face(layer.front.page, (-fx, fy))
            back_face = Face(layer.back.page, layer.back.head)
        else:
            front_face = Face(layer.front.page, layer.front.head)
            back_face = Face(layer.back.page, layer.back.head)
        g1[(lr, lc)] = [
            Layer(
                up="F",
                front=front_face,
                back=back_face,
                cell=(lr, lc),
            )
        ]
    st1 = g1
    for f in program2:
        st1 = apply_fold(st1, f)
    stack1 = next(iter(st1.values()))

    errors: list[dict] = []
    orders = []
    for copy_no, stack in ((0, stack0), (1, stack1)):
        order = []
        for k, layer in enumerate(stack):
            up = layer.front if layer.up == "F" else layer.back
            exp = leaf_pages[k][0]
            order.append(
                {
                    "leaf": k + 1,
                    "recto": up.page,
                    "verso": (layer.back if layer.up == "F" else layer.front).page,
                    "orientation": "upright" if up.head == (0, -1) else "rotated",
                }
            )
            if up.page != exp:
                errors.append(
                    err(
                        "UPSIDE_DOWN",
                        f"翻身印张第 {copy_no + 1} 份书帖第 {k + 1} 叶页码顺序错误"
                        f"（应为 {exp}，实为 {up.page}）",
                    )
                )
            elif up.head != (0, -1) and up.page is not None:
                errors.append(
                    err(
                        "UPSIDE_DOWN",
                        f"翻身印张第 {copy_no + 1} 份书帖第 {k + 1} 叶"
                        f"第 {up.page} 页倒页",
                    )
                )
        orders.append(order)
    return {"copy0": orders[0], "copy1": orders[1], "errors": errors}


# ---------------------------------------------------------------------------
# 印版套准标记与折线
# ---------------------------------------------------------------------------


def plate_marks(
    job: JobInput, ox: float, oy: float, grid_w: float, grid_h: float
) -> tuple[list[dict], list[dict]]:
    """共用印版套准十字：两份各四角，共 8 枚（返回第 1/第 2 过版坐标）。"""
    off = job.bleed_mm + job.marks_margin_mm / 2
    marks1 = []
    base = [
        (ox - off, oy - off),
        (ox + grid_w + off, oy - off),
        (ox - off, oy + grid_h + off),
        (ox + grid_w + off, oy + grid_h + off),
    ]
    for x, y in base:
        marks1.append({"x": r3(x), "y": r3(y), "kind": "register_cross", "unit": 0})
    for x, y in base:
        fx, fy = flip_point(job, x, y, 0.0, 0.0)
        marks1.append({"x": r3(fx), "y": r3(fy), "kind": "register_cross", "unit": 1})
    marks2 = [
        {
            "x": r3(flip_point(job, m["x"], m["y"], 0.0, 0.0)[0]),
            "y": r3(flip_point(job, m["x"], m["y"], 0.0, 0.0)[1]),
            "kind": "register_cross",
            "unit": m["unit"],
        }
        for m in marks1
    ]
    return marks1, marks2


def plate_fold_lines(
    job: JobInput, program, ox: float, oy: float
) -> list[dict]:
    """两份书帖的折线（单元0 = copy0，单元1 为沿翻纸轴的镜像折线）。"""
    cw, ch = job.page.width, job.page.height
    out = []
    for i, f in enumerate(program):
        if turn_axis(job) == "V":
            at0 = ox + f.line * cw
            at1 = job.paper.width - at0
        else:
            at0 = oy + f.line * ch
            at1 = job.paper.height - at0
        out.append(
            {
                "step": i + 1,
                "axis": f.axis,
                "copy": 0,
                "at_mm": r3(at0),
            }
        )
        out.append(
            {
                "step": i + 1,
                "axis": f.axis,
                "copy": 1,
                "at_mm": r3(at1),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 翻后越界与中缝净距校验
# ---------------------------------------------------------------------------


def validate_geometry(
    job: JobInput,
    sheet_index: int,
    cells1: list[dict],
    cells2: list[dict],
    passes: list[dict],
) -> list[dict]:
    """两次过版可印区域越界、翻后越界、裁线侵入出血检查。错误定位印张与页格。"""
    errors: list[dict] = []
    pr1 = Rect(**passes[0]["printable"])
    pr2 = Rect(**passes[1]["printable"])
    cut = cut_line(job)
    cut_at = cut["at_mm"]

    def check_pass(cells, pr, pass_no):
        for cell in cells:
            bb = cell["bleed_box"]
            if (
                bb["x"] < pr.x - EPS
                or bb["y"] < pr.y - EPS
                or bb["x"] + bb["width"] > pr.x + pr.width + EPS
                or bb["y"] + bb["height"] > pr.y + pr.height + EPS
            ):
                errors.append(
                    err(
                        "FLIP_OUT_OF_BOUNDS",
                        f"印张{sheet_index + 1} 第 {pass_no} 次过版：页格"
                        f"(行{cell['row'] + 1},列{cell['col'] + 1})"
                        f"第 {cell['page']} 页出血框越出翻转后可印区域",
                    )
                )

    check_pass(cells1, pr1, 1)
    check_pass(cells2, pr2, 2)

    # 中缝净距：裁线不得进入任何出血框；两侧出血框之间净距 >= gutter_trim_mm
    # （对称布局理论上恒等，爬移可能使内侧页格出血框向中缝靠拢）
    inner = []
    for cell in cells1:
        bb = cell["bleed_box"]
        if cut["axis"] == "vertical":
            near = cut_at - (bb["x"] + bb["width"]) if cell["unit"] == 0 else bb["x"] - cut_at
        else:
            near = cut_at - (bb["y"] + bb["height"]) if cell["unit"] == 0 else bb["y"] - cut_at
        if near < job.gutter_trim_mm / 2 - EPS:
            inner.append((near, cell))
        if cut["axis"] == "vertical":
            intrude = bb["x"] < cut_at < bb["x"] + bb["width"]
        else:
            intrude = bb["y"] < cut_at < bb["y"] + bb["height"]
        if intrude:
            errors.append(
                err(
                    "CUT_INTRUDES_BLEED",
                    f"印张{sheet_index + 1} 中缝裁线侵入页格"
                    f"(行{cell['row'] + 1},列{cell['col'] + 1})"
                    f"第 {cell['page']} 页出血框",
                )
            )
    if inner and not any(e["code"] == "CUT_INTRUDES_BLEED" for e in errors):
        cell = min(inner, key=lambda t: t[0])[1]
        errors.append(
            err(
                "CUT_CLEARANCE",
                f"印张{sheet_index + 1} 中缝净距小于裁切余量 "
                f"{r3(job.gutter_trim_mm)}mm：页格(行{cell['row'] + 1},"
                f"列{cell['col'] + 1})第 {cell['page']} 页",
            )
        )
    return errors
