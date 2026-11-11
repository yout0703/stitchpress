"""Explicit local scale probe; self-authored inputs, sequential rendering, no customer assets."""

import argparse
import json
import resource
import time
from pathlib import Path

from PIL import Image

from stitchpress.app.fixtures import demo_project, sample_design
from stitchpress.app.reference_package import render_context
from stitchpress.core.configuration import ReferenceSettings
from stitchpress.core.reading import reading_layout
from stitchpress.core.render import Renderer
from stitchpress.core.resources import assign_symbols


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    import stitchpress.core.render as render_module

    # Probe an increased bound before release; final configuration must be checked separately.
    before_limit = render_module.MAX_RENDER_PIXELS
    render_module.MAX_RENDER_PIXELS = 80_000_000
    started = time.monotonic()
    _, base = render_context(demo_project())
    small = ReferenceSettings(dpi=72, cell_mm="10")
    _, plan = reading_layout(base, small)
    design = sample_design(plan.capacity[0] * 10, plan.capacity[1] * 20, 8)
    renderer = Renderer(design, base.library, assign_symbols(design, base.library), base.fabric)
    reading, plan = reading_layout(renderer, small)
    assert len(plan.pages) == 200
    stitches = 0
    for page in plan.pages:
        image, raster = reading.reference(page, plan)
        checks = reading.verify_cells(image, raster)
        stitches += checks["stitches"]
        path = args.output / "last-page.jpg"
        image.convert("RGB").save(path, "JPEG", quality=95)
        with Image.open(path) as actual:
            actual.load()
            assert actual.size == plan.paper_px
        image.close()
    assert stitches == sum(design.counts)
    pages_seconds = time.monotonic() - started
    # An actual 80 MP reading raster, not just an allocation or patched output size.
    big = ReferenceSettings(
        paper="custom", custom_width_cm="67.7333333333", custom_height_cm="84.6666666667"
    )
    reading, plan = reading_layout(base, big)
    assert plan.paper_px == (8000, 10000)
    image, raster = reading.reference(plan.pages[0], plan)
    assert reading.verify_cells(image, raster)["stitches"] == sum(base.design.counts)
    path = args.output / "80mp-reading.tif"
    image.save(path, "TIFF", compression="tiff_lzw", dpi=(300, 300))
    image.close()
    with Image.open(path) as actual:
        actual.load()
        assert actual.size == (8000, 10000)
        assert reading.verify_cells(actual, raster)["stitches"] == sum(base.design.counts)
    # The boundary still refuses one extra pixel row before allocation.
    try:
        reading.canvas((8000, 10001))
    except Exception as exc:
        rejection = str(exc)
    else:
        raise AssertionError("pixel bound missing")
    report = dict(
        pages=200,
        stitches=stitches,
        pages_seconds=round(pages_seconds, 3),
        page_size=(8000, 10000),
        seconds=round(time.monotonic() - started, 3),
        peak_rss_host_units=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        original_pixel_limit=before_limit,
        tested_pixel_limit=80_000_000,
        overflow_rejected=rejection,
        windows="not-tested",
        factory="not-tested",
    )
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
