import io
from dataclasses import asdict, replace

import pytest
from PIL import Image

from stitchpress.app.commerce import commerce_context, export_commerce
from stitchpress.app.commerce_ui import CommerceDialog
from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project
from stitchpress.app.jobs import verify_delivery
from stitchpress.core.commerce import TARGETS, CommerceRenderer, CommerceSettings, SceneSettings
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest


def project_with_source():
    project = demo_project(12, 16, 3)
    raw = io.BytesIO()
    Image.new("RGB", (60, 80), "pink").save(raw, "PNG")
    project.assets["sources/original.png"] = raw.getvalue()
    project.entities["source"] = {"asset": "sources/original.png", "role": "unreviewed-input"}
    return project


def test_twelve_commerce_outputs_keep_pixel_sizes_and_only_scene_dependencies_change(tmp_path):
    project = project_with_source()
    before = digest(project.entities)
    first = export_commerce(
        project, tmp_path / "first", tmp_path / "work", demo=True, cache_root=tmp_path / "cache"
    )
    assert (
        first["files"] == 12 and verify_delivery(tmp_path / "first", first)["digital"] == "passed"
    )
    assert digest(project.entities) == before
    for item in first["artifacts"]:
        with Image.open(tmp_path / "first" / item["name"]) as image:
            assert image.size == ((800, 800) if "square-" in item["name"] else (1200, 1600))
            assert image.info["icc_profile"]
    settings = CommerceSettings()
    scenes = list(settings.scenes)
    scenes[0] = {**scenes[0], "frame": 5}
    project.entities["commerce"] = replace(settings, scenes=tuple(scenes), square_dpi=300).to_dict()
    # DPI-only metadata changes are dependencies; square pixels stay exactly 800x800.
    second = export_commerce(
        project,
        tmp_path / "second",
        tmp_path / "work",
        targets=TARGETS[7:],
        demo=True,
        cache_root=tmp_path / "cache",
    )
    assert second["files"] == 5
    project.entities["commerce"]["square_dpi"] = 150
    third = export_commerce(
        project, tmp_path / "third", tmp_path / "work", demo=True, cache_root=tmp_path / "cache"
    )
    assert third["reused"] == 10
    changed = {item["checks"]["content"] for item in third["artifacts"] if not item["reused"]}
    assert changed == {"scene-1"}


def test_scene_and_frame_coverage_and_out_of_bounds_preserve_design():
    project = project_with_source()
    for i in range(13):
        settings = CommerceSettings(scenes=(asdict(SceneSettings(scene=i, frame=i % 8)),) * 3)
        renderer = CommerceRenderer(commerce_context(project), settings, project.assets, demo=True)
        image, checks = renderer.render("detail-scene-1")
        assert (
            checks["scene_art_box"]
            and checks["design_sha"] == commerce_context(project)["design"].content_sha
        )
        image.close()
        renderer.subject.close()
    settings = CommerceSettings(scenes=(asdict(SceneSettings(scale="1.4", dx="0.5")),) * 3)
    renderer = CommerceRenderer(commerce_context(project), settings, project.assets)
    with pytest.raises(StitchPressError, match="超出"):
        renderer.render("detail-scene-1")
    renderer.subject.close()


def test_commerce_missing_source_failure_and_cancel_never_publish_incomplete_set(tmp_path):
    project = demo_project(8, 8, 2)
    with pytest.raises(StitchPressError, match="来源图片"):
        export_commerce(
            project, tmp_path / "missing", tmp_path / "work", targets=("detail-compare",), demo=True
        )
    assert not (tmp_path / "missing").exists()
    with pytest.raises(StitchPressError, match="取消"):
        export_commerce(
            project, tmp_path / "cancel", tmp_path / "work", demo=True, cancel=lambda: True
        )
    assert not (tmp_path / "cancel").exists()
    with pytest.raises(StitchPressError, match="审核"):
        export_commerce(project, tmp_path / "unfrozen", tmp_path / "work", targets=("square-1",))


def test_commerce_dialog_cancel_keeps_three_independent_settings(qtbot, monkeypatch):
    window = Workbench()
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    qtbot.addWidget(window)
    before = digest(window.draft.entities)
    dialog = CommerceDialog(window)
    qtbot.addWidget(dialog)
    dialog.scenes[0]["scene"].setCurrentIndex(7)
    assert dialog.values().scenes[0]["scene"] == 7
    assert dialog.values().scenes[1] == dialog.saved.scenes[1]
    dialog.reject()
    assert digest(window.draft.entities) == before
    window.close()


@pytest.mark.parametrize("ratio", [(300, 100), (100, 300), (200, 200)])
def test_photo_scenes_keep_subject_ratio_and_pixels(ratio):
    project = project_with_source()
    renderer = CommerceRenderer(commerce_context(project), CommerceSettings(), project.assets)
    renderer.subject.close()
    renderer.subject = Image.new("RGB", ratio, "#a52d39")
    for scene in range(9, 13):
        canvas = Image.new("RGB", (600, 800), "white")
        box = renderer.scene(canvas, (0, 0, 600, 800), SceneSettings(scene=scene, border=0, mat=0))
        left, top, right, bottom = box
        assert 0 <= left < right <= 600 and 0 <= top < bottom <= 800
        assert abs((right - left) / (bottom - top) - ratio[0] / ratio[1]) < 0.02
        assert canvas.getpixel(((left + right) // 2, (top + bottom) // 2)) == (165, 45, 57)
        canvas.close()
    renderer.subject.close()


def test_custom_background_overrides_selected_photo_scene():
    project = project_with_source()
    spec = SceneSettings(scene=12, background_asset="sources/original.png")
    settings = CommerceSettings(scenes=(asdict(spec),) * 3)
    renderer = CommerceRenderer(commerce_context(project), settings, project.assets)
    image, checks = renderer.render("detail-scene-1")
    assert checks["scene_art_box"]
    image.close()
    renderer.subject.close()
