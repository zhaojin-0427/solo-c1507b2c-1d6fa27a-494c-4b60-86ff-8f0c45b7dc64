"""SVG 预览：印张正反面拼版示意图。输出确定性（固定精度、无随机/时间戳）。"""
from __future__ import annotations

from .imposition import effective_printable
from .models import JobInput

SCALE_TARGET = 920.0  # 面板目标宽度 px
PAD = 24.0
TITLE_H = 22.0

DIR_FROM_ROT = {0: (0, -1), 90: (1, 0), 180: (0, 1), 270: (-1, 0)}


def _f(v: float) -> str:
    return f"{v:.2f}"


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _mark(x: float, y: float, s: float) -> str:
    r = 2.2 * s
    g = 1.0 * s
    return (
        f'<circle cx="{_f(x)}" cy="{_f(y)}" r="{_f(r)}" fill="none" stroke="#000" stroke-width="0.6"/>'
        f'<line x1="{_f(x - r - g)}" y1="{_f(y)}" x2="{_f(x + r + g)}" y2="{_f(y)}" stroke="#000" stroke-width="0.6"/>'
        f'<line x1="{_f(x)}" y1="{_f(y - r - g)}" x2="{_f(x)}" y2="{_f(y + r + g)}" stroke="#000" stroke-width="0.6"/>'
    )


def _cell_elements(cell: dict, s: float, ox: float, oy: float) -> list[str]:
    el = []
    x = ox + cell["x"] * s
    y = oy + cell["y"] * s
    w = cell["width"] * s
    h = cell["height"] * s
    bb = cell["bleed_box"]
    sb = cell["safe_box"]
    # 出血框
    el.append(
        f'<rect x="{_f(ox + bb["x"] * s)}" y="{_f(oy + bb["y"] * s)}" '
        f'width="{_f(bb["width"] * s)}" height="{_f(bb["height"] * s)}" '
        f'fill="none" stroke="#d63384" stroke-width="0.5" stroke-dasharray="3,2"/>'
    )
    # 成品（裁切）框
    el.append(
        f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" height="{_f(h)}" '
        f'fill="none" stroke="#222" stroke-width="0.8"/>'
    )
    # 安全框
    el.append(
        f'<rect x="{_f(ox + sb["x"] * s)}" y="{_f(oy + sb["y"] * s)}" '
        f'width="{_f(sb["width"] * s)}" height="{_f(sb["height"] * s)}" '
        f'fill="none" stroke="#2a9d8f" stroke-width="0.5" stroke-dasharray="1.5,1.5"/>'
    )
    # 爬移补偿指示（内容框随补偿平移）
    cr = cell["creep"]
    if abs(cr["dx"]) > 1e-9 or abs(cr["dy"]) > 1e-9:
        el.append(
            f'<rect x="{_f(x + cr["dx"] * s)}" y="{_f(y + cr["dy"] * s)}" '
            f'width="{_f(w)}" height="{_f(h)}" fill="none" stroke="#1d6fd1" '
            f'stroke-width="0.5" stroke-dasharray="4,2"/>'
        )
    cx, cy = x + w / 2, y + h / 2
    rot = cell["rotation"]
    page = cell["page"]
    label = "空" if page is None else str(page)
    color = "#999" if page is None else "#111"
    el.append(
        f'<text x="{_f(cx)}" y="{_f(cy)}" font-size="{_f(min(w, h) * 0.28)}" '
        f'fill="{color}" text-anchor="middle" dominant-baseline="central" '
        f'font-family="sans-serif" transform="rotate({rot} {_f(cx)} {_f(cy)})">{label}</text>'
    )
    # 页头方向箭头
    dx, dy = DIR_FROM_ROT[rot]
    ax = cx + dx * w * 0.36
    ay = cy + dy * h * 0.36
    el.append(
        f'<line x1="{_f(cx)}" y1="{_f(cy)}" x2="{_f(ax)}" y2="{_f(ay)}" '
        f'stroke="#e76f51" stroke-width="0.9" marker-end="url(#ah)"/>'
    )
    return el


def _panel(result: dict, sig: dict, sheet: dict, side: str, s: float, dy: float) -> tuple[list[str], float, float]:
    """绘制一个印张面，返回 (元素, 宽px, 高px)。"""
    job = result["job"]
    paper_w = job["paper"]["width"]
    paper_h = job["paper"]["height"]
    pw = paper_w * s + 2 * PAD
    ph = paper_h * s + TITLE_H + 2 * PAD
    ox = PAD
    oy = dy + TITLE_H + PAD
    side_text = "正面" if side == "front" else "背面"
    title = (
        f"帖{sig['index'] + 1} · 印张{sheet['index'] + 1} · {side_text}"
        f"（{sig['spec']['pages']}页/{sig['spec']['style']}，爬移 {sheet['creep_offset_mm']}mm）"
    )
    el = [
        f'<text x="{_f(PAD)}" y="{_f(dy + TITLE_H * 0.75)}" font-size="13" '
        f'font-family="sans-serif" fill="#222">{_esc(title)}</text>',
        f'<rect x="{_f(ox)}" y="{_f(oy)}" width="{_f(paper_w * s)}" height="{_f(paper_h * s)}" '
        f'fill="#fff" stroke="#333" stroke-width="1"/>',
    ]
    # 可印区域
    job_obj = JobInput(**job)
    pr = effective_printable(job_obj)
    el.append(
        f'<rect x="{_f(ox + pr.x * s)}" y="{_f(oy + pr.y * s)}" width="{_f(pr.width * s)}" '
        f'height="{_f(pr.height * s)}" fill="none" stroke="#999" stroke-width="0.6" '
        f'stroke-dasharray="5,3"/>'
    )
    # 咬口
    g = job["press"]["gripper_mm"]
    if g > 0:
        edge = job["press"]["gripper_edge"]
        if edge == "bottom":
            gx, gy, gw, gh = 0, paper_h - g, paper_w, g
        elif edge == "top":
            gx, gy, gw, gh = 0, 0, paper_w, g
        elif edge == "left":
            gx, gy, gw, gh = 0, 0, g, paper_h
        else:
            gx, gy, gw, gh = paper_w - g, 0, g, paper_h
        el.append(
            f'<rect x="{_f(ox + gx * s)}" y="{_f(oy + gy * s)}" width="{_f(gw * s)}" '
            f'height="{_f(gh * s)}" fill="#ffcccc" fill-opacity="0.6" stroke="none"/>'
        )
        el.append(
            f'<text x="{_f(ox + (gx + gw / 2) * s)}" y="{_f(oy + (gy + gh / 2) * s)}" '
            f'font-size="9" fill="#c00" text-anchor="middle" font-family="sans-serif">咬口</text>'
        )
    # 折线（仅正面）
    if side == "front":
        for fl in sheet["fold_lines"]:
            if fl["axis"] == "V":
                x = ox + fl["at_mm"] * s
                el.append(
                    f'<line x1="{_f(x)}" y1="{_f(oy)}" x2="{_f(x)}" y2="{_f(oy + paper_h * s)}" '
                    f'stroke="#e63946" stroke-width="0.7" stroke-dasharray="6,3"/>'
                )
                el.append(
                    f'<text x="{_f(x + 2)}" y="{_f(oy + 10)}" font-size="9" fill="#e63946" '
                    f'font-family="sans-serif">折{fl["step"]}</text>'
                )
            else:
                y = oy + fl["at_mm"] * s
                el.append(
                    f'<line x1="{_f(ox)}" y1="{_f(y)}" x2="{_f(ox + paper_w * s)}" y2="{_f(y)}" '
                    f'stroke="#e63946" stroke-width="0.7" stroke-dasharray="6,3"/>'
                )
                el.append(
                    f'<text x="{_f(ox + 2)}" y="{_f(y + 10)}" font-size="9" fill="#e63946" '
                    f'font-family="sans-serif">折{fl["step"]}</text>'
                )
    # 页格
    for cell in sheet[side]["cells"]:
        el.extend(_cell_elements(cell, s, ox, oy))
    # 套准标记
    for m in sheet["marks"]:
        el.append(_mark(ox + m["x"] * s, oy + m["y"] * s, s))
    # 书脊配帖标（该帖最外层印张的对应面）
    for cm in result.get("collating_marks", {}).get("marks", []):
        if (
            cm["signature"] == sig["index"]
            and cm["sheet"] == sheet["index"]
            and cm["side"] == side
        ):
            el.append(_collating_mark(cm, s, ox, oy))
    return el, pw, ph


def _collating_mark(cm: dict, s: float, ox: float, oy: float) -> str:
    """绘制一枚配帖标：黑色实底矩形 + 帖号标注（title 含全部制版信息）。"""
    x = ox + cm["x"] * s
    y = oy + cm["y"] * s
    w = cm["width"] * s
    h = cm["height"] * s
    side_text = "正面" if cm["side"] == "front" else "背面"
    tip = (
        f"帖{cm['mark_number']} 配帖标 · 页{cm['page_start']}-{cm['page_end']} · "
        f"印张{cm['sheet'] + 1} · {side_text} · "
        f"({cm['x']}, {cm['y']}) {cm['width']}×{cm['height']}mm · "
        f"旋转{cm['rotation']}° · 书脊 {cm['spine_start_mm']}-{cm['spine_end_mm']}mm"
    )
    cx, cy = x + w / 2, y + h / 2
    return (
        f'<g><title>{_esc(tip)}</title>'
        f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" height="{_f(h)}" '
        f'fill="#111" stroke="none"/>'
        f'<text x="{_f(cx)}" y="{_f(cy)}" font-size="{_f(max(4.0, min(w, h) * 0.45))}" '
        f'fill="#fff" text-anchor="middle" dominant-baseline="central" '
        f'font-family="sans-serif" transform="rotate({cm["rotation"]} {_f(cx)} {_f(cy)})">'
        f'{cm["mark_number"]}</text></g>'
    )


def _svg_doc(width: float, height: float, body: list[str]) -> str:
    defs = (
        '<defs><marker id="ah" markerWidth="6" markerHeight="6" refX="4" refY="2" '
        'orient="auto"><path d="M0,0 L5,2 L0,4 z" fill="#e76f51"/></marker></defs>'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_f(width)}" height="{_f(height)}" '
        f'viewBox="0 0 {_f(width)} {_f(height)}">{defs}'
        + "".join(body)
        + "</svg>"
    )


COLL_PANEL_PAD_TOP = 26.0
COLL_ROW_H = 26.0
COLL_PANEL_BOTTOM = 30.0


def _collating_panel(result: dict, dy: float) -> tuple[list[str], float]:
    """书脊配帖标阶梯面板：书脊条 + 逐帖阶梯黑标（按书脊顺序标注帖号）。

    返回 (元素, 面板高px)。横向为沿书脊方向，纵向为列（向书芯内错开）。
    """
    coll = result["collating_marks"]
    cfg = result["job"]["collating_marks"]
    spine = coll["spine_length_mm"]
    lo, hi = coll["usable_range_mm"]
    s = (SCALE_TARGET - 2 * PAD) / spine
    n_cols = max((m["column"] for m in coll["marks"]), default=0) + 1
    row_h = max(COLL_ROW_H, cfg["mark_width_mm"] * s + 8.0)
    bar_h = n_cols * row_h
    bar_y = dy + COLL_PANEL_PAD_TOP
    height = COLL_PANEL_PAD_TOP + bar_h + COLL_PANEL_BOTTOM
    title = (
        f"书脊配帖标阶梯 · 书脊长 {spine}mm · 可用 [{lo}, {hi}]mm · "
        f"共 {len(coll['marks'])} 帖"
    )
    el = [
        f'<text x="{_f(PAD)}" y="{_f(dy + 16)}" font-size="13" '
        f'font-family="sans-serif" fill="#222">{_esc(title)}</text>',
        f'<rect x="{_f(PAD)}" y="{_f(bar_y)}" width="{_f(spine * s)}" '
        f'height="{_f(bar_h)}" fill="#fff" stroke="#333" stroke-width="1"/>',
    ]
    # 两端安全余量区
    if lo > 0:
        el.append(
            f'<rect x="{_f(PAD)}" y="{_f(bar_y)}" width="{_f(lo * s)}" '
            f'height="{_f(bar_h)}" fill="#f0f0f0" stroke="none"/>'
        )
    if hi < spine:
        el.append(
            f'<rect x="{_f(PAD + hi * s)}" y="{_f(bar_y)}" '
            f'width="{_f((spine - hi) * s)}" height="{_f(bar_h)}" '
            f'fill="#f0f0f0" stroke="none"/>'
        )
    el.append(
        f'<text x="{_f(PAD - 8)}" y="{_f(bar_y + bar_h / 2)}" font-size="9" fill="#666" '
        f'text-anchor="end" dominant-baseline="central" font-family="sans-serif">头</text>'
    )
    el.append(
        f'<text x="{_f(PAD + spine * s + 8)}" y="{_f(bar_y + bar_h / 2)}" font-size="9" '
        f'fill="#666" text-anchor="start" dominant-baseline="central" '
        f'font-family="sans-serif">尾</text>'
    )
    # 阶梯黑标（按书脊顺序）
    mark_h = cfg["mark_height_mm"] * s
    mark_w = cfg["mark_width_mm"] * s
    for p in coll["spine_pattern"]:
        x = PAD + p["spine_start_mm"] * s
        y = bar_y + p["column"] * row_h + (row_h - mark_w) / 2
        el.append(
            f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(mark_h)}" '
            f'height="{_f(mark_w)}" fill="#111" stroke="none"/>'
        )
        el.append(
            f'<text x="{_f(x + mark_h / 2)}" y="{_f(bar_y + bar_h + 12)}" font-size="8" '
            f'fill="#444" text-anchor="middle" font-family="sans-serif">'
            f'帖{p["mark_number"]}</text>'
        )
    return el, height


def render_overview(result: dict) -> str:
    """整案预览：所有印张正反面依次排列（配置配帖标时前置书脊阶梯面板）。"""
    paper = result["job"]["paper"]
    s = SCALE_TARGET / max(paper["width"], paper["height"])
    panels: list[tuple[dict, dict, str]] = []
    for sig in result["signatures"]:
        for sheet in sig["sheets"]:
            panels.append((sig, sheet, "front"))
            panels.append((sig, sheet, "back"))
    body: list[str] = []
    y = 0.0
    width = 0.0
    if "collating_marks" in result:
        el, ph = _collating_panel(result, y)
        body.extend(el)
        y += ph
        width = max(width, SCALE_TARGET)
    for sig, sheet, side in panels:
        el, pw, ph = _panel(result, sig, sheet, side, s, y)
        body.extend(el)
        y += ph
        width = max(width, pw)
    return _svg_doc(width, y, body)


def render_sheet_side(result: dict, sig_index: int, sheet_index: int, side: str) -> str:
    """单个印张面预览。"""
    paper = result["job"]["paper"]
    s = SCALE_TARGET / max(paper["width"], paper["height"])
    sig = result["signatures"][sig_index]
    sheet = sig["sheets"][sheet_index]
    el, pw, ph = _panel(result, sig, sheet, side, s, 0.0)
    return _svg_doc(pw, ph, el)
