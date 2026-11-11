"""Actual reference-target previews in an isolated worker; never writes an active project."""

from dataclasses import replace
from pathlib import Path

from stitchpress.core.cards import symbol_pages
from stitchpress.core.commerce import TARGETS, CommerceRenderer, CommerceSettings
from stitchpress.core.errors import StitchPressError
from stitchpress.core.reading import reading_layout
from stitchpress.core.render import MAX_RENDER_PIXELS, screen_rgb, stitch_preview

from .reference_package import render_context


def preview_reference(project, target, output, number=1):
    context, renderer = render_context(project)
    reading, plan = reading_layout(renderer, context["reference"])
    checks = dict(target=target, page=number, partial=False)
    if target in TARGETS:
        composer = CommerceRenderer(
            context,
            CommerceSettings(**project.entities.get("commerce", {})),
            project.assets,
            demo=True,
        )
        try:
            image, extra = composer.render(target)
            checks.update(extra)
        finally:
            composer.subject.close()
    elif target == "effect":
        image = stitch_preview(
            context["design"],
            cell=project.entities.get("post", {}).get("effect_cell", 18),
            style="dot",
        )
    elif target == "production":
        w, h = renderer.layout.canvas_px
        if w * h > MAX_RENDER_PIXELS:
            # A true first band, explicitly identified, not a downscaled stand-in.
            surface, _ = renderer.production(band=(0, min(h, 512)))
            image = surface.image
            checks.update(partial=True, band=(0, min(h, 512)), full_size=(w, h))
        else:
            image, raster = renderer.production()
            checks.update(renderer.verify_cells(image, raster))
    elif target in {"whole", "pages"}:
        if target == "whole":
            reading, plan = reading_layout(
                renderer, replace(context["reference"], paper="whole", overlap=0, landscape=False)
            )
        if type(number) is not int or not 1 <= number <= len(plan.pages):
            raise StitchPressError("查图页码越界")
        image, raster = reading.reference(
            plan.pages[number - 1],
            plan,
            style=context["reference"].style,
            minor_grid=context["reference"].minor_grid,
        )
        checks.update(
            reading.verify_cells(image, raster), pages=len(plan.pages), dpi=reading.fabric.dpi
        )
    elif target in {"symbols", "card"}:
        image = None
        for index, candidate in enumerate(symbol_pages(reading, target), 1):
            if index == number:
                image = candidate
                break
            candidate.close()
        if image is None:
            raise StitchPressError("符号表/速查卡页码越界")
    else:
        raise StitchPressError("未知参考预览目标")
    rgb = screen_rgb(image, context["library"].icc if image.mode == "CMYK" else None)
    image.close()
    path = Path(output)
    rgb.save(path, "PNG")
    checks["preview_size"] = rgb.size
    rgb.close()
    return dict(preview=str(path), checks=checks)
