from dataclasses import asdict, replace
from math import ceil

import pytest
from PIL import ImageDraw

from stitchpress.app.fixtures import demo_project
from stitchpress.app.reference_package import render_context
from stitchpress.core.layout import Fabric
from stitchpress.core.models import Design
from stitchpress.core.render import ruler_label
from stitchpress.core.streaming import save_production_stream


def narrow_project():
    project = demo_project(109, 106, 16)
    design = Design.from_dict(project.entities["design"])
    design = replace(
        design, palette=tuple(replace(c, code=str(3800 + i)) for i, c in enumerate(design.palette))
    )
    project.entities["design"] = design.to_dict()
    project.entities["fabric"] = asdict(
        replace(Fabric.theoretical("14"), left="3", right="3", top="3", bottom="3")
    )
    return project


@pytest.mark.parametrize("footer", [False, True])
def test_narrow_margin_keeps_all_legends_clear_of_chart_and_each_other(footer, monkeypatch):
    project = narrow_project()
    project.entities["text_layers"] = {
        "production_bottom": {"enabled": footer, "template": "CT242"}
    }
    _, renderer = render_context(project)
    boxes = []
    original = renderer.text

    def capture(image, xy, text, size, anchor="lt"):
        box = ImageDraw.Draw(image).textbbox(
            xy, text, font=renderer.font(ceil(size * renderer.fabric.dpi / 300)), anchor=anchor
        )
        boxes.append((text, size, box))
        return original(image, xy, text, size, anchor)

    monkeypatch.setattr(renderer, "text", capture)
    image, raster = renderer.production()
    assert image.size == (3044, 2980)
    assert renderer.verify_cells(image, raster)["stitches"] == sum(renderer.design.counts)
    codes = [entry for entry in boxes if entry[1] == 56]
    assert {t for t, _, _ in codes} == {c.code for c in renderer.design.palette}
    assert len(codes) == 16
    chart = (raster.xs[0], raster.ys[0], raster.xs[-1], raster.ys[-1])

    def separate(a, b):
        return a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]

    for text, _, box in codes:
        assert separate(box, chart)
        for other, _, other_box in boxes:
            if other != text:
                assert separate(box, other_box)
    image.close()


def test_disabled_footer_matches_absent_footer_and_stream_matches_pixels(tmp_path):
    project = narrow_project()
    _, renderer = render_context(project)
    before, _ = renderer.production()
    project.entities["text_layers"] = {"production_bottom": {"enabled": False}}
    _, renderer = render_context(project)
    after, _ = renderer.production()
    assert after.tobytes() == before.tobytes()
    checks = save_production_stream(renderer, tmp_path / "narrow.tif")
    from PIL import Image

    with Image.open(tmp_path / "narrow.tif") as actual:
        assert actual.tobytes() == after.tobytes()
    assert checks["stitches"] == sum(renderer.design.counts)
    before.close()
    after.close()


@pytest.mark.parametrize("dpi", [72, 300, 600])
def test_small_theoretical_ct_chart_prints_without_repeating_decimal_overflow(dpi, tmp_path):
    project = demo_project(13, 18, 3)
    project.entities["metadata"]["code"] = "OXS-001"
    project.entities["fabric"] = asdict(
        replace(Fabric.theoretical("14"), left="3", right="3", top="3", bottom="3", dpi=dpi)
    )
    _, renderer = render_context(project)
    assert ruler_label(13, renderer.fabric.sx) == "13 cells = 2.3586 cm"
    image, raster = renderer.production()
    assert renderer.verify_cells(image, raster)["stitches"] == sum(renderer.design.counts)
    # Display rounding must not feed back into physical grid geometry.
    from decimal import Decimal

    from stitchpress.core.layout import px

    assert raster.xs[-1] == px(Decimal("3") + Decimal(renderer.fabric.sx) * 13, dpi)
    if dpi == 300:
        from PIL import Image

        save_production_stream(renderer, tmp_path / "small.tif")
        with Image.open(tmp_path / "small.tif") as streamed:
            assert streamed.tobytes() == image.tobytes()
    image.close()
