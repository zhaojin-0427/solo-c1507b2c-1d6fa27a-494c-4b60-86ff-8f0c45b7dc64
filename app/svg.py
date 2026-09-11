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
    return el, pw, ph


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


def render_overview(result: dict) -> str:
    """整案预览：所有印张正反面依次排列。"""
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
