import io
from dataclasses import asdict, replace
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image

from stitchpress.app.fixtures import demo_project
from stitchpress.app.jobs import verify_delivery
from stitchpress.app.post_settings import MissingSettingsAssets, load_settings, save_settings
from stitchpress.app.reference_package import export_reference, preflight
from stitchpress.core.configuration import ReferenceSettings
from stitchpress.core.errors import Cancelled, StitchPressError
from stitchpress.core.identity import canonical_bytes, sha256
from stitchpress.core.post import POST_TARGETS, PostSettings, ThreadPlan
from stitchpress.core.resources import assign_symbols
from stitchpress.core.xsd import import_xsd


def project():
    value = demo_project(4, 3, 2)
    raw = Path("fixtures/s01-independent.xsd").read_bytes()
    import json

    value.entities["design"] = json.loads(canonical_bytes(import_xsd(raw).to_dict()))
    from stitchpress.core.resources import library_from_assets

    value.entities["mapping"] = assign_symbols(
        import_xsd(raw), library_from_assets(value.entities["library"], value.assets)
    )
    value.entities["metadata"] = {"title": "Reference fixture", "code": "AK0026"}
    value.assets["original.xsd"] = raw
    value.entities["pattern_input"] = {
        "asset": "original.xsd",
        "format": "xsd",
        "sha256": sha256(raw),
        "filename": "AK0026.xsd",
    }
    image = io.BytesIO()
    Image.new("RGB", (40, 30), "#e2c7aa").save(image, "PNG")
    value.assets["source.png"] = image.getvalue()
    value.entities["source"] = {"asset": "source.png", "correspondence_confirmed": True}
    value.entities["reference"] = ReferenceSettings(
        dpi=150, cell_mm="2.5", margins_mm=("12.7",) * 4
    ).to_dict()
    value.entities["commerce"] = {"art_source": "dot"}
    value.entities["post"] = PostSettings(
        targets=POST_TARGETS,
        thread_plans=tuple(
            asdict(ThreadPlan(str(ct), "180", "Self-authored coefficient fixture", True))
            for ct in (9, 14)
        ),
    ).to_dict()
    return value


def test_reference_full_collection_real_encodings_and_json_reopen(tmp_path):
    value = project()
    original = value.assets["original.xsd"]
    result = export_reference(value, tmp_path / "collection", tmp_path / "work", demo=True)
    assert result["status"] == "completed"
    assert verify_delivery(tmp_path / "collection", result)["files"] == result["files"]
    paths = [tmp_path / "collection" / item["name"] for item in result["artifacts"]]
    assert next(p for p in paths if p.suffix == ".xsd").read_bytes() == original
    for suffix, format in [(".bmp", "BMP"), (".jpg", "JPEG"), (".tif", "TIFF")]:
        examples = [p for p in paths if p.suffix == suffix]
        assert examples
        for p in examples:
            with Image.open(p) as image:
                assert image.format == format
    books = [p for p in paths if p.suffix == ".xlsx"]
    assert len(books) == 2
    from openpyxl import load_workbook

    for p in books:
        book = load_workbook(p, data_only=True)
        assert book.active["B3"].value == "310" and book.active["F5"].value == 6
        assert book.active["G3"].value == 1 and book.active["G5"].value == 2
        assert len(book.active._images) == 2
        assert book.active["E3"].fill.fgColor.rgb.endswith("000000")
        with ZipFile(p) as archive:
            assert len([n for n in archive.namelist() if n.startswith("xl/media/")]) == 2
    settings = next(p for p in paths if p.name.endswith("作品设置.json"))
    restored = load_settings(settings)
    assert restored.entities["design"] == value.entities["design"]
    assert restored.entities["mapping"] == value.entities.get(
        "mapping", restored.entities.get("mapping")
    )
    assert restored.assets["original.xsd"] == original
    assert value.assets["original.xsd"] == original
    assert len([p for p in paths if "电商图片" in p.parts]) == 12
    with Image.open(next(p for p in paths if p.parent.name == "分页图纸")) as image:
        assert image.size == (1240, 1754) and image.info["dpi"] == (150, 150)


def test_portable_settings_moved_and_missing_or_modified_assets_refuse(tmp_path):
    value = project()
    out = tmp_path / "settings.json"
    save_settings(value, out)
    import json
    import shutil

    record = json.loads(out.read_bytes())
    name = next(iter(record["assets"]))
    entry = record["assets"][name]
    resource = tmp_path / entry["file"]
    replacement = tmp_path / "selected.bin"
    replacement.write_bytes(resource.read_bytes())
    resource.unlink()
    with pytest.raises(MissingSettingsAssets):
        load_settings(out)
    assert (
        load_settings(out, replacements={name: replacement}).entities["metadata"]["code"]
        == "AK0026"
    )
    replacement.write_bytes(b"changed")
    with pytest.raises(StitchPressError, match="SHA"):
        load_settings(out, replacements={name: replacement})
    with pytest.raises(StitchPressError, match="已存在"):
        save_settings(value, out)
    # A fresh complete document can move with its relative sidecar.
    save_settings(value, tmp_path / "complete.json")
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.copy(tmp_path / "complete.json", moved / "complete.json")
    complete = json.loads((tmp_path / "complete.json").read_bytes())
    folder = next(iter(complete["assets"].values()))["file"].split("/")[0]
    shutil.copytree(tmp_path / folder, moved / folder)
    assert load_settings(moved / "complete.json").entities["design"] == value.entities["design"]


def test_reference_output_preconditions_and_cancel_keep_old_bytes(tmp_path):
    value = project()
    value.entities.pop("source")
    with pytest.raises(StitchPressError, match="来源图片"):
        preflight(value, demo=True)
    value = project()
    options = PostSettings(targets=("production",))
    with pytest.raises(Cancelled):
        export_reference(
            value,
            tmp_path / "cancelled",
            tmp_path / "work",
            options=options,
            demo=True,
            cancel=lambda: True,
        )
    assert not (tmp_path / "cancelled").exists()
    assert not list(tmp_path.glob(".stitchpress-post-*"))
    value.entities["design"]["labels"][0][0] = 1
    with pytest.raises(StitchPressError, match="原XSD"):
        preflight(value, demo=True)


def test_symbol_colour_matching_is_count_first_and_preserves_manual(minimal):
    import numpy as np

    from stitchpress.core.identity import digest
    from stitchpress.core.resources import SymbolLibrary, parse_symbol

    symbols = []
    for number, ink in enumerate([(0, 255, 255, 0), (255, 0, 255, 0), (0, 0, 0, 255)]):
        image = Image.new("CMYK", (20, 20))
        image.paste(ink, (4, 4, 16, 16))
        raw = io.BytesIO()
        image.save(raw, "TIFF")
        symbols.append(parse_symbol(raw.getvalue(), number, str(number)))
    library = SymbolLibrary(digest("colors"), tuple(symbols), None, "self-authored")
    red = replace(minimal.palette[0], rgb=(255, 0, 0))
    black = replace(minimal.palette[1], rgb=(0, 0, 0))
    design = replace(minimal, palette=(red, black))
    mapping = assign_symbols(design, library, strategy="color-nearest-v1")
    assert mapping[red.key] == symbols[0].key and mapping[black.key] == symbols[2].key
    manual = {red.key: symbols[1].key}
    assert (
        assign_symbols(design, library, manual, strategy="color-nearest-v1")[red.key]
        == symbols[1].key
    )
    assert mapping == assign_symbols(design, library, strategy="color-nearest-v1")
    assert np.asarray(library.symbols[0].image()).shape == (20, 20, 4)


@pytest.mark.parametrize("group,count", [("details", 7), ("squares", 5), ("single", 1)])
def test_reference_commerce_jpg_groups_do_not_generate_other_targets(tmp_path, group, count):
    from stitchpress.core.commerce import TARGETS

    value = project()
    selected = (
        tuple(t for t in TARGETS if t.startswith("detail"))
        if group == "details"
        else tuple(t for t in TARGETS if t.startswith("square"))
        if group == "squares"
        else ("square-1",)
    )
    options = PostSettings(targets=("commerce",), commerce_targets=selected)
    result = export_reference(
        value, tmp_path / "chosen", tmp_path / "work", demo=True, options=options
    )
    assert len(list((tmp_path / "chosen").rglob("*.jpg"))) == count
    assert result["files"] == count + 1  # The verification JSON is always present.
    assert not list((tmp_path / "chosen").glob("*.tif"))
    assert verify_delivery(tmp_path / "chosen", result)["files"] == count + 1


def test_reference_low_disk_never_publishes(tmp_path, monkeypatch):
    from collections import namedtuple

    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        "stitchpress.app.reference_package.shutil.disk_usage", lambda _: usage(100, 100, 0)
    )
    with pytest.raises(StitchPressError, match="磁盘"):
        export_reference(project(), tmp_path / "out", tmp_path / "work", demo=True)
    assert not (tmp_path / "out").exists()
