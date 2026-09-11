"""折叠引擎测试：所有页数/折法/装订边组合的拼版正确性。"""
import pytest

from app.folding import (
    GRIDS,
    STYLES,
    build_program,
    generate_layout,
    verify_layout,
)

ALL_COMBOS = [
    (pages, style, binding)
    for pages in GRIDS
    for style in sorted(STYLES[pages])
    for binding in ("left", "right", "top", "bottom")
]


@pytest.mark.parametrize("pages,style,binding", ALL_COMBOS)
def test_layout_pages_complete(pages, style, binding):
    """每个页格正反面页码合起来恰好是 1..N（不重不缺）。"""
    cols, rows = GRIDS[pages]
    prog = build_program(cols, rows, binding, style)
    layout = generate_layout(cols, rows, prog, 0, 999)
    assert len(layout) == cols * rows
    got = []
    for layer in layout.values():
        got += [layer.front.page, layer.back.page]
    assert sorted(got) == list(range(1, pages + 1))


@pytest.mark.parametrize("pages,style,binding", ALL_COMBOS)
def test_reading_order_and_orientation(pages, style, binding):
    """折叠后阅读顺序 1..N，所有页头朝上（不倒页）。"""
    cols, rows = GRIDS[pages]
    prog = build_program(cols, rows, binding, style)
    layout = generate_layout(cols, rows, prog, 0, 999)
    ver = verify_layout(layout, prog, 0, 999)
    assert ver["ok"], ver
    assert ver["upside_down"] == []
    seq = []
    for leaf in ver["reading_order"]:
        seq += [leaf["recto"], leaf["verso"]]
        assert leaf["orientation"] == "upright"
    assert seq == list(range(1, pages + 1))


@pytest.mark.parametrize("pages,style,binding", ALL_COMBOS)
def test_leaf_pairs_share_cell(pages, style, binding):
    """每格正/反页码配成叶对，叶对应互不相同且覆盖全部 N/2 叶。"""
    cols, rows = GRIDS[pages]
    prog = build_program(cols, rows, binding, style)
    layout = generate_layout(cols, rows, prog, 0, 999)
    pairs = set()
    for layer in layout.values():
        a, b = sorted((layer.front.page, layer.back.page))
        pairs.add((a, b))
    assert len(pairs) == pages // 2


@pytest.mark.parametrize("pages,style,binding", ALL_COMBOS)
def test_blank_pages_at_end(pages, style, binding):
    """总页数不足时，空白页出现在书帖末尾且页码为 None。"""
    cols, rows = GRIDS[pages]
    total = pages - 2  # 最后两页空白
    prog = build_program(cols, rows, binding, style)
    layout = generate_layout(cols, rows, prog, 0, total)
    ver = verify_layout(layout, prog, 0, total)
    assert ver["ok"]
    seq = []
    for leaf in ver["reading_order"]:
        seq += [leaf["recto"], leaf["verso"]]
    assert seq[: total] == list(range(1, total + 1))
    assert seq[total:] == [None, None]


def test_known_8page_layout():
    """8 页标准折（左装订）的经典版面：正面 2/3 与 6/7 头对头。"""
    prog = build_program(2, 2, "left", "standard")
    layout = generate_layout(2, 2, prog, 0, 999)
    fronts = {pos: l.front.page for pos, l in layout.items()}
    backs = {pos: l.back.page for pos, l in layout.items()}
    # 正面页码集合 = {2,3,6,7}，背面 = {1,4,5,8}
    assert sorted(fronts.values()) == [2, 3, 6, 7]
    assert sorted(backs.values()) == [1, 4, 5, 8]
