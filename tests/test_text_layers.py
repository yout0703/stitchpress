from dataclasses import asdict
from pathlib import Path

import pytest

from stitchpress.app.export import export_package
from stitchpress.core.errors import StitchPressError
from stitchpress.core.layout import Fabric, page_plan
from stitchpress.core.render import Renderer
from stitchpress.core.resources import assign_symbols, bundled_font, demo_library
from stitchpress.core.text import TextLayer, checked_font, expand


def test_placeholders_are_allowlisted_and_not_expressions():
    assert expand("{code} / {{literal}}", {"code": "AK0026"}) == "AK0026 / {literal}"
    for value in ("{code.__class__}", "{code!r}", "{code:>999}", "{unknown}"):
        with pytest.raises(StitchPressError, match="占位符"):
            TextLayer(template=value)


def test_custom_reference_text_preserves_grid_and_overflow_blocks(minimal):
    library = demo_library(2)
    layer = asdict(TextLayer(template="{code} | Page {page}/{pages}", align="center"))
    renderer = Renderer(
        minimal,
        library,
        assign_symbols(minimal, library),
        Fabric(),
        code="AK0026",
        text_layers={"reference_top": layer},
    )
    plan = page_plan(minimal.width, minimal.height, 2, Fabric())
    image, raster = renderer.reference(plan.pages[0], plan)
    assert renderer.verify_cells(image, raster)["stitches"] == sum(minimal.counts)
    image.close()
    renderer.text_layers["reference_top"] = TextLayer(template="Wide text " * 100, points="36")
    with pytest.raises(StitchPressError, match="溢出"):
        renderer.reference(plan.pages[0], plan)
    renderer.text_layers["reference_top"] = TextLayer(template="中文缺字")
    image, raster = renderer.reference(plan.pages[0], plan)
    assert renderer.verify_cells(image, raster)["stitches"] == sum(minimal.counts)
    image.close()
    # Explicit customer typography stays explicit, rather than silently switching fonts.
    renderer.font_data = (bundled_font().parent / "NotoSans-Regular.ttf").read_bytes()
    with pytest.raises(StitchPressError, match="U\\+4E2D"):
        renderer.reference(plan.pages[0], plan)


def test_default_print_font_supports_chinese_metadata_without_system_fonts(minimal):
    library = demo_library(2)
    renderer = Renderer(
        minimal,
        library,
        assign_symbols(minimal, library),
        Fabric(),
        code="客户001",
        title="花园图纸",
        text_layers={"production_top": asdict(TextLayer(template="{title} · {code}"))},
    )
    assert bundled_font().name == "NotoSansCJKsc-Regular.otf"
    checked_font(32, None, "中文，英文 ABC 001 · × ± ℃")
    image, raster = renderer.production()
    assert renderer.verify_cells(image, raster)["stitches"] == sum(minimal.counts)
    image.close()
    plan = page_plan(minimal.width, minimal.height, 2, Fabric())
    image, raster = renderer.reference(plan.pages[0], plan)
    assert renderer.verify_cells(image, raster)["stitches"] == sum(minimal.counts)
    image.close()


def test_missing_font_message_identifies_character_and_recovery():
    latin = Path(bundled_font().parent / "NotoSans-Regular.ttf").read_bytes()
    with pytest.raises(StitchPressError, match="U\\+4E2D") as error:
        checked_font(32, latin, "客户中文")
    assert "恢复内置" in str(error.value) and "标题与页脚" in str(error.value)
    with pytest.raises(StitchPressError, match="U\\+10FFFF"):
        checked_font(32, None, "\U0010ffff")


def test_reference_text_changes_do_not_rebuild_production(tmp_path, minimal):
    library = demo_library(2)
    args = dict(
        design=minimal,
        library=library,
        mapping=assign_symbols(minimal, library),
        fabric=Fabric(),
        code="001",
        title="title",
        review=None,
        demo=True,
        work=tmp_path / "work",
        cache_root=tmp_path / "cache",
    )
    first = export_package(**args, destination=tmp_path / "first")
    second = export_package(
        **args,
        destination=tmp_path / "second",
        text_layers={"reference_top": asdict(TextLayer(template="New heading {code}"))},
    )
    old = {i["name"]: i for i in first["artifacts"]}
    for item in second["artifacts"]:
        if "Production/" in item["name"] or item["name"].endswith((".xlsx", ".png")):
            assert item["reused"] and item["sha256"] == old[item["name"]]["sha256"]
        if "TIFF_Pages/" in item["name"]:
            assert not item["reused"]
