"""折叠引擎：折法程序生成、正向折叠模拟与反向展开生成。

坐标系：网格坐标，x 向右（列），y 向下（行），单位为"页格"。
每个页格是印张上的一个成品页位置；印张有正(F)/反(B)两面。

核心思想：
- 正向折叠 apply_fold：把折法程序作用于平面印张，得到折帖书帖堆叠。
- 反向展开 apply_unfold：从折好的书帖（已按阅读顺序赋页码）反向展开，
  得到平面印张上每个页格正/反面的页码与朝向 —— 即拼版结果。
- 生成后再做一次正向模拟校验：阅读顺序必须递增、所有页头朝上（不倒页）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# 页头朝向（向量）：上=(0,-1) 右=(1,0) 下=(0,1) 左=(-1,0)
HEAD_UP = (0, -1)

# 每张纸页数 -> (列数, 行数)
GRIDS = {8: (2, 2), 12: (3, 2), 16: (4, 2), 32: (4, 4)}

# 每张纸页数 -> 可用折法
STYLES = {
    8: {"standard"},
    12: {"roll", "z"},
    16: {"standard", "cross"},
    32: {"standard", "cross"},
}


@dataclass
class Face:
    """纸面上某一面的内容：页码(None=空白)与页头朝向。"""

    page: int | None = None
    head: tuple[int, int] = HEAD_UP


@dataclass
class Layer:
    """一层纸（一个页格），含正反两面；up 表示当前朝上的一面。

    pv/ph 记录该层自平面状态起经历的垂直/水平翻面次数奇偶，
    用于把"折好状态下朝书脊方向"的爬移补偿正确映射回平面坐标。
    cell 记录该层所属的原始页格位置。
    """

    up: str = "F"  # 'F' | 'B'
    front: Face = field(default_factory=Face)
    back: Face = field(default_factory=Face)
    pv: bool = False
    ph: bool = False
    cell: tuple = (0, 0)


@dataclass
class Fold:
    """一次折叠。

    axis: 'V' 垂直折线 / 'H' 水平折线；line: 折线位置（格单位）；
    moving: 移动侧 left/right/top/bottom；placement: 移动部分落在静止部分之上(over)/之下(under)。
    moving_cells/moved_count 由 resolve_meta 在正向预演时填充，供反向展开使用。
    """

    axis: str
    line: int
    moving: str
    placement: str = "over"
    moving_cells: frozenset = frozenset()
    moved_count: int = 0


# ---------------------------------------------------------------------------
# 几何原语
# ---------------------------------------------------------------------------


def _mirror_idx(i: int, line: int) -> int:
    return 2 * line - 1 - i


def _in_moving(pos: tuple[int, int], fold: Fold) -> bool:
    r, c = pos
    if fold.axis == "V":
        return c < fold.line if fold.moving == "left" else c >= fold.line
    return r < fold.line if fold.moving == "top" else r >= fold.line


def _mirror_pos(pos: tuple[int, int], fold: Fold) -> tuple[int, int]:
    r, c = pos
    if fold.axis == "V":
        return (r, _mirror_idx(c, fold.line))
    return (_mirror_idx(r, fold.line), c)


def _flip_layer(layer: Layer, axis: str) -> Layer:
    """纸层翻面（沿折线镜像）：正反面互换，页头向量在折线轴向上取反。"""

    def fh(h: tuple[int, int]) -> tuple[int, int]:
        return (-h[0], h[1]) if axis == "V" else (h[0], -h[1])

    return Layer(
        up=("B" if layer.up == "F" else "F"),
        front=Face(layer.front.page, fh(layer.front.head)),
        back=Face(layer.back.page, fh(layer.back.head)),
        pv=(not layer.pv) if axis == "V" else layer.pv,
        ph=(not layer.ph) if axis == "H" else layer.ph,
        cell=layer.cell,
    )


# ---------------------------------------------------------------------------
# 正向折叠 / 反向展开
# ---------------------------------------------------------------------------


def apply_fold(state: dict, fold: Fold) -> dict:
    """正向折叠一步。state: {位置: [纸层栈，顶→底]}。"""
    new: dict = {}
    moved: dict = {}
    for pos, stack in state.items():
        if _in_moving(pos, fold):
            q = _mirror_pos(pos, fold)
            moved[q] = [_flip_layer(l, fold.axis) for l in reversed(stack)]
        else:
            new[pos] = list(stack)
    for q, block in moved.items():
        base = new.get(q, [])
        new[q] = (block + base) if fold.placement == "over" else (base + block)
    return new


def apply_unfold(state: dict, fold: Fold) -> dict:
    """反向展开一步（apply_fold 的逆运算）。"""
    new: dict = {}
    targets = {_mirror_pos(p, fold) for p in fold.moving_cells}
    for q, stack in state.items():
        if q in targets:
            src = _mirror_pos(q, fold)
            if fold.placement == "over":
                block, rest = stack[: fold.moved_count], stack[fold.moved_count :]
            else:
                block, rest = stack[-fold.moved_count :], stack[: -fold.moved_count]
            new[q] = list(rest)
            new[src] = [_flip_layer(l, fold.axis) for l in reversed(block)]
        else:
            new[q] = list(stack)
    return new


# ---------------------------------------------------------------------------
# 折法程序
# ---------------------------------------------------------------------------


def _program_left(cols: int, rows: int, style: str) -> list[Fold]:
    """装订边在左时的折法程序（第一条折线平行于书脊）。

    除标准网格外还包含转置网格（2x3、2x4），供天头/地脚装订时换轴使用。
    """
    if (cols, rows) == (2, 2):  # 8 页/张
        return [Fold("V", 1, "left"), Fold("H", 1, "top")]
    if (cols, rows) == (3, 2):  # 12 页/张
        if style == "roll":  # 卷折（C 折）
            return [
                Fold("V", 2, "right"),
                Fold("V", 1, "right"),
                Fold("H", 1, "top"),
            ]
        # 风琴折（Z 折）
        return [
            Fold("V", 1, "left", "over"),
            Fold("V", 2, "right", "under"),
            Fold("H", 1, "top"),
        ]
    if (cols, rows) == (2, 3):  # 12 页/张（转置）
        if style == "roll":
            return [
                Fold("V", 1, "left"),
                Fold("H", 2, "bottom"),
                Fold("H", 1, "bottom"),
            ]
        return [
            Fold("V", 1, "left"),
            Fold("H", 1, "top", "over"),
            Fold("H", 2, "bottom", "under"),
        ]
    if (cols, rows) == (4, 2):  # 16 页/张
        if style == "standard":
            return [
                Fold("V", 2, "left"),
                Fold("V", 3, "left"),
                Fold("H", 1, "top"),
            ]
        return [
            Fold("V", 2, "left"),
            Fold("H", 1, "top"),
            Fold("V", 3, "left"),
        ]
    if (cols, rows) == (2, 4):  # 16 页/张（转置）
        return [
            Fold("V", 1, "left"),
            Fold("H", 2, "top"),
            Fold("H", 3, "top"),
        ]
    if (cols, rows) == (4, 4):  # 32 页/张
        if style == "standard":
            return [
                Fold("V", 2, "left"),
                Fold("V", 3, "left"),
                Fold("H", 2, "top"),
                Fold("H", 3, "top"),
            ]
        return [
            Fold("V", 2, "left"),
            Fold("H", 2, "top"),
            Fold("V", 3, "left"),
            Fold("H", 3, "top"),
        ]
    raise ValueError(f"无折法程序: {cols}x{rows}")


def _mirror_h(program: list[Fold], cols: int) -> list[Fold]:
    """左右镜像（装订边左 -> 右）。"""
    out = []
    for f in program:
        if f.axis == "V":
            moving = {"left": "right", "right": "left"}[f.moving]
            out.append(replace(f, line=cols - f.line, moving=moving))
        else:
            out.append(replace(f))
    return out


def _swap_axes(program: list[Fold]) -> list[Fold]:
    """横纵轴互换（装订边左/右 -> 上/下，用于转置网格）。"""
    m = {"left": "top", "right": "bottom", "top": "left", "bottom": "right"}
    return [
        Fold("H" if f.axis == "V" else "V", f.line, m[f.moving], f.placement)
        for f in program
    ]


def resolve_meta(program: list[Fold], cols: int, rows: int) -> list[Fold]:
    """正向预演折法程序，填充每步的 moving_cells / moved_count。"""
    counts = {(r, c): 1 for r in range(rows) for c in range(cols)}
    out: list[Fold] = []
    for f in program:
        moving = frozenset(p for p in counts if _in_moving(p, f))
        if not moving:
            raise ValueError(f"折步无移动区域: {f}")
        layer_counts = {counts[p] for p in moving}
        if len(layer_counts) != 1:
            raise ValueError(f"折步移动区域层数不一致: {f}")
        moved_count = layer_counts.pop()
        targets = {_mirror_pos(p, f) for p in moving}
        if targets & moving:
            raise ValueError(f"折步移动区域与目标区域重叠: {f}")
        new_counts = {p: v for p, v in counts.items() if p not in moving}
        for p in moving:
            q = _mirror_pos(p, f)
            new_counts[q] = new_counts.get(q, 0) + counts[p]
        counts = new_counts
        out.append(
            Fold(f.axis, f.line, f.moving, f.placement, moving, moved_count)
        )
    return out


def build_program(cols: int, rows: int, binding: str, style: str) -> list[Fold]:
    """按网格、装订边、折法生成完整折法程序（含元数据）。"""
    if binding in ("left", "right"):
        prog = _program_left(cols, rows, style)
        if binding == "right":
            prog = _mirror_h(prog, cols)
    else:  # top / bottom：在转置网格上生成后换轴
        prog = _program_left(rows, cols, style)
        if binding == "bottom":
            prog = _mirror_h(prog, rows)
        prog = _swap_axes(prog)
    return resolve_meta(prog, cols, rows)


# ---------------------------------------------------------------------------
# 拼版生成与校验
# ---------------------------------------------------------------------------


def generate_layout(
    cols: int,
    rows: int,
    program: list[Fold],
    page_base: int,
    total_pages: int,
) -> dict:
    """生成平面拼版：{位置: Layer}，每个位置一层，正面朝上。

    page_base: 本张纸起始页码（0 基）；超过 total_pages 的页为空白页(None)。
    """
    pages_per_sheet = cols * rows * 2

    def num(n: int) -> int | None:
        return n if n <= total_pages else None

    # 1) 符号化正向折叠（只跟踪正反面朝向与翻面奇偶，不赋页码）
    state = {
        (r, c): [Layer(up="F", cell=(r, c))] for r in range(rows) for c in range(cols)
    }
    for f in program:
        state = apply_fold(state, f)
    assert len(state) == 1, "折叠后应只剩一个位置"
    stack = next(iter(state.values()))
    assert len(stack) == pages_per_sheet // 2
    # 每个原始页格在正向折叠中的翻面奇偶（爬移方向映射用）
    parities = {layer.cell: (layer.pv, layer.ph) for layer in stack}

    # 2) 在折好状态下按阅读顺序赋页码：顶→底依次为奇数页朝上、偶数页朝下
    for k, layer in enumerate(stack):
        up_face = layer.front if layer.up == "F" else layer.back
        down_face = layer.back if layer.up == "F" else layer.front
        up_face.page = num(page_base + 2 * k + 1)
        up_face.head = HEAD_UP
        down_face.page = num(page_base + 2 * k + 2)
        down_face.head = HEAD_UP

    # 3) 反向展开回平面
    for f in reversed(program):
        state = apply_unfold(state, f)
    assert len(state) == cols * rows
    layout = {pos: stk[0] for pos, stk in state.items()}
    assert all(len(stk) == 1 for stk in state.values())
    assert all(l.up == "F" for l in layout.values()), "展开后应全部正面朝上"
    # 恢复正向折叠的翻面奇偶（展开过程已将其翻转回零）
    for pos, layer in layout.items():
        layer.pv, layer.ph = parities[pos]
    return layout


def verify_layout(
    layout: dict,
    program: list[Fold],
    page_base: int,
    total_pages: int,
) -> dict:
    """正向折叠生成的拼版，校验阅读顺序与页面朝向（倒页检查）。

    返回 {"ok": bool, "reading_order": [...], "upside_down": [...]}。
    """
    state = {pos: [layer] for pos, layer in layout.items()}
    for f in program:
        state = apply_fold(state, f)
    assert len(state) == 1
    stack = next(iter(state.values()))

    reading_order = []
    upside_down = []
    pages_per_sheet = len(stack) * 2
    for k, layer in enumerate(stack):
        up_face = layer.front if layer.up == "F" else layer.back
        down_face = layer.back if layer.up == "F" else layer.front
        expected_up = page_base + 2 * k + 1
        expected_down = page_base + 2 * k + 2
        exp_up = expected_up if expected_up <= total_pages else None
        exp_down = expected_down if expected_down <= total_pages else None
        reading_order.append(
            {
                "leaf": k + 1,
                "recto": up_face.page,
                "verso": down_face.page,
                "orientation": "upright" if up_face.head == HEAD_UP else "rotated",
            }
        )
        if up_face.page != exp_up or down_face.page != exp_down:
            return {
                "ok": False,
                "reading_order": reading_order,
                "upside_down": upside_down,
                "error": f"第 {k+1} 张叶页码顺序错误",
            }
        if up_face.head != HEAD_UP and up_face.page is not None:
            upside_down.append(up_face.page)
    return {"ok": not upside_down, "reading_order": reading_order,
            "upside_down": upside_down}
