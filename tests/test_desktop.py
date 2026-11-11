import copy
from dataclasses import replace

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QGraphicsView

from stitchpress.app.desktop import AISettings, Workbench
from stitchpress.app.fixtures import sample_design
from stitchpress.app.store import Coordinator, Project
from stitchpress.core.errors import StitchPressError


def test_desktop_open_preview_select_patch_and_undo(qtbot, tmp_path):
    window = Workbench()
    qtbot.addWidget(window)
    window.show()
    original = window.design
    window.select_region(QRectF(200, 300, 80, 80))
    assert window.region is not None
    window.patch_color.setCurrentIndex(0)  # Set selected cells to blank.
    window.patch_region()
    assert window.design.parent_revision_id == original.revision_id
    window.undo_patch()
    assert window.design.content_sha == original.content_sha
    window.dirty = False
    window.close()


def test_desktop_title_save_and_reopen(qtbot, tmp_path):
    window = Workbench()
    qtbot.addWidget(window)
    path = tmp_path / "中文作品.stitchpress"
    coordinator = Coordinator(path, window.draft)
    window.set_project(coordinator.project, path, coordinator)
    window.code_edit.setText("AK0026")
    window.title_edit.setText("白花测试")
    assert window.save()
    actual = Project.decode(path.read_bytes())
    assert actual.entities["metadata"] == {"code": "AK0026", "title": "白花测试"}
    assert actual.entities["mapping"]
    window.close()


def test_title_edit_preserves_asymmetric_fabric_margins(qtbot):
    window = Workbench()
    qtbot.addWidget(window)
    window.draft.entities["fabric"].update(left="1.1", top="2.2", right="3.3", bottom="4.4")
    window.refresh()
    window.title_edit.setText("只改标题")
    assert window.apply_fields()
    fabric = window.draft.entities["fabric"]
    assert [fabric[k] for k in ("left", "top", "right", "bottom")] == ["1.1", "2.2", "3.3", "4.4"]
    window.dirty = False
    window.close()


def test_save_as_adds_portable_extension(qtbot, tmp_path, monkeypatch):
    window = Workbench()
    qtbot.addWidget(window)
    monkeypatch.setattr(
        "stitchpress.app.desktop.QFileDialog.getSaveFileName",
        lambda *args: (str(tmp_path / "无后缀作品"), ""),
    )
    assert window.save_as()
    assert (tmp_path / "无后缀作品.stitchpress").is_file()
    assert not (tmp_path / "无后缀作品").exists()
    window.close()


def test_ai_configuration_defaults_without_touching_keychain(qtbot, monkeypatch):
    monkeypatch.setattr("stitchpress.app.desktop.load_settings", lambda: {})
    saved = []
    monkeypatch.setattr("stitchpress.app.desktop.save_settings", saved.append)
    dialog = AISettings()
    qtbot.addWidget(dialog)
    assert dialog.model.text() == "deepseek-flash"
    dialog.save()
    assert saved[0]["ai_profile"]["model"] == "deepseek-flash"
    assert "api_key" not in saved[0]


def test_rejected_large_candidate_does_not_replace_existing_master(qtbot):
    window = Workbench()
    qtbot.addWidget(window)
    original = window.design.content_sha
    with pytest.raises(StitchPressError, match="容量"):
        window.adopt_design(replace(sample_design(67, 1, 67), labels=(tuple(range(67)),)))
    assert window.design.content_sha == original
    assert not window.history
    window.dirty = False
    window.close()


def test_theme_switch_preserves_master_selection_zoom_and_other_settings(qtbot, monkeypatch):
    settings = {"ai_profile": {"model": "customer-model"}, "installation_id": "test-install"}
    monkeypatch.setattr("stitchpress.app.desktop.load_settings", lambda: settings.copy())
    saved = []
    monkeypatch.setattr("stitchpress.app.desktop.save_settings", saved.append)
    window = Workbench()
    qtbot.addWidget(window)
    window.show()
    original = copy.deepcopy(window.draft)
    window.select_region(QRectF(200, 300, 80, 80))
    region = window.region
    window.canvas.actual_pixels()
    window.canvas.zoom(1.2)
    zoom = window.canvas.transform().m11()
    window.toggle_theme()
    assert window.theme == "light"
    assert saved[-1]["appearance"]["theme"] == "light"
    assert saved[-1]["ai_profile"] == settings["ai_profile"]
    assert saved[-1]["installation_id"] == "test-install"
    assert window.draft.entities == original.entities
    assert window.draft.assets == original.assets
    assert window.region == region
    assert window.canvas.transform().m11() == zoom
    assert not window.dirty
    window.close()


def test_theme_preference_restores_and_invalid_preference_falls_back(qtbot, monkeypatch):
    monkeypatch.setattr(
        "stitchpress.app.desktop.load_settings", lambda: {"appearance": {"theme": "light"}}
    )
    window = Workbench()
    qtbot.addWidget(window)
    assert window.theme == "light"
    window.close()
    monkeypatch.setattr(
        "stitchpress.app.desktop.load_settings", lambda: {"appearance": {"theme": "obsolete"}}
    )
    fallback = Workbench()
    qtbot.addWidget(fallback)
    assert fallback.theme == "dark"
    fallback.close()


def test_palette_search_highlight_and_navigation_keep_canvas_context(qtbot):
    window = Workbench()
    qtbot.addWidget(window)
    window.show()
    original = window.design.content_sha
    first_color = window.design.palette[0]
    window.palette_search.setText(first_color.code)
    visible = [r for r in range(window.table.rowCount()) if not window.table.isRowHidden(r)]
    assert visible
    assert all(first_color.code in window.table.item(r, 1).toolTip() for r in visible)
    window.table.selectRow(visible[0])
    assert window.pages.currentIndex() == 0
    assert window.canvas.isVisible()
    assert window.symbol_button.isEnabled()
    window.clear_highlight()
    assert not window.symbol_button.isEnabled()
    window.palette_search.setText("no-such-thread")
    assert all(window.table.isRowHidden(r) for r in range(window.table.rowCount()))
    window.show_page(1)
    assert not window.view_controls.isVisible()
    window.show_page(2)
    assert window.export_nav.isChecked()
    window.show_page(0)
    assert window.view_controls.isVisible()
    window.set_canvas_tool(True)
    assert window.canvas.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    window.inspector_button.setChecked(True)
    qtbot.waitUntil(lambda: window.inspector.width() >= 300)
    assert window.title_edit.isVisible()
    qtbot.waitUntil(lambda: window.inspector.horizontalScrollBar().maximum() == 0)
    window.inspector_button.setChecked(False)
    assert window.inspector.isHidden()
    window.ai_nav.setChecked(True)
    assert window.pages.currentIndex() == 1
    window.export_nav.setChecked(True)
    assert window.pages.currentIndex() == 2
    window.workbench_nav.setChecked(True)
    assert window.pages.currentIndex() == 0
    assert window.design.content_sha == original
    window.close()


def test_project_change_clears_stale_selection_and_disables_patch(qtbot):
    window = Workbench()
    qtbot.addWidget(window)
    window.show()
    window.select_region(QRectF(200, 300, 80, 80))
    assert window.patch_button.isEnabled()
    window.select_region(QRectF(-1000, -1000, 10, 10))
    assert window.region is None
    assert not window.patch_button.isEnabled()
    window.select_region(QRectF(200, 300, 80, 80))
    window.set_project(window.baseline)
    assert window.region is None
    assert not window.patch_button.isEnabled()
    assert not window.patch_color.isEnabled()
    assert not window.undo_button.isEnabled()
    window.close()
