import io
import json
from dataclasses import asdict

import numpy as np
import pytest
from PIL import Image, ImageCms
from PySide6.QtCore import QRectF

from stitchpress.app.assets import merge_assets, remap_assets
from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project
from stitchpress.app.local_features import SpecificationDialog
from stitchpress.app.recovery import RecoveryStore
from stitchpress.app.worker import run
from stitchpress.core.configuration import ReferenceSettings
from stitchpress.core.conversion import ConversionOptions, convert_image
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, sha256
from stitchpress.core.layout import Fabric
from stitchpress.core.masks import annotation_masks


def test_resource_merge_preserves_both_catalog_revisions():
    original = {"resources/catalog.json": b"old"}
    incoming = {"resources/catalog.json": b"new"}
    assets, aliases = merge_assets(original, incoming)
    record = remap_assets({"asset": "resources/catalog.json"}, aliases)
    assert assets["resources/catalog.json"] == b"old"
    assert assets[record["asset"]] == b"new"
    assert original["resources/catalog.json"] == b"old"


def test_recovery_includes_unfinished_invalid_form_without_corrupting_master(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr("stitchpress.app.local_features.settings_dir", lambda: tmp_path)
    window = Workbench()
    qtbot.addWidget(window)
    original = window.design.content_sha
    window.sx.setText("unfinished-")
    window.title_edit.setText("尚未离开输入框的标题")
    window.checkpoint_draft()
    record = RecoveryStore(tmp_path / "recovery").candidates()[0]
    restored = RecoveryStore.restore(record)
    assert restored.entities["recovery_form"]["sx"] == "unfinished-"
    assert restored.entities["recovery_form"]["title"] == "尚未离开输入框的标题"
    assert restored.entities["fabric"]["sx"] == "0.181"
    assert window.design.content_sha == original
    window.refresh()
    window.close()


def test_specification_dialog_uses_snapshot_and_does_not_apply_on_cancel(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr("stitchpress.app.local_features.settings_dir", lambda: tmp_path)
    window = Workbench()
    qtbot.addWidget(window)
    before = canonical_bytes(window.draft.entities)
    dialog = SpecificationDialog(window)
    qtbot.addWidget(dialog)
    dialog.fill(
        Fabric(sx="0.18", sy="0.19", left="1", right="2"),
        ReferenceSettings(paper="A3", scale="2", overlap=4),
    )
    fabric, reference = dialog.values()
    assert fabric.right == "2"
    assert reference.paper_cm == ("29.7", "42")
    dialog.reject()
    assert canonical_bytes(window.draft.entities) == before
    window.close()


def test_protected_region_cannot_be_patched_and_old_annotation_survives_clear(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr("stitchpress.app.local_features.settings_dir", lambda: tmp_path)
    window = Workbench()
    qtbot.addWidget(window)
    errors = []
    monkeypatch.setattr(window, "error", errors.append)
    original = window.design.content_sha
    window.select_region(QRectF(200, 300, 80, 80))
    window.annotate_region("protected")
    window.patch_color.setCurrentIndex(0)
    window.patch_region()
    assert errors and "保护区" in str(errors[-1])
    assert window.design.content_sha == original
    window.clear_annotations()
    assert "annotations" not in window.draft.entities
    assert any(k.startswith("annotations:") for k in window.draft.entities)
    window.patch_region()
    assert window.design.content_sha != original
    window.undo_patch()
    window.dirty = False
    window.close()


def test_masks_reject_context_drift_and_conflicting_blank_subject():
    record = {
        "schema_version": 1,
        "grid": [4, 3],
        "source_sha": "source",
        "regions": [{"kind": "blank", "region": [0, 0, 2, 2]}],
    }
    masks = annotation_masks(record, 4, 3, "source")
    assert int(masks["blank"].sum()) == 4
    with pytest.raises(StitchPressError, match="语境"):
        annotation_masks(record, 5, 3, "source")
    with pytest.raises(StitchPressError, match="来源"):
        annotation_masks(record, 4, 3, "different")
    record["regions"].append({"kind": "subject", "region": [1, 1, 2, 2]})
    with pytest.raises(StitchPressError, match="冲突"):
        annotation_masks(record, 4, 3, "source")


def test_explicit_input_icc_and_blank_mask_are_in_conversion_fingerprint(minimal):
    image = Image.new("RGB", (3, 2), "white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    blank = np.zeros((2, 3), dtype=bool)
    blank[0, 0] = True
    design, record = convert_image(
        buffer.getvalue(),
        minimal.palette,
        ConversionOptions(3, 2, 2),
        blank_mask=blank,
        override_icc=icc,
    )
    assert design.labels[0][0] == -1
    assert sum(design.counts) == 5
    assert record["source_icc_sha"] == sha256(icc)
    assert record["blank_mask_sha"] == sha256(blank.tobytes())


@pytest.mark.parametrize("operation", ["plan", "preview", "quality"])
def test_local_worker_plan_preview_and_quality_are_read_only(tmp_path, operation):
    project = demo_project(12, 16, 3)
    project.entities["reference"] = asdict(ReferenceSettings(scale="2", style="pale", overlap=1))
    original = project.encode()
    path = tmp_path / "snapshot.stitchpress"
    path.write_bytes(original)
    output = tmp_path / "result.json"
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            {
                "operation": operation,
                "project": str(path),
                "page_number": 1,
                "output": str(output),
                "base_sha": "base",
            }
        )
    )
    assert run(job) == 0
    result = json.loads(output.read_bytes())
    assert result["status"] == "completed"
    if operation == "preview":
        with Image.open(result["result"]["preview"]) as image:
            assert image.size == (2480, 3508)
    assert path.read_bytes() == original


def test_margin_units_use_actual_axis_pitch_and_preserve_unchanged_cm(qtbot, monkeypatch):
    from dataclasses import asdict

    from stitchpress.app.local_features import SpecificationDialog
    from stitchpress.core.layout import Fabric

    window = Workbench()
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    qtbot.addWidget(window)
    window.draft.entities["fabric"] = asdict(Fabric(sx="0.2", sy="0.25", left="2", top="2.5"))
    dialog = SpecificationDialog(window)
    qtbot.addWidget(dialog)
    dialog.margin_unit.setCurrentIndex(dialog.margin_unit.findData("cells"))
    assert dialog.fabric_fields["left"].text() == "10"
    assert dialog.fabric_fields["top"].text() == "10"
    dialog.fabric_fields["sx"].setText("0.3")
    fabric, _ = dialog.values()
    assert fabric.left == "3.0" and fabric.top == "2.5"
    dialog.margin_unit.setCurrentIndex(dialog.margin_unit.findData("mm"))
    fabric, _ = dialog.values()
    assert fabric.left == "3.0" and fabric.top == "2.5"
    dialog.dpi.setValue(600)
    assert dialog.values()[0].dpi == 600
    assert window.draft.entities["fabric"]["dpi"] == 300
    dialog.reject()
    window.close()
