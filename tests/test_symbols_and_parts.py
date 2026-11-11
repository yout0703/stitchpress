import io
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from stitchpress.app.desktop import Workbench
from stitchpress.app.export import export_package
from stitchpress.app.fixtures import demo_project
from stitchpress.app.symbols import SymbolLibraryDialog, export_symbol_card
from stitchpress.core.configuration import ShowcaseSettings
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest
from stitchpress.core.layout import Fabric
from stitchpress.core.render import Renderer
from stitchpress.core.resources import (
    assign_symbols,
    demo_library,
    library_assets,
    library_from_assets,
    revise_library,
)


def test_library_edits_preserve_ids_old_pixels_and_used_bindings():
    original = demo_library(2)
    renamed = revise_library(original, "rename", key="int:0", name="显示名称")
    assert renamed.symbols[0].external_id == 0
    assert original.symbols[0].filename == "demo-0.tif"
    assert renamed.symbols[0].pixel_sha == original.symbols[0].pixel_sha
    assert renamed.revision != original.revision
    with pytest.raises(StitchPressError, match="使用中"):
        revise_library(renamed, "remove", key="int:0", used=frozenset({"int:0"}))
    image = Image.new("CMYK", (20, 20), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((4, 4, 16, 16), outline=(255, 255, 255, 255), width=3)
    buffer = io.BytesIO()
    image.save(buffer, "TIFF")
    updated = revise_library(
        renamed, "add", external_id="0.1", data=buffer.getvalue(), name="new.tif"
    )
    record, assets = library_assets(updated)
    assert library_from_assets(record, assets).by_key["str:0.1"].external_id == "0.1"
    assert len(original.symbols) == 2


def test_natural_thread_order_does_not_reassign_existing_manual_mapping(minimal):
    design = replace(
        minimal,
        palette=(replace(minimal.palette[0], code="10"), replace(minimal.palette[1], code="2")),
    )
    library = demo_library(2)
    manual = {design.palette[0].key: "int:0", design.palette[1].key: "int:1"}
    assert assign_symbols(design, library, manual) == manual
    renderer = Renderer(design, library, manual, Fabric())
    assert [c.code for _, c in renderer.entries] == ["2", "10"]
    assert renderer.mapping[design.palette[1].key] == "int:1"


def test_symbol_card_uses_exact_current_mapping_and_keeps_project_unchanged(tmp_path):
    project = demo_project(6, 8, 3)
    before = digest(project.entities)
    target = tmp_path / "符号速查卡.pdf"
    result = export_symbol_card(project, target)
    assert result["pages"] == 1
    assert result["colors"] == 3
    assert len(PdfReader(target).pages) == 1
    assert digest(project.entities) == before
    with pytest.raises(StitchPressError, match="另存"):
        export_symbol_card(project, target)


def test_symbol_dialog_magnifies_real_tile_and_cancel_keeps_library(qtbot):
    window = Workbench()
    qtbot.addWidget(window)
    revision = window.draft.entities["library"]["revision"]
    dialog = SymbolLibraryDialog(window)
    qtbot.addWidget(dialog)
    dialog.table.selectRow(0)
    assert dialog.preview.pixmap() and not dialog.preview.pixmap().isNull()
    dialog.reject()
    assert window.draft.entities["library"]["revision"] == revision
    window.close()


@pytest.mark.parametrize("target, suffix", [("counts", ".xlsx"), ("pdf", ".pdf")])
def test_single_item_does_not_render_production_or_publish_internal_pages(
    tmp_path, minimal, monkeypatch, target, suffix
):
    def forbidden(*args, **kwargs):
        raise AssertionError("unselected production must not render")

    monkeypatch.setattr(Renderer, "production", forbidden)
    library = demo_library(2)
    result = export_package(
        design=minimal,
        library=library,
        mapping=assign_symbols(minimal, library),
        fabric=Fabric(),
        code="AK0026",
        title="test",
        destination=tmp_path / "part",
        work=tmp_path / "work",
        review=None,
        demo=True,
        targets=frozenset({target}),
    )
    files = [p for p in (tmp_path / "part").rglob("*") if p.is_file()]
    assert result["files"] == 1
    assert len(files) == 1 and files[0].suffix == suffix
    assert result["snapshot"]["targets"] == [target]


def test_dot_showcase_change_reuses_every_unaffected_engineering_file(tmp_path, minimal):
    library = demo_library(2)
    params = dict(
        design=minimal,
        library=library,
        mapping=assign_symbols(minimal, library),
        fabric=Fabric(),
        code="AK0026",
        title="test",
        work=tmp_path / "work",
        cache_root=tmp_path / "cache",
        review=None,
        demo=True,
    )
    first = export_package(**params, destination=tmp_path / "x")
    second = export_package(
        **params, destination=tmp_path / "dot", showcase_options=ShowcaseSettings(style="dot")
    )
    assert second["reused"] == 4
    by_name = {r["name"]: r for r in first["artifacts"]}
    for item in second["artifacts"]:
        if item["reused"]:
            assert item["sha256"] == by_name[item["name"]]["sha256"]
    image = next(r for r in second["artifacts"] if r["name"].endswith(".png"))
    assert image["checks"]["style"] == "dot"
    assert not image["checks"]["one_x_per_occupied_cell"]


@pytest.mark.parametrize("format", ["PNG", "BMP"])
def test_import_symbol_normalizes_and_does_not_turn_transparency_into_ink(format):
    from stitchpress.core.resources import normalize_symbol_image, parse_symbol

    source = Image.new("RGBA", (16, 16), (255, 0, 0, 0))
    ImageDraw.Draw(source).rectangle((5, 5, 10, 10), fill=(0, 0, 0, 255))
    if format == "BMP":
        source = source.convert("RGB")
    buffer = io.BytesIO()
    source.save(buffer, format)
    result, receipt = normalize_symbol_image(buffer.getvalue(), None)
    symbol = parse_symbol(result, "001", "normalized.tif")
    assert symbol.key == "str:001"
    assert receipt["source_format"] == format
    assert receipt["method"].startswith("demo-only")
    if format == "PNG":
        assert symbol.image().getpixel((0, 0)) == (0, 0, 0, 0)
        assert any(symbol.image().getpixel((6, 6)))
