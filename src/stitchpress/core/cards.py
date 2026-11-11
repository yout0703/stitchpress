"""Separate symbol-table and quick-card layouts with the active mapping and text."""

from math import ceil

from .errors import StitchPressError
from .layout import decimal, px


def symbol_pages(renderer, kind):
    if kind not in {"symbols", "card"}:
        raise StitchPressError("未知符号图目标")
    factor = renderer.fabric.dpi / 300

    def sc(v):
        return ceil(v * factor)

    size = tuple(px(decimal(v), renderer.fabric.dpi) for v in ("21", "29.7"))
    font = renderer.font(sc(39))
    code_width = max(font.getlength(c.code) for _, c in renderer.entries)
    entry = max(sc(345), ceil(code_width) + sc(185))
    cols = min(6 if kind == "card" else 3, (size[0] - sc(300)) // entry)
    if cols < 1:
        raise StitchPressError("线号/字号无法放入符号表，请调整纸张或字体")
    row_h = sc(88 if kind == "card" else 150)
    rows = (size[1] - sc(540)) // row_h
    if rows < 1:
        raise StitchPressError("符号表没有可用行")
    capacity = cols * rows
    for offset in range(0, len(renderer.entries), capacity):
        image = renderer.canvas(size)
        renderer.text(
            image,
            (sc(150), sc(60)),
            f"{renderer.code} | " + ("Quick Symbol Card" if kind == "card" else "Symbol Table"),
            34,
        )
        renderer.text(
            image, (sc(150), sc(150)), f"No. / Symbol / DMC | {offset // capacity + 1}", 32
        )
        col_w = (size[0] - sc(300)) // cols
        for number, (index, color) in enumerate(
            renderer.entries[offset : offset + capacity], offset
        ):
            col, row = divmod(number - offset, rows)
            x = sc(150) + col * col_w
            y = sc(300) + row * row_h
            renderer.text(image, (x, y + sc(12)), str(number + 1), 36)
            side = sc(42 if kind == "card" else 84)
            image.paste(renderer.tile(index, side, side), (x + sc(65), y))
            renderer.text(image, (x + sc(65) + side + sc(20), y + sc(8)), color.code, 39)
        renderer.text(
            image,
            (sc(150), size[1] - sc(135)),
            f"{sum(renderer.design.counts)} full crosses | {len(renderer.entries)} colours",
            32,
        )
        renderer.custom_text(image, "symbols")
        yield image
