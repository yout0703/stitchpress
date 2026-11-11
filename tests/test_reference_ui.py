from pathlib import Path

import pytest
from PIL import Image

from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project
from stitchpress.app.reference_actions import bind_symbol, import_pattern, import_symbol_folder
from stitchpress.app.reference_package import render_context
from stitchpress.app.reference_preview import preview_reference
from stitchpress.core.configuration import ReferenceSettings
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes
from stitchpress.core.models import Design
from stitchpress.core.reading import reading_layout
from stitchpress.core.resources import assign_symbols, library_from_assets
from stitchpress.core.streaming import save_production_stream


def test_five_workspaces_apply_cancel_conflict_and_project_switch(qtbot, monkeypatch):
    w = Workbench()
    qtbot.addWidget(w)
    errors = []
    monkeypatch.setattr(w, "error", errors.append)
    assert w.pages.count() == 9 and w.pages.currentIndex() == 3
    for index, button in zip((3, 4, 6, 8, 7), w.reference_nav, strict=True):
        button.setChecked(True)  # Native AX toggles without emitting clicked().
        assert w.pages.currentIndex() == index and button.isChecked()
    original_fabric = dict(w.draft.entities["fabric"])
    d = w.spec_panel.editor
    d.reading_dpi.setValue(150)
    d.reading_cell.setText("2.5")
    assert w.reference_pending()
    assert not w.save() and errors
    w.spec_panel.apply()
    assert w.draft.entities["reference"]["dpi"] == 150
    assert w.draft.entities["fabric"] == original_fabric
    assert not w.reference_pending()
    w.spec_panel.editor.reading_dpi.setValue(600)
    w.spec_panel.reload()  # Page Cancel restores the applied state.
    assert w.spec_panel.editor.reading_dpi.value() == 150
    w.spec_panel.editor.reading_dpi.setValue(300)
    w.draft.entities["reference"]["dpi"] = 200
    w.spec_panel.apply()
    assert w.draft.entities["reference"]["dpi"] == 200 and "别处改变" in str(errors[-1])
    w.set_project(demo_project())
    assert not w.reference_pending() and w.spec_panel.editor.reading_dpi.value() == 0
    w.close()


def test_thread_text_commerce_pages_save_independent_state(qtbot, monkeypatch):
    w = Workbench()
    qtbot.addWidget(w)
    errors = []
    monkeypatch.setattr(w, "error", errors.append)
    w.add_thread_row(
        plan=dict(
            ct="14",
            stitches_per_metre="180",
            source_note="自有数字测试，非客户系数",
            confirmed=True,
        )
    )
    w.apply_thread_rows()
    assert len(w.draft.entities["post"]["thread_plans"]) == 1
    w.post_targets["threads"].setChecked(True)
    w.apply_post_options()
    d = w.text_panel.editor
    d.extra_controls["symbols_top"]["enabled"].setChecked(False)
    w.text_panel.apply()
    assert not w.draft.entities["text_layers"]["symbols_top"]["enabled"]
    d = w.commerce_panel.editor
    d.detail_fields["detail_width"].setValue(1500)
    d.detail_fields["detail_dpi"].setValue(200)
    d.art_source.setCurrentIndex(d.art_source.findData("original"))
    d.scenes[1]["scene"].setCurrentIndex(8)
    w.commerce_panel.apply()
    saved = w.draft.entities["commerce"]
    assert saved["detail_width"] == 1500 and saved["detail_dpi"] == 200
    assert saved["scenes"][1]["scene"] == 8 and saved["scenes"][0]["scene"] != 8
    assert saved["art_source"] == "original"
    assert not errors and not w.reference_pending()
    w.dirty = False
    w.close()


def test_import_swap_manual_binding_and_failed_folder_are_transactional(tmp_path):
    initial = demo_project()
    initial.entities["mapping"] = assign_symbols(
        Design.from_dict(initial.entities["design"]),
        library_from_assets(initial.entities["library"], initial.assets),
    )
    before = canonical_bytes(initial.entities)
    value = import_pattern(initial, "fixtures/s01-independent.xsd")
    assert canonical_bytes(initial.entities) == before
    assert (
        value.assets[value.entities["pattern_input"]["asset"]]
        == Path("fixtures/s01-independent.xsd").read_bytes()
    )
    keys = list(value.entities["mapping"])
    old = dict(value.entities["mapping"])
    bind_symbol(value, keys[0], old[keys[1]])
    assert value.entities["mapping"][keys[1]] == old[keys[0]]
    assert value.entities["manual_mapping"]["bindings"] == value.entities["mapping"]
    value.encode()
    lib = library_from_assets(value.entities["library"], value.assets)
    folder = tmp_path / "symbols"
    folder.mkdir()
    for name, data in [
        ("10.tif", lib.symbols[0].tile),
        ("2.tif", lib.symbols[1].tile),
        ("0.1.tif", lib.symbols[2].tile),
    ]:
        (folder / name).write_bytes(data)
    imported = import_symbol_folder(value, folder)
    assert [e["external_id"] for e in imported["record"]["entries"]] == ["0.1", 2, 10]
    prior = canonical_bytes(value.entities)
    (folder / "3.tif").write_bytes(lib.symbols[1].tile)
    with pytest.raises(StitchPressError, match="重复"):
        import_symbol_folder(value, folder)
    assert canonical_bytes(value.entities) == prior


def test_reading_dpi_margins_styles_do_not_change_production_pixels_and_bigtiff(tmp_path):
    value = import_pattern(demo_project(), "fixtures/s01-independent.xsd")
    _, renderer = render_context(value)
    full, _ = renderer.production()
    baseline = full.tobytes()
    for dpi in (72, 150, 300, 600):
        for style in ("symbols", "pale", "color"):
            config = ReferenceSettings(
                dpi=dpi, cell_mm="2.5", margins_mm=("5", "10", "20", "15"), style=style
            )
            reading, plan = reading_layout(renderer, config)
            image, raster = reading.reference(plan.pages[0], plan, style=style)
            assert reading.verify_cells(image, raster)["stitches"] == 6
            assert reading.fabric.dpi == dpi
            assert raster.xs[0] == plan.origin[0]
            image.close()
    second, _ = renderer.production()
    assert second.tobytes() == baseline
    checks = save_production_stream(renderer, tmp_path / "big.tif", bigtiff=True)
    import tifffile

    with tifffile.TiffFile(tmp_path / "big.tif") as actual:
        assert actual.is_bigtiff
    assert checks["bigtiff"] and checks["stitches"] == 6
    full.close()
    second.close()


def test_each_reference_preview_uses_real_target_and_rejects_bad_page(tmp_path):
    value = import_pattern(demo_project(), "fixtures/s01-independent.xsd")
    value.entities["reference"] = ReferenceSettings(dpi=150, cell_mm="2.5").to_dict()
    for target in ("production", "whole", "pages", "symbols", "card", "effect"):
        result = preview_reference(value, target, tmp_path / f"{target}.png")
        with Image.open(result["preview"]) as actual:
            assert actual.format == "PNG" and actual.mode == "RGB"
            if target == "pages":
                assert actual.size == (1240, 1754)
        assert not result["checks"]["partial"]
    with pytest.raises(StitchPressError, match="页码"):
        preview_reference(value, "pages", tmp_path / "bad.png", 99)


def test_tiny_production_legend_does_not_overlap_measured_ruler(monkeypatch):
    from math import ceil

    from PIL import ImageDraw

    value = import_pattern(demo_project(), "fixtures/s01-independent.xsd")
    _, renderer = render_context(value)
    boxes = {}
    original = renderer.text

    def record(image, xy, value, size, anchor="lt"):
        boxes.setdefault(value, []).append(
            ImageDraw.Draw(image).textbbox(
                xy, value, font=renderer.font(ceil(size * renderer.fabric.dpi / 300)), anchor=anchor
            )
        )
        return original(image, xy, value, size, anchor)

    monkeypatch.setattr(renderer, "text", record)
    image, _ = renderer.production()
    ruler = next(key for key in boxes if key.startswith("4 cells ="))
    a = boxes[ruler][0]
    for key in ("1", "2", "310", "B5200", "No. / Symbol / DMC"):
        for b in boxes[key]:
            assert a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3]
    image.close()


def test_accessibility_edits_update_file_fields_and_correspondence(qtbot):
    w = Workbench()
    qtbot.addWidget(w)
    w.draft.assets["test-source"] = b"not-rendered-in-this-state-test"
    w.draft.entities["source"] = dict(asset="test-source")
    w.refresh()
    assert not w.dirty
    w.source_confirm.setChecked(True)
    assert w.draft.entities["source"]["correspondence_confirmed"] is True
    w.ref_code.setText("000026")
    assert w.apply_fields()
    assert w.draft.entities["metadata"]["code"] == "000026"
    w.source_confirm.setChecked(False)
    assert w.draft.entities["source"]["correspondence_confirmed"] is False
    w.draft.entities.pop("source")
    w.dirty = False
    w.close()


def test_local_worker_completion_clears_running_status(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QProcess

    from stitchpress.app.task_history import context_fingerprint

    w = Workbench()
    qtbot.addWidget(w)
    results = []
    monkeypatch.setattr(w, "show_post_plan", results.append)
    output = tmp_path / "result.json"
    output.write_bytes(
        canonical_bytes(
            dict(status="completed", result=dict(marker="plan"), base_sha=w.design.content_sha)
        )
    )
    w.job = dict(
        operation="post-plan",
        output=output,
        project_id=w.draft.project_id,
        base_sha=w.design.content_sha,
        include_image=False,
        source_sha=None,
        context_sha=context_fingerprint(w, "post-plan", False),
    )
    w.statusBar().showMessage("后台任务运行中")
    w.worker_finished(0, QProcess.ExitStatus.NormalExit)
    assert results == [dict(marker="plan")]
    assert w.statusBar().currentMessage() == "本地任务已完成"
    w.close()


def test_export_dialog_default_and_custom_directory_reach_worker(qtbot, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QInputDialog

    from stitchpress.core.identity import valid_directory_name
    from stitchpress.core.post import PostSettings

    w = Workbench()
    qtbot.addWidget(w)
    errors, launched = [], []
    monkeypatch.setattr(w, "error", errors.append)
    monkeypatch.setattr(
        w, "start_worker", lambda operation, fields: launched.append((operation, fields))
    )
    w.post_folder.setText(str(tmp_path))
    w.post_demo.setChecked(True)
    options = PostSettings(targets=("production",))
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: (kwargs["text"], True))
    w.export_reference_output(options=options)
    assert not errors and launched[-1][0] == "reference-export"
    assert Path(launched[-1][1]["target"]).parent == tmp_path
    for name in ("客户甲-第二次出图", "CT242-qa"):
        monkeypatch.setattr(
            QInputDialog, "getText", lambda *args, selected=name, **kwargs: (selected, True)
        )
        w.export_reference_output(options=options)
        assert Path(launched[-1][1]["target"]).name == name
    count = len(launched)
    for name in ("../escape", "a/b", "a\\b", "NUL", "CON.txt", "bad.", ".", "..", "x" * 241):
        assert not valid_directory_name(name)
        monkeypatch.setattr(
            QInputDialog, "getText", lambda *args, selected=name, **kwargs: (selected, True)
        )
        w.export_reference_output(options=options)
    assert len(launched) == count and len(errors) == 9
    w.dirty = False
    w.close()
