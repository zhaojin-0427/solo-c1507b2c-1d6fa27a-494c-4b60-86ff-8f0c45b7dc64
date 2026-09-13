"""锁线打孔模板 SVG：逐帖书脊孔位图（含禁打区、支撑带、锁定/破损孔）。

输出确定性（固定精度、无随机/时间戳）。坐标原点在 SVG 画布左上角。
"""
from __future__ import annotations

SCALE_W = 920.0  # 面板目标宽度 px
PAD = 36.0
TITLE_H = 22.0
BAR_H = 16.0
PANEL_H = 112.0

LEGEND = [
    ("#222222", "有效孔"),
    ("#1d6fd1", "锁定孔"),
    ("#c1121f", "破损孔"),
    ("#999999", "跳过孔"),
    ("#8d99ae", "支撑带"),
    ("#e63946", "禁打区"),
]


def _f(v: float) -> str:
    return f"{v:.2f}"


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _legend(dy: float) -> list[str]:
    el = []
    x = SCALE_W - PAD - 6 * 82.0
    for color, label in LEGEND:
        el.append(
            f'<rect x="{_f(x)}" y="{_f(dy + 6)}" width="8" height="8" fill="{color}"/>'
        )
        el.append(
            f'<text x="{_f(x + 11)}" y="{_f(dy + 13)}" font-size="9" fill="#444" '
            f'font-family="sans-serif">{_esc(label)}</text>'
        )
        x += 82.0
    return el


def _panel(result: dict, sig: dict, dy: float) -> list[str]:
    spine = result["spine_length_mm"]
    head, tail = result["usable_range_mm"]
    s = (SCALE_W - 2 * PAD) / spine
    bar_y = dy + TITLE_H + 26.0
    cy = bar_y + BAR_H / 2
    direction = "头→尾" if sig["direction"] == "head_to_tail" else "尾→头"
    title = (
        f"帖{sig['index'] + 1} · 页{sig['page_start']}–{sig['page_end']} · "
        f"走线方向 {direction} · 有效孔 {len(sig['effective_holes_mm'])}"
    )
    el = [
        f'<text x="{_f(PAD)}" y="{_f(dy + TITLE_H * 0.72)}" font-size="13" '
        f'font-family="sans-serif" fill="#222">{_esc(title)}</text>',
    ]
    el.extend(_legend(dy))
    # 书脊条与头尾留量区
    el.append(
        f'<rect x="{_f(PAD)}" y="{_f(bar_y)}" width="{_f(spine * s)}" '
        f'height="{_f(BAR_H)}" fill="#fff" stroke="#333" stroke-width="1"/>'
    )
    el.append(
        f'<rect x="{_f(PAD)}" y="{_f(bar_y)}" width="{_f(head * s)}" '
        f'height="{_f(BAR_H)}" fill="#f0f0f0" stroke="none"/>'
    )
    el.append(
        f'<rect x="{_f(PAD + tail * s)}" y="{_f(bar_y)}" width="{_f((spine - tail) * s)}" '
        f'height="{_f(BAR_H)}" fill="#f0f0f0" stroke="none"/>'
    )
    el.append(
        f'<text x="{_f(PAD - 8)}" y="{_f(cy)}" font-size="9" fill="#666" '
        f'text-anchor="end" dominant-baseline="central" font-family="sans-serif">头</text>'
    )
    el.append(
        f'<text x="{_f(PAD + spine * s + 8)}" y="{_f(cy)}" font-size="9" fill="#666" '
        f'text-anchor="start" dominant-baseline="central" font-family="sans-serif">尾</text>'
    )
    # 禁打区
    for z in sig["forbidden_zones_mm"]:
        x = PAD + z["start_mm"] * s
        w = (z["end_mm"] - z["start_mm"]) * s
        el.append(
            f'<rect x="{_f(x)}" y="{_f(bar_y)}" width="{_f(w)}" height="{_f(BAR_H)}" '
            f'fill="#e63946" fill-opacity="0.22" stroke="none"/>'
        )
        el.append(
            f'<text x="{_f(x + w / 2)}" y="{_f(bar_y - 14)}" font-size="8" fill="#e63946" '
            f'text-anchor="middle" font-family="sans-serif">禁打区</text>'
        )
    # 支撑带
    for t in result["tapes"]:
        a1, a2 = t["anchors_mm"]
        x = PAD + a1 * s
        w = (a2 - a1) * s
        el.append(
            f'<rect x="{_f(x)}" y="{_f(bar_y - 4)}" width="{_f(w)}" '
            f'height="{_f(BAR_H + 8)}" fill="#8d99ae" fill-opacity="0.45" stroke="none"/>'
        )
        el.append(
            f'<text x="{_f(x + w / 2)}" y="{_f(bar_y - 8)}" font-size="8" fill="#5a6472" '
            f'text-anchor="middle" font-family="sans-serif">带{t["index"] + 1}</text>'
        )
    # 孔位
    eff = set(sig["effective_holes_mm"])
    locked = set(sig["locked_holes_mm"])
    skip = {sk["position_mm"]: sk["reason"] for sk in sig["skipped_holes_mm"]}
    for h in result["holes_mm"]:
        x = PAD + h * s
        if h in eff:
            if h in locked:
                el.append(
                    f'<circle cx="{_f(x)}" cy="{_f(cy)}" r="3.6" fill="none" '
                    f'stroke="#1d6fd1" stroke-width="0.9"/>'
                )
                el.append(f'<circle cx="{_f(x)}" cy="{_f(cy)}" r="2.0" fill="#1d6fd1"/>')
            else:
                el.append(f'<circle cx="{_f(x)}" cy="{_f(cy)}" r="2.6" fill="#222"/>')
        elif skip.get(h) == "damaged":
            el.append(
                f'<line x1="{_f(x - 3)}" y1="{_f(cy - 3)}" x2="{_f(x + 3)}" y2="{_f(cy + 3)}" '
                f'stroke="#c1121f" stroke-width="1.2"/>'
                f'<line x1="{_f(x - 3)}" y1="{_f(cy + 3)}" x2="{_f(x + 3)}" y2="{_f(cy - 3)}" '
                f'stroke="#c1121f" stroke-width="1.2"/>'
            )
        else:
            el.append(
                f'<circle cx="{_f(x)}" cy="{_f(cy)}" r="2.6" fill="none" '
                f'stroke="#999" stroke-width="0.8" stroke-dasharray="2,2"/>'
            )
        el.append(
            f'<text x="{_f(x)}" y="{_f(bar_y + BAR_H + 11)}" font-size="6.5" fill="#666" '
            f'text-anchor="middle" font-family="sans-serif">{h:g}</text>'
        )
    return el


def _svg_doc(width: float, height: float, body: list[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_f(width)}" height="{_f(height)}" '
        f'viewBox="0 0 {_f(width)} {_f(height)}">'
        + "".join(body)
        + "</svg>"
    )


def render_signature_template(result: dict, sig_index: int) -> str:
    """单帖打孔模板。"""
    sig = result["signatures"][sig_index]
    return _svg_doc(SCALE_W, PANEL_H, _panel(result, sig, 0.0))


def render_all_templates(result: dict) -> str:
    """整案打孔模板：逐帖自上而下排列。"""
    body: list[str] = []
    for i, sig in enumerate(result["signatures"]):
        body.extend(_panel(result, sig, i * PANEL_H))
    return _svg_doc(SCALE_W, PANEL_H * len(result["signatures"]), body)
