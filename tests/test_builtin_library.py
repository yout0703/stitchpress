import io

import pytest
from PIL import ImageCms
from PySide6.QtWidgets import QInputDialog

from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project, new_project, sample_design
from stitchpress.app.reference_actions import import_pattern
from stitchpress.app.store import Project
from stitchpress.core.errors import ResourceError
from stitchpress.core.models import Design
from stitchpress.core.resources import (
    _read_builtin_library,
    assign_symbols,
    builtin_library,
    library_from_assets,
)


def test_color_matching_reuses_one_transform_without_changing_rgb_pixels(monkeypatch):
    library = builtin_library()
    build = ImageCms.buildTransformFromOpenProfiles
    apply = ImageCms.applyTransform
    calls = {"build": 0, "apply": 0}

    def counted_build(*args, **kwargs):
        calls["build"] += 1
        return build(*args, **kwargs)

    def checked_apply(image, transform):
        rgb = apply(image, transform)
        if calls["apply"] in {0, 75, 151}:
            expected = ImageCms.profileToProfile(
                image,
                ImageCms.ImageCmsProfile(io.BytesIO(library.icc)),
                ImageCms.createProfile("sRGB"),
                outputMode="RGB",
                renderingIntent=1,
            )
            assert rgb.tobytes() == expected.tobytes()
            expected.close()
        calls["apply"] += 1
        return rgb

    monkeypatch.setattr(ImageCms, "buildTransformFromOpenProfiles", counted_build)
    monkeypatch.setattr(ImageCms, "applyTransform", checked_apply)
    mapping = assign_symbols(sample_design(), library, strategy="color-nearest-v1")
    assert len(set(mapping.values())) == len(mapping) == 8
    assert calls == {"build": 1, "apply": 152}


def test_builtin_colors_and_profile_are_portable_after_save_and_pattern_import():
    library = builtin_library()
    assert len(library.symbols) == 152
    assert library.symbols[0].external_id == "01"
    assert library.symbols[-1].external_id == 152
    assert len({symbol.pixel_sha for symbol in library.symbols}) == 152
    profile = ImageCms.ImageCmsProfile(io.BytesIO(library.icc))
    assert profile.profile.xcolor_space.strip() == "CMYK"
    assert any(
        any(c != m or m != y for c, m, y, _k in symbol.image().getdata())
        for symbol in library.symbols
    )
    project = import_pattern(new_project(), "fixtures/s01-independent.xsd")
    restored = Project.decode(project.encode())
    loaded = library_from_assets(restored.entities["library"], restored.assets)
    assert loaded == library
    assert restored.entities["mapping"] == project.entities["mapping"]
    assert Design.from_dict(restored.entities["design"]).counts == (3, 3)


def test_damaged_or_missing_bundled_library_never_falls_back_to_black(tmp_path):
    path = tmp_path / "missing.tar.xz"
    with pytest.raises(ResourceError, match="内置彩色符号库"):
        _read_builtin_library(path)
    path.write_bytes(b"not a color library")
    with pytest.raises(ResourceError, match="内置彩色符号库"):
        _read_builtin_library(path)


def test_first_launch_new_and_library_picker_use_colors_but_old_project_stays(qtbot, monkeypatch):
    window = Workbench()
    qtbot.addWidget(window)
    assert len(window.symbol_library().symbols) == 152
    assert window.symbol_library().icc
    assert "彩色" in window.ref_library_label.text()
    assert "已带 CMYK ICC" in window.ref_library_label.text()
    assert window.ref_symbols.count() == 152
    old = demo_project()
    old_record = old.entities["library"].copy()
    window.set_project(old)
    assert window.draft.entities["library"] == old_record
    assert len(window.symbol_library().symbols) == 66
    choices = []

    def choose(_parent, _title, _label, labels, *_args):
        choices.extend(labels)
        return next(label for label in labels if "彩色" in label), True

    monkeypatch.setattr(QInputDialog, "getItem", choose)
    window.select_reference_library()
    assert any("彩色" in label for label in choices)
    assert len(window.symbol_library().symbols) == 152
    assert old_record in window.draft.entities.values()
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    window.new_demo()
    assert len(window.symbol_library().symbols) == 152
    assert window.symbol_library().icc
