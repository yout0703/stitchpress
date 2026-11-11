from dataclasses import replace

import pytest
from PySide6.QtCore import QRectF

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
