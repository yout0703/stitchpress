"""Independent reading raster; production geometry and pixels never inherit reading DPI."""

from dataclasses import replace
from decimal import Decimal
from math import ceil

from .layout import page_plan, px
from .render import Renderer


def reading_layout(renderer, reference):
    fabric = replace(renderer.fabric, dpi=reference.dpi or renderer.fabric.dpi)
    scale = reference.scale
    if reference.cell_mm is not None:
        fabric = replace(
            fabric, sx=str(Decimal(reference.cell_mm) / 10), sy=str(Decimal(reference.cell_mm) / 10)
        )
        scale = "1"
    reading = Renderer(
        renderer.design,
        renderer.library,
        renderer.mapping,
        fabric,
        code=renderer.code,
        font_data=renderer.font_data,
        title=renderer.title,
        text_layers={k: vars(v) for k, v in renderer.text_layers.items()},
        font_assets=renderer.font_assets,
    )
    # Metadata describes the real cloth; only the reading ruler uses reading pitch.
    from .text import values_for

    reading._text_values = values_for(
        renderer.design, renderer.fabric, code=renderer.code, title=renderer.title
    )
    paper = reference.paper_cm
    if reference.paper == "whole":
        factor, mag = fabric.dpi / 300, Decimal(scale)
        margins = reference.margins_mm or ("12.7",) * 4
        left, top, right, bottom = [px(Decimal(v) / 10, fabric.dpi) for v in margins]
        entry = ceil(reading.legend_entry_width() * factor)
        pw = max(
            left
            + right
            + px(Decimal(fabric.sx) * ceil(renderer.design.width / 10) * 10 * mag, fabric.dpi)
            + 50,
            left + right + 6 * entry,
        )
        columns = min(6, (pw - left - right) // entry)
        ph = (
            top
            + bottom
            + ceil((135 + 80 + 180) * factor)
            + ceil(len(renderer.mapping) / columns) * ceil(62 * factor)
            + px(Decimal(fabric.sy) * ceil(renderer.design.height / 10) * 10 * mag, fabric.dpi)
            + 50
        )
        paper = tuple(str(Decimal(n) * Decimal("2.54") / fabric.dpi) for n in (pw, ph))
    plan = page_plan(
        renderer.design.width,
        renderer.design.height,
        len(renderer.mapping),
        fabric,
        paper_cm=paper,
        overlap=reference.overlap,
        scale=scale,
        legend_entry_width=reading.legend_entry_width(),
        margins_mm=reference.margins_mm,
    )
    return reading, plan
