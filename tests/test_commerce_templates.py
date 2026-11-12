import copy
import io
from dataclasses import replace

import pytest
from PIL import Image
from PySide6.QtCore import QProcess

from stitchpress.app.commerce import export_commerce
from stitchpress.app.commerce_templates import (
    apply_preset,
    apply_template,
    fingerprint,
    load_preset,
    preset_project,
    render_sheet,
    save_preset,
)
from stitchpress.app.fixtures import demo_project
from stitchpress.app.store import Project
from stitchpress.core.commerce import TARGETS, CommerceSettings
from stitchpress.core.commerce_templates import templates, validate_template
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest


def source_project(width=16, height=24):
    project = demo_project(width, height, 3)
    project.entities["metadata"] = dict(title="蓝绣球与白蝶", code="00123")
    raw = io.BytesIO()
    Image.new("RGB", (width * 5, height * 5), "#f3f1eb").save(raw, "PNG")
    project.assets["sources/original.png"] = raw.getvalue()
    project.entities["source"] = dict(asset="sources/original.png", role="unreviewed-input")
    return project


@pytest.mark.parametrize("shape", [(16, 24), (24, 16), (18, 18)])
def test_four_layouts_render_complete_sets_preserving_master_and_sizes(tmp_path, shape):
    p = source_project(*shape)
    master = copy.deepcopy(p.entities["design"])
    fingerprints = set()
    for template in templates():
        settings = apply_template(CommerceSettings(), template, keep_scenes=False)
        p.entities["commerce"] = settings.to_dict()
        result = render_sheet(p, tmp_path / template["id"])
        assert [v["target"] for v in result["pages"]] == list(TARGETS)
        assert all("error" not in v for v in result["pages"])
        with Image.open(result["pages"][0]["preview"]) as img:
            fingerprints.add(img.tobytes())
            assert img.size == (1200, 1600)
        for page in result["pages"][7:]:
            with Image.open(page["preview"]) as img:
                assert img.size == (800, 800)
        assert p.entities["design"] == master
    assert len(fingerprints) == 4


def test_template_switch_preserves_overrides_classic_and_portable_resources(tmp_path):
    p = source_project()
    old = CommerceSettings(title="品牌 · {code}", detail_width=1600, detail_height=1200)
    first = apply_template(old, templates()[0])
    assert first.title == old.title and first.scenes == old.scenes
    assert first.detail_width == 1600 and first.squares == old.squares
    assert CommerceSettings(**{}).template is None
    reset = apply_template(first, templates()[1], keep_scenes=False, reset_text=True)
    assert reset.title == "{title}" and reset.scenes != first.scenes
    asset = "resources/background.png"
    assets = {asset: p.assets["sources/original.png"]}
    scenes = list(reset.scenes)
    scenes[0] = {**scenes[0], "background_asset": asset}
    reset = replace(reset, scenes=tuple(scenes), title="蓝绣球与白蝶 · 00123")
    preset = preset_project(reset, assets, p.entities["metadata"], "我的家居")
    save_preset(preset, tmp_path / "portable.stitchtemplate")
    reopened = load_preset(tmp_path / "portable.stitchtemplate")
    assert reopened.assets[asset] == assets[asset]
    assert reopened.entities["commerce"]["font_asset"] in reopened.assets
    assert len(reopened.assets) == 2
    assert set(reopened.entities) == {"metadata", "commerce", "commerce_preset"}
    assert reopened.entities["commerce"]["title"] == "{title} · {code}"
    assert reopened.entities["metadata"] == {"title": "", "code": ""}
    selected = apply_preset(old, reopened, keep_scenes=False)
    assert selected.title == old.title and selected.detail_width == 1600
    p.entities["commerce"] = selected.to_dict()
    p.assets.update(reopened.assets)
    assert digest(Project.decode(p.encode()).entities["commerce"]) == digest(selected.to_dict())
    with pytest.raises(StitchPressError, match="已存在"):
        save_preset(preset, tmp_path / "portable.stitchtemplate")
    broken = copy.deepcopy(templates()[0])
    broken["pages"]["portrait"]["scene"]["title"] = [0, 0, 1, 1]
    with pytest.raises(StitchPressError, match="重叠"):
        validate_template(broken)


def test_preview_errors_cache_version_resource_and_cancellation(tmp_path):
    from stitchpress.app.export import Cancelled

    p = source_project()
    p.entities["commerce"] = apply_template(CommerceSettings(), templates()[0]).to_dict()
    first = export_commerce(
        p,
        tmp_path / "one",
        tmp_path / "work",
        targets=TARGETS[:1],
        demo=True,
        cache_root=tmp_path / "cache",
    )
    same = export_commerce(
        p,
        tmp_path / "two",
        tmp_path / "work",
        targets=TARGETS[:1],
        demo=True,
        cache_root=tmp_path / "cache",
    )
    assert first["reused"] == 0 and same["reused"] == 1
    p.entities["commerce"]["template"]["version"] = 2
    changed = export_commerce(
        p,
        tmp_path / "three",
        tmp_path / "work",
        targets=TARGETS[:1],
        demo=True,
        cache_root=tmp_path / "cache",
    )
    assert changed["reused"] == 0
    before = fingerprint(p)
    p.assets["sources/original.png"] += b"changed"
    assert fingerprint(p) != before
    p.entities.pop("source")
    sheet = render_sheet(p, tmp_path / "sheet")
    errors = [v for v in sheet["pages"] if "error" in v]
    assert len(errors) == 1 and errors[0]["target"] == "detail-compare"
    with pytest.raises(Cancelled):
        render_sheet(p, tmp_path / "cancelled", cancel=lambda: True)
    p.entities["commerce"]["title"] = "超长中文标题" * 200
    sheet = render_sheet(p, tmp_path / "overflow")
    assert all("溢出" in v["error"] for v in sheet["pages"])


def test_native_picker_cancel_apply_undo_and_late_worker(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMainWindow

    from stitchpress.app.commerce_studio import TemplatePicker
    from stitchpress.app.commerce_ui import CommerceDialog

    owner = QMainWindow()
    owner.draft = source_project()
    qtbot.addWidget(owner)
    editor = CommerceDialog(owner)
    qtbot.addWidget(editor)
    initial = fingerprint(owner.draft)
    picker = TemplatePicker(editor)
    qtbot.addWidget(picker)
    picker.choices.setCurrentRow(2)
    picker.reject()
    assert fingerprint(owner.draft) == initial and editor.values().template is None
    editor.previous = (editor.values(), {})
    editor.set_values(picker.result_settings)
    assert editor.values().template["id"] == "home"
    editor.undo_template()
    editor.board.stop()
    assert editor.values().template is None and fingerprint(owner.draft) == initial
    # A process that completed against an older draft must never replace the visible preview.
    board = editor.board
    proc = QProcess(board)
    board.process = proc
    owner.draft.entities["metadata"]["title"] = "已修改"
    board.finished(proc, initial, 0)
    assert "参数已改变" in board.status.text() and not board.pages
    board.stop()
    picker.board.stop()


def test_template_font_does_not_inherit_unrelated_production_font(tmp_path):
    from pathlib import Path

    from stitchpress.core import text

    p = source_project()
    font = Path(text.__file__).parents[1] / "resources/fonts/NotoSans-Regular.ttf"
    p.assets["resources/latin.ttf"] = font.read_bytes()
    p.entities["output_font"] = dict(asset="resources/latin.ttf", name="Latin")
    p.entities["commerce"] = apply_template(CommerceSettings(), templates()[0]).to_dict()
    result = render_sheet(p, tmp_path / "separate-font")
    assert all("error" not in page for page in result["pages"])


def test_bundled_font_revision_invalidates_preview_and_export_review(monkeypatch):
    from stitchpress.app.reference_package import post_fingerprint
    from stitchpress.core import commerce_layout

    p = source_project()
    p.entities["commerce"] = apply_template(CommerceSettings(), templates()[0]).to_dict()
    before = (fingerprint(p), post_fingerprint(p))
    monkeypatch.setattr(commerce_layout, "commerce_font", lambda: b"new font revision")
    assert fingerprint(p) != before[0]
    assert post_fingerprint(p) != before[1]


def test_preview_debounce_waits_for_valid_stable_parameters(qtbot, monkeypatch):
    from stitchpress.app.commerce_studio import PreviewBoard

    project = source_project()
    board = PreviewBoard(lambda: project)
    qtbot.addWidget(board)
    monkeypatch.setattr(board, "isVisible", lambda: True)
    calls = []
    monkeypatch.setattr(board, "refresh", lambda: calls.append(fingerprint(project)))
    board.loaded_sha = fingerprint(project)
    project.entities["metadata"]["title"] = "新标题"
    board.check_stale()
    assert not calls
    board.check_stale()
    assert calls == [fingerprint(project)]

    # A rejected form does not launch a worker or remove the last image.
    def invalid():
        raise ValueError("缩放无效")

    board.snapshot = invalid
    board.check_stale()
    assert len(calls) == 1 and "缩放无效" in board.status.text()
