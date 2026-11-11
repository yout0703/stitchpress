import copy
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project, sample_design
from stitchpress.app.post_settings import load_settings, save_settings
from stitchpress.app.reference_actions import import_pattern
from stitchpress.app.reference_package import render_context
from stitchpress.app.symbols import SymbolLibraryDialog
from stitchpress.core.errors import Cancelled, ResourceError
from stitchpress.core.render import screen_rgb, stitch_preview
from stitchpress.core.resources import library_assets, library_from_assets, revise_library
from stitchpress.core.streaming import BAND_BYTES, production_overview


def test_library_name_survives_portable_settings_without_rebinding(tmp_path):
    project = import_pattern(demo_project(), "fixtures/s01-independent.xsd")
    original = copy.deepcopy(project.entities["library"])
    original.pop("name", None)  # Old projects without the new field still load.
    library = library_from_assets(original, project.assets)
    renamed = revise_library(library, "rename-library", name="工厂甲 · 细格符号")
    assert renamed.revision != library.revision
    assert renamed.symbols == library.symbols and renamed.icc == library.icc
    mapping = copy.deepcopy(project.entities["mapping"])
    record, assets = library_assets(renamed)
    project.entities["library:" + library.revision] = original
    project.entities["library"] = record
    project.assets.update(assets)
    path = tmp_path / "设置.json"
    save_settings(project, path)
    restored = load_settings(path)
    assert restored.entities["library"]["name"] == renamed.name
    assert restored.entities["mapping"] == mapping
    with pytest.raises(ResourceError):
        revise_library(renamed, "rename-library", name=" ")
    assert revise_library(renamed, "rename-library", name=renamed.name) is renamed


def test_overview_samples_full_raster_without_strip_seams_and_can_cancel():
    _, renderer = render_context(demo_project())
    full, _ = renderer.production()
    rgb = screen_rgb(full, renderer.library.icc)
    full.close()
    overview, checks = production_overview(renderer, max_edge=157)
    ow, oh = overview.size
    xs = ((np.arange(ow) + 0.5) * rgb.width / ow).astype(int)
    ys = ((np.arange(oh) + 0.5) * rgb.height / oh).astype(int)
    expected = np.asarray(rgb)[ys[:, None], xs[None, :]]
    assert np.array_equal(np.asarray(overview), expected)
    assert checks["full_size"] == rgb.size and not checks["partial"]
    assert checks["max_source_band_bytes"] <= BAND_BYTES
    with pytest.raises(Cancelled):
        production_overview(renderer, cancel=lambda: True)
    overview.close()
    rgb.close()


def test_diamond_effect_preserves_blank_and_white_stitch_at_all_cell_sizes():
    design = sample_design(2, 1, 1)
    design = replace(
        design, labels=((0, -1),), palette=(replace(design.palette[0], rgb=(255, 255, 255)),)
    )
    for cell in range(6, 37):
        image = stitch_preview(design, cell=cell, style="dot")
        dx, dy = (image.width - 2 * cell) // 2, (image.height - cell) // 2
        occupied = image.crop((dx, dy, dx + cell, dy + cell))
        blank = image.crop((dx + cell, dy, dx + 2 * cell, dy + cell))
        assert len(np.unique(np.asarray(occupied).reshape(-1, 3), axis=0)) >= 2
        assert np.all(np.asarray(blank) == 255)
        assert image.width * 4 == image.height * 3
        occupied.close()
        blank.close()
        image.close()


def test_pending_navigation_and_inline_preview_do_not_mutate_project(qtbot, monkeypatch, tmp_path):
    w = Workbench()
    qtbot.addWidget(w)
    w.resize(1280, 800)
    w.show()
    original = copy.deepcopy(w.draft.entities)
    w.spec_panel.editor.fabric_fields["sx"].setText("0.254")
    assert "10.000" in w.spec_panel.editor.effective_ct.text()
    assert (
        w.spec_panel.editor.fabric_fields["nominal_ct"].text() == original["fabric"]["nominal_ct"]
    )
    assert "未应用" in w.reference_nav[1].text()
    w.show_page(8)
    with pytest.raises(Exception, match="定位"):
        w.require_reference_applied()
    assert w.pages.currentIndex() == 4 and w.print_tabs.currentIndex() == 0
    qtbot.wait(30)
    assert w.spec_panel.buttons.isVisible()
    from PySide6.QtCore import QPoint

    bottom = w.spec_panel.buttons.mapTo(w, QPoint(0, w.spec_panel.buttons.height()))
    assert bottom.y() < w.statusBar().geometry().top()
    w.spec_panel.reload()
    assert "未应用" not in w.reference_nav[1].text()
    assert "未应用" not in w.preview_state.text()
    image = Image.new("RGB", (100, 80), "red")
    path = tmp_path / "preview.png"
    image.save(path)
    image.close()
    w.show_page(7)
    w.show_post_preview(
        dict(preview=str(path), checks=dict(target="production", preview_size=(100, 80)))
    )
    assert w.post_preview_canvas.isVisible()
    assert w.post_tabs.currentIndex() == 0
    assert w.draft.entities == original
    assert w.cancel_button.isVisible() and not w.cancel_button.isEnabled()
    w.close()


def test_library_dialog_name_is_staged_and_cancel_keeps_project(qtbot):
    w = Workbench()
    qtbot.addWidget(w)
    original = copy.deepcopy(w.draft.entities)
    dialog = SymbolLibraryDialog(w)
    qtbot.addWidget(dialog)
    dialog.library_name.setText("新库名")
    dialog.accept_library()
    assert dialog.library.name == "新库名"
    assert w.draft.entities == original
    w.close()


def test_reference_page_cap_accepts_200_and_rejects_201(monkeypatch):
    from stitchpress.app import reference_package
    from stitchpress.core.errors import StitchPressError
    from stitchpress.core.post import PostSettings
    from stitchpress.core.reading import reading_layout

    project = import_pattern(demo_project(), "fixtures/s01-independent.xsd")
    _, renderer = render_context(project)
    from stitchpress.core.configuration import ReferenceSettings

    reading, original = reading_layout(renderer, ReferenceSettings())
    monkeypatch.setattr(
        reference_package,
        "reading_layout",
        lambda *_: (reading, replace(original, pages=original.pages * 200)),
    )
    options = PostSettings(targets=("pages",))
    assert reference_package.preflight(project, options, demo=True)["page_count"] == 200
    monkeypatch.setattr(
        reference_package,
        "reading_layout",
        lambda *_: (reading, replace(original, pages=original.pages * 201)),
    )
    with pytest.raises(StitchPressError, match="200页"):
        reference_package.preflight(project, options, demo=True)


def test_preview_target_change_and_pending_edits_hide_previous_image(qtbot, tmp_path):
    w = Workbench()
    qtbot.addWidget(w)
    w.show()
    path = tmp_path / "rendered.png"
    Image.new("RGB", (64, 80), "white").save(path)
    result = dict(preview=str(path), checks=dict(target="production", preview_size=(64, 80)))
    w.show_post_preview(result)
    assert w.post_preview_stack.currentIndex() == 1
    w.post_preview_target.setCurrentIndex(w.post_preview_target.findData("whole"))
    assert w.post_preview_stack.currentIndex() == 0
    w.show_post_preview(result)  # Late completion of the old target must not reappear.
    assert w.post_preview_stack.currentIndex() == 0
    w.post_preview_target.setCurrentIndex(w.post_preview_target.findData("production"))
    w.spec_panel.editor.fabric_fields["sx"].setText("0.2")
    w.show_post_preview(result)  # Pending forms are not yet in the worker fingerprint.
    assert w.post_preview_stack.currentIndex() == 0
    w.spec_panel.reload()
    w.close()


def test_apply_actions_track_changes_and_restore_keeps_current_tab(qtbot, monkeypatch):
    from PySide6.QtWidgets import QDialogButtonBox

    w = Workbench()
    qtbot.addWidget(w)
    panel = w.commerce_panel
    save = panel.buttons.button(QDialogButtonBox.StandardButton.Save)
    assert not save.isEnabled()
    original = copy.deepcopy(w.draft.entities)
    panel.apply()
    assert w.draft.entities == original  # A no-op must not append a history revision.
    panel.editor.tabs.setCurrentIndex(1)
    panel.editor.scenes[1]["fields"]["scale"].setText("0.9")
    assert save.isEnabled()
    panel.reload()
    assert panel.editor.tabs.currentIndex() == 1
    assert not panel.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
    calls = []
    monkeypatch.setattr(w, "start_worker", lambda operation, job: calls.append((operation, job)))
    w.open_commerce_preview()
    assert calls[0][0] == "post-preview"
    assert calls[0][1]["target"] == "detail-scene-2"
    assert w.post_preview_stack.currentIndex() == 0
    assert "正在生成" in w.preview_state.text()
    w.close()


def test_editor_reload_hides_retired_buttons_and_preserves_compact_fields(qtbot):
    from PySide6.QtCore import Qt

    w = Workbench()
    qtbot.addWidget(w)
    w.show()
    panel = w.text_panel
    old = panel.buttons
    panel.reload()
    assert old.isHidden()  # Deferred deletion must not leave a floating button box.
    fields, _, colors, _ = next(iter(panel.editor.controls.values()))
    assert fields["points"].maximumWidth() == 150
    assert fields["dx_mm"].maximumWidth() == 150
    assert all(box.maximumWidth() == 85 for box in colors)
    assert w.spec_panel.editor.fabric_fields["sx"].maximumWidth() == 110
    w.show_page(6)
    w.ref_symbol_search.setText("int:12")
    visible = [
        w.ref_symbols.item(i)
        for i in range(w.ref_symbols.count())
        if not w.ref_symbols.item(i).isHidden()
    ]
    assert len(visible) == 1 and visible[0].text() == "12"
    assert visible[0].data(Qt.ItemDataRole.UserRole) == "int:12"
    w.close()
