from dataclasses import asdict

import numpy as np
import pytest
from PIL import Image

from stitchpress.app.fixtures import demo_project
from stitchpress.core.errors import Cancelled
from stitchpress.core.layout import Fabric
from stitchpress.core.models import Design
from stitchpress.core.render import Renderer
from stitchpress.core.resources import assign_symbols, demo_library
from stitchpress.core.streaming import save_production_stream
from stitchpress.core.text import TextLayer


@pytest.mark.parametrize("custom_text", [False, True])
def test_streaming_matches_full_pixels_and_checks_every_cell_across_strip_boundaries(
    tmp_path, custom_text
):
    design = Design.from_dict(demo_project(48, 64, 8).entities["design"])
    library = demo_library(8)
    renderer = Renderer(
        design,
        library,
        assign_symbols(design, library),
        Fabric(),
        code="AK0026",
        text_layers={"production_top": asdict(TextLayer(template="Test {code}", align="right"))}
        if custom_text
        else {},
    )
    full, raster = renderer.production()
    expected = np.asarray(full)
    target = tmp_path / "stream.tif"
    checks = save_production_stream(renderer, target)
    assert checks["stitches"] == sum(design.counts) and checks["strips"] > 2
    assert checks["maximum_band_bytes"] <= 8 * 1024**2
    with Image.open(target) as actual:
        assert actual.mode == "CMYK"
        assert np.array_equal(np.asarray(actual), expected)
        assert renderer.verify_cells(actual, raster)["counts"] == list(design.counts)
    full.close()


def test_streamed_tiff_cancellation_is_explicit(tmp_path, minimal):
    library = demo_library(2)
    renderer = Renderer(minimal, library, assign_symbols(minimal, library), Fabric())
    with pytest.raises(Cancelled):
        save_production_stream(renderer, tmp_path / "cancel.tif", cancel=lambda: True)
