from pathlib import Path

import pytest
from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader

from stitchpress.app.export import Cancelled, export_package, zip_package
from stitchpress.core.documents import write_counts
from stitchpress.core.errors import StitchPressError
from stitchpress.core.importers import export_oxs, import_oxs
from stitchpress.core.layout import Fabric
from stitchpress.core.render import Renderer, stitch_preview
from stitchpress.core.resources import assign_symbols, demo_library


def test_independent_oxs_expected_matrix():
    data = Path("fixtures/s01.oxs").read_bytes()
    design = import_oxs(data)
    assert (design.width, design.height) == (3, 2)
    assert design.labels == ((0, -1, 1), (1, 0, 0))
    assert [c.code for c in design.palette] == ["001", "3865"]
    assert import_oxs(export_oxs(design)).labels == design.labels


@pytest.mark.parametrize(
    "extension",
    [
        b"<backstitches><backstitch /></backstitches>",
        b"<partstitches><stitch /></partstitches>",
        b"<unknown />",
        b"<format><stitch /></format>",
        b"<format specialstitches='1' />",
        b"<commentboxes><commentbox /></commentboxes>",
    ],
)
def test_s07_unknown_or_unsupported_oxs_rejected(extension):
    data = Path("fixtures/s01.oxs").read_bytes().replace(b"</chart>", extension + b"</chart>")
    with pytest.raises(StitchPressError):
        import_oxs(data)


def test_oxs_format_comments_preserve_full_stitch_matrix():
    data = Path("fixtures/s01.oxs").read_bytes()
    compatible = data.replace(
        b"</chart>",
        b'<format comments01="Open Cross Stitch" comments02="Blank cells omitted" />'
        b"<commentboxes /></chart>",
    )
    actual = import_oxs(compatible)
    expected = import_oxs(data)
    assert actual.labels == expected.labels
    assert actual.counts == expected.counts


def test_counts_formula_and_cached_integer(tmp_path, minimal):
    path = tmp_path / "counts.xlsx"
    write_counts(minimal, "AK0026", path)
    formula = load_workbook(path).active
    values = load_workbook(path, data_only=True).active
    assert values["B1"].value == "AK0026"
    assert values["C3"].value == "001"
    assert values["E3"].value == 3
    assert values["E4"].value == 2
    assert values["E5"].value == 5
    assert formula["E5"].value == "=SUM(E3:E4)"
    assert values["E3"].font.sz == 14


def test_showcase_white_subject_and_blank_distinct(minimal):
    image = stitch_preview(minimal)
    assert image.width * 4 == image.height * 3
    # White subject has shadowed thread, while the blank cell remains pure white.
    dx, dy = (image.width - 54) // 2, (image.height - 36) // 2
    assert image.crop((dx + 18, dy, dx + 36, dy + 18)).tobytes() == bytes([255]) * 18 * 18 * 3
    assert len(set(image.crop((dx + 36, dy, dx + 54, dy + 18)).tobytes())) > 1


def test_tiff_full_readback_detects_wrong_stitch(tmp_path, minimal):
    library = demo_library(2)
    renderer = Renderer(minimal, library, assign_symbols(minimal, library), Fabric())
    image, raster = renderer.production()
    path = tmp_path / "production.tif"
    assert renderer.save_tiff(image, path, raster)["stitches"] == 5
    image.putpixel((raster.xs[0] + 4, raster.ys[0] + 4), (0, 255, 0, 0))
    with pytest.raises(StitchPressError, match="针格反读"):
        renderer.verify_cells(image, raster)


def test_n_plus_6_atomic_publish_and_pdf(tmp_path, minimal):
    library = demo_library(2)
    destination = tmp_path / "交付"
    result = export_package(
        design=minimal,
        library=library,
        mapping=assign_symbols(minimal, library),
        fabric=Fabric(),
        code="AK0026",
        title="fixture",
        destination=destination,
        work=tmp_path / "jobs",
        review=None,
        demo=True,
    )
    assert result["status"] == "completed"
    assert result["visual_review"] == "pending"
    assert len(list(p for p in destination.rglob("*") if p.is_file())) == 7
    pdf = PdfReader(next(destination.glob("*.pdf")))
    assert len(pdf.pages) == 1
    assert abs(float(pdf.pages[0].mediabox.width) - 210 / 25.4 * 72) < 0.001
    tiff_path = next((destination / "A4_TIFF_Pages").glob("*.tif"))
    with Image.open(tiff_path) as tif:
        assert (
            pdf.pages[0]["/Resources"]["/XObject"]["/Chart"].get_object().get_data()
            == tif.tobytes()
        )
    assert zip_package(destination, tmp_path / "bundle.zip")["files"] == 7


def test_no_unreviewed_production_and_cancel_leaves_no_partial_package(tmp_path, minimal):
    library = demo_library(2)
    params = dict(
        design=minimal,
        library=library,
        mapping=assign_symbols(minimal, library),
        fabric=Fabric(),
        code="AK0026",
        title="test",
        work=tmp_path / "jobs",
        review=None,
    )
    with pytest.raises(StitchPressError, match="冻结"):
        export_package(**params, destination=tmp_path / "formal")
    stages = []
    with pytest.raises(Cancelled):
        export_package(
            **params,
            destination=tmp_path / "cancelled",
            demo=True,
            cancel=lambda: len(stages) >= 2,
            progress=stages.append,
        )
    assert not (tmp_path / "cancelled").exists()
    assert not list(tmp_path.glob(".stitchpress-*"))
