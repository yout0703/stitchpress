"""Exercise user gestures, visual symbol choice and work avoided on hot paths."""

import copy
from zipfile import ZIP_STORED

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox

from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project
from stitchpress.app.post_settings import settings_document
from stitchpress.app.reference_package import post_fingerprint
from stitchpress.app.store import Project
from stitchpress.app.symbol_picker import SymbolPicker
from stitchpress.core.identity import digest


def test_real_mouse_click_drag_replace_and_undo(qtbot):
    w = Workbench()
    qtbot.addWidget(w)
    w.show()
    w.show_page(0)
    qtbot.waitExposed(w)
    w.canvas.fit()
    item = w.canvas._grid_item
    point = w.canvas.mapFromScene(item.dx + 6.5 * 18, item.dy + 8.5 * 18)
    qtbot.mouseClick(w.canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert w.region == (6, 8, 7, 9)
    assert w.patch_button.isEnabled() and w.patch_color.isEnabled()
    original = w.design
    target = 0 if original.labels[8][6] != 0 else 1
    w.patch_color.setCurrentIndex(w.patch_color.findData(target))
    qtbot.mouseClick(w.patch_button, Qt.MouseButton.LeftButton)
    assert w.design.labels[8][6] == target
    w.undo_patch()
    assert w.design.content_sha == original.content_sha
    assert "freeze" not in w.draft.entities
    start = w.canvas.mapFromScene(item.dx + 10.2 * 18, item.dy + 12.2 * 18)
    end = w.canvas.mapFromScene(item.dx + 12.8 * 18, item.dy + 14.8 * 18)
    qtbot.mousePress(w.canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(w.canvas.viewport(), end)
    qtbot.mouseRelease(w.canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert w.region == (10, 12, 13, 15)
    assert w.patch_color.itemIcon(1).isNull() is False
    w.dirty = False
    w.close()


def test_visual_symbol_picker_swap_cancel_and_hidden_search(qtbot, monkeypatch):
    w = Workbench()
    qtbot.addWidget(w)
    w.table.selectRow(0)
    color_index = w.table.selectedItems()[0].data(Qt.ItemDataRole.UserRole)
    color = w.design.palette[color_index]
    mapping = dict(w.draft.entities["mapping"])
    other = next(k for k in mapping if k != color.key)
    target = mapping[other]
    before = w.design.content_sha
    dialog = SymbolPicker(w, color)
    qtbot.addWidget(dialog)
    assert {b.text() for b in dialog.buttons.buttons()} == {"应用符号", "取消"}
    assert all(not dialog.symbols.item(i).icon().isNull() for i in range(dialog.symbols.count()))
    dialog.search.setText("no-such-symbol")
    assert not dialog.apply_button.isEnabled()
    dialog.search.setText(color.code)
    assert dialog.apply_button.isEnabled()
    assert color.code in dialog.selection.text()
    dialog.reject()
    assert w.draft.entities["mapping"] == mapping

    def accept_target(self):
        for row in range(self.symbols.count()):
            item = self.symbols.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == target:
                self.symbols.setCurrentItem(item)
                assert "DMC" in item.text() or "TEST" in item.text()
        return 1

    monkeypatch.setattr(SymbolPicker, "exec", accept_target)
    w.change_symbol()
    assert w.draft.entities["mapping"][color.key] == target
    assert w.draft.entities["mapping"][other] == mapping[color.key]
    assert w.design.content_sha == before
    assert w.draft.entities["manual_mapping"]["bindings"][other] == mapping[color.key]
    assert len([k for k in w.draft.entities if k.startswith("mapping:")]) == 1
    w.dirty = False
    w.close()


def test_refresh_never_compresses_or_rebuilds_unchanged_palette(qtbot, monkeypatch):
    w = Workbench()
    qtbot.addWidget(w)
    item = w.canvas._grid_item
    first = w.table.item(0, 1)
    record, _ = settings_document(w.draft, "resources")
    record.pop("origin_project_id")
    from stitchpress.app.commerce_templates import template_font_sha

    record["commerce_font_sha"] = template_font_sha(w.draft)
    expected = digest(record)
    assert post_fingerprint(w.draft) == expected

    def forbidden(*_, **__):
        raise AssertionError("UI refresh must not serialize the project")

    monkeypatch.setattr(Project, "encode", forbidden)
    monkeypatch.setattr("stitchpress.app.symbol_picker.library_from_assets", forbidden)
    for _ in range(20):
        w.refresh()
        w.show_page(0)
        w.show_page(3)
    assert w.canvas._grid_item is item
    assert w.table.item(0, 1) is first
    assert post_fingerprint(w.draft) == expected
    w.draft.entities["metadata"]["title"] = "修改后"
    w.refresh()
    assert w.document_title.text().startswith("修改后")
    assert post_fingerprint(w.draft) != expected
    w.close()


def test_artwork_cache_detects_asset_replacement_and_keeps_one_library(qtbot):
    from stitchpress.core.resources import demo_library, library_assets

    w = Workbench()
    qtbot.addWidget(w)
    old = w.symbol_library()
    w.symbol_artwork.icon(old.symbols[0].key)
    record, assets = library_assets(demo_library(10))
    w.draft.entities["library"] = record
    w.draft.assets.update(assets)
    assert len(w.symbol_library().symbols) == 10
    assert not w.symbol_artwork.icons
    assert w.symbol_library() is not old
    assert len(w.symbol_artwork.sources) == 10
    w.close()


def test_large_draft_checkpoint_runs_outside_ui_and_recovers_latest_form(
    qtbot, tmp_path, monkeypatch
):
    from stitchpress.app.recovery import RecoveryStore

    monkeypatch.setattr("stitchpress.app.local_features.settings_dir", lambda: tmp_path)
    w = Workbench()
    qtbot.addWidget(w)
    # An opaque, incompressible resource models a large imported image.
    import random

    w.draft.assets["sources/large.bin"] = random.Random(7).randbytes(9 * 1024**2)
    w.title_edit.setText("后台恢复测试")
    w.checkpoint_draft()
    process = w._recovery_process
    assert process is not None
    w.show_page(0)
    w.table.selectRow(0)
    assert w.symbol_button.isEnabled()
    w.checkpoint_draft()
    assert w._recovery_process is process
    qtbot.waitUntil(lambda: w._recovery_process is None, timeout=30000)
    records = RecoveryStore(tmp_path / "recovery").candidates()
    restored = RecoveryStore.restore(records[0])
    assert restored.entities["recovery_form"]["title"] == "后台恢复测试"
    assert not list((tmp_path / "recovery-work").glob("*/input.stitchpress"))
    w.refresh()
    w.dirty = False
    w.close()


def test_uncompressed_worker_snapshot_preserves_validation_and_contents():
    project = demo_project()
    decoded = Project.decode(project.encode(compression=ZIP_STORED))
    assert digest(decoded.entities) == digest(project.entities)
    assert decoded.assets == project.assets
    invalid = copy.deepcopy(project)
    invalid.entities["fabric"]["sx"] = "invalid"
    import pytest

    from stitchpress.core.errors import StitchPressError

    with pytest.raises(StitchPressError):
        invalid.encode(compression=ZIP_STORED)


def test_coordinate_selection_accepts_reversed_bounds(qtbot, monkeypatch):
    from PySide6.QtWidgets import QDialog, QSpinBox

    w = Workbench()
    qtbot.addWidget(w)

    def choose(dialog):
        controls = dialog.findChildren(QSpinBox)
        for control, value in zip(controls, (10, 12, 3, 4), strict=True):
            control.setValue(value)
        assert {b.text() for b in dialog.findChildren(QDialogButtonBox)[0].buttons()} == {
            "选择区域",
            "取消",
        }
        return 1

    monkeypatch.setattr(QDialog, "exec", choose)
    w.choose_grid_region()
    assert w.region == (2, 3, 10, 12)
    assert w.patch_button.isEnabled()
    w.close()


def test_project_switch_cancels_old_recovery_without_publishing_it(qtbot, tmp_path, monkeypatch):
    from stitchpress.app.recovery import RecoveryStore

    monkeypatch.setattr("stitchpress.app.local_features.settings_dir", lambda: tmp_path)
    w = Workbench()
    qtbot.addWidget(w)
    w.draft.assets["sources/large.bin"] = b"a" * (9 * 1024**2)
    w.title_edit.setText("不应回写的旧作品")
    w.checkpoint_draft()
    assert w._recovery_process is not None
    w.set_project(demo_project())
    assert w._recovery_process is None
    qtbot.waitUntil(
        lambda: not list((tmp_path / "recovery-work").glob("*/input.stitchpress")), timeout=10000
    )
    assert not RecoveryStore(tmp_path / "recovery").candidates()
    w.close()


def test_background_save_keeps_edits_made_while_compressing(qtbot, tmp_path, monkeypatch):
    from stitchpress.app.store import Coordinator

    monkeypatch.setattr("stitchpress.app.background_save.settings_dir", lambda: tmp_path)
    w = Workbench()
    qtbot.addWidget(w)
    errors = []
    monkeypatch.setattr(w, "error", errors.append)
    project = demo_project()
    project.assets["sources/large.bin"] = b"a" * (9 * 1024**2)
    path = tmp_path / "保存测试.stitchpress"
    coordinator = Coordinator(path, project)
    coordinator.commit(expected_seq=0, reads={}, changes={})
    w.set_project(coordinator.project, path, coordinator)
    w.title_edit.setText("第一份保存快照")
    w.draft.entities["freeze"] = None  # Legacy undo left a deletion marker in the draft.
    assert w.request_save()
    assert w._save_process is not None
    w.title_edit.setText("保存期间继续修改")
    w.apply_fields()
    w.show_page(0)
    qtbot.waitUntil(lambda: w._save_process is None, timeout=30000)
    assert not errors
    assert Project.decode(path.read_bytes()).entities["metadata"]["title"] == "第一份保存快照"
    assert "freeze" not in Project.decode(path.read_bytes()).entities
    assert w.draft.entities["metadata"]["title"] == "保存期间继续修改"
    assert w.dirty
    assert w.request_save()
    qtbot.waitUntil(lambda: w._save_process is None, timeout=30000)
    assert Project.decode(path.read_bytes()).entities["metadata"]["title"] == "保存期间继续修改"
    assert not w.dirty
    assert w.request_save() and w._save_process is None
    assert not list((tmp_path / "save-work").glob("*/input.stitchpress"))
    w.close()


def test_large_save_as_is_background_and_preserves_later_edits(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("stitchpress.app.background_save.settings_dir", lambda: tmp_path)
    w = Workbench()
    qtbot.addWidget(w)
    w.draft.assets["sources/large.bin"] = b"a" * (9 * 1024**2)
    original_id = w.draft.project_id
    target = tmp_path / "大型作品副本.stitchpress"
    monkeypatch.setattr(
        "stitchpress.app.desktop.QFileDialog.getSaveFileName", lambda *args: (str(target), "")
    )

    def no_ui_compression(*args, **kwargs):
        raise AssertionError("主界面不应编码大型作品")

    monkeypatch.setattr(Project, "encode", no_ui_compression)
    w.title_edit.setText("另存时快照")
    assert w.save_as()
    assert w._save_process is not None
    assert not target.exists()
    assert w.draft.project_id == original_id
    w.title_edit.setText("保存期间继续编辑")
    w.show_page(0)
    qtbot.waitUntil(lambda: w._save_process is None, timeout=30000)
    saved = Project.decode(target.read_bytes())
    assert saved.entities["metadata"]["title"] == "另存时快照"
    assert saved.project_id != original_id
    assert w.draft.project_id == saved.project_id
    assert w.title_edit.text() == "保存期间继续编辑"
    assert w.dirty
    assert w.project_path == target
    assert not list((tmp_path / "save-work").glob("*/snapshot.json"))
    w.dirty = False
    w.title_edit.setText(saved.entities["metadata"]["title"])
    w.close()


def test_background_copy_does_not_overwrite_external_file(qtbot, tmp_path, monkeypatch):
    from stitchpress.app.background_save import request_save

    monkeypatch.setattr("stitchpress.app.background_save.settings_dir", lambda: tmp_path)
    w = Workbench()
    qtbot.addWidget(w)
    errors = []
    monkeypatch.setattr(w, "error", errors.append)
    original_id = w.draft.project_id
    target = tmp_path / "外部文件.stitchpress"
    assert request_save(w, copy_target=target)
    target.write_bytes(b"external file")
    qtbot.waitUntil(lambda: w._save_process is None, timeout=30000)
    assert target.read_bytes() == b"external file"
    assert errors and "外部修改" in str(errors[0])
    assert w.draft.project_id == original_id and w.project_path is None
    assert w._save_cleanup is None
    w.close()


def test_stopping_background_copy_keeps_source_and_releases_destination(
    qtbot, tmp_path, monkeypatch
):
    from stitchpress.app.background_save import request_save
    from stitchpress.app.store import Coordinator

    monkeypatch.setattr("stitchpress.app.background_save.settings_dir", lambda: tmp_path)
    w = Workbench()
    qtbot.addWidget(w)
    original_id = w.draft.project_id
    target = tmp_path / "取消的另存.stitchpress"
    assert request_save(w, copy_target=target)
    errors = []
    monkeypatch.setattr(w, "error", errors.append)
    assert not w.dirty and not w.confirm_discard()
    assert errors and "后台保存" in str(errors[-1])
    w.stop_save_worker()
    assert w._save_process is None and not target.exists()
    assert w.draft.project_id == original_id and w.project_path is None
    replacement = Coordinator(target, w.draft)
    replacement.close()
    qtbot.waitUntil(
        lambda: not list((tmp_path / "save-work").glob("*/snapshot.json")), timeout=10000
    )
    w.close()


def test_prepared_save_cannot_bypass_content_or_external_change_checks(tmp_path):
    import pytest

    from stitchpress.app.store import Coordinator
    from stitchpress.core.errors import ConflictError

    project = demo_project()
    path = tmp_path / "校验保存.stitchpress"
    writer = Coordinator(path, project)
    original = writer.commit(expected_seq=0, reads={}, changes={})
    prepared = copy.deepcopy(original)
    prepared.commit_seq += 1
    prepared.entities["metadata"]["title"] = "不匹配的后台快照"
    with pytest.raises(ConflictError, match="快照"):
        writer.commit(expected_seq=1, reads={}, changes={}, prepared_payload=prepared.encode())
    assert Project.decode(path.read_bytes()).commit_seq == 1
    prepared.entities["metadata"] = original.entities["metadata"]
    path.write_bytes(b"external change")
    with pytest.raises(ConflictError, match="外部修改"):
        writer.commit(expected_seq=1, reads={}, changes={}, prepared_payload=prepared.encode())
    writer.close()
