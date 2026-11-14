from dataclasses import asdict
from decimal import Decimal

import pytest
from PIL import Image

from stitchpress.app.desktop import Workbench
from stitchpress.app.store import Project
from stitchpress.core.layout import Fabric, physical_layout
from stitchpress.core.render import Renderer
from stitchpress.core.resources import assign_symbols, demo_library


@pytest.mark.parametrize(
    "ct,pitch",
    [
        ("9", "0.282222"),
        ("11", "0.230909"),
        ("14", "0.181429"),
        ("16", "0.158750"),
        ("18", "0.141111"),
    ],
)
def test_customer_ct_table_has_exact_saved_precision_and_physical_size(ct, pitch):
    fabric = Fabric.from_ct(ct)
    assert fabric.nominal_ct == ct and fabric.sx == fabric.sy == pitch
    assert fabric.source == "customer-ct-pitches-2026-11-14"
    layout = physical_layout(100, 200, fabric)
    assert Decimal(layout.width_cm) == Decimal(pitch) * 100
    assert Decimal(layout.height_cm) == Decimal(pitch) * 200
    assert layout.scale100_cm == (layout.width_cm, layout.width_cm)


def test_other_ct_keeps_theoretical_calculation_and_existing_calibration():
    assert Fabric.from_ct("10") == Fabric.theoretical("10")
    assert Fabric.from_ct("09.0") == Fabric.from_ct("9")
    # Existing calibrated projects must not be migrated to the new rounded CT table.
    old = Fabric()
    assert old.sx == old.sy == "0.181" and old.source == "factory-v10-actual-pitch"
    assert Fabric.from_ct("14").sx != old.sx


def test_nine_ct_button_is_pending_until_applied_and_roundtrips(qtbot):
    w = Workbench()
    qtbot.addWidget(w)
    original = dict(w.draft.entities["fabric"])
    design_sha, counts = w.design.content_sha, w.design.counts
    panel = w.spec_panel
    assert list(panel.editor.ct_buttons) == ["9", "11", "14", "16", "18"]
    panel.editor.ct_buttons["9"].click()
    fabric = panel.editor.fabric_values()
    assert fabric.nominal_ct == "9" and fabric.sx == fabric.sy == "0.282222"
    assert [getattr(fabric, k) for k in ("left", "top", "right", "bottom")] == [
        original[k] for k in ("left", "top", "right", "bottom")
    ]
    assert panel.pending() and w.draft.entities["fabric"] == original
    panel.reload()  # Cancel must restore the original production calibration.
    assert panel.editor.fabric_values().sx == original["sx"]
    panel.editor.ct_buttons["9"].click()
    panel.apply()
    assert w.draft.entities["fabric"] == asdict(fabric)
    restored = Project.decode(w.draft.encode())
    assert restored.entities["fabric"] == asdict(fabric)
    assert w.design.content_sha == design_sha and w.design.counts == counts
    w.dirty = False
    w.close()


def test_manual_nine_ct_uses_same_table_and_keeps_cell_margin_semantics(qtbot):
    w = Workbench()
    qtbot.addWidget(w)
    d = w.spec_panel.editor
    d.margin_unit.setCurrentIndex(d.margin_unit.findData("cells"))
    d.fabric_fields["nominal_ct"].setText("9")
    d.theoretical()
    fabric = d.fabric_values()
    assert fabric.sx == "0.282222" and Decimal(fabric.left) == Decimal("8.466660")
    assert fabric.margin_unit == "cells"  # Original 30 cells remain 30 cells at 9 CT.
    assert w.draft.entities["fabric"]["sx"] == "0.181"
    w.spec_panel.reload()
    w.close()


def test_nine_ct_tiff_uses_customer_pitch_and_preserves_stitches(tmp_path, minimal):
    library = demo_library(2)
    fabric = Fabric.from_ct("9")
    renderer = Renderer(minimal, library, assign_symbols(minimal, library), fabric)
    image, raster = renderer.production()
    path = tmp_path / "9CT.tif"
    image.save(path, compression="tiff_lzw", dpi=(300, 300))
    image.close()
    with Image.open(path) as reopened:
        assert reopened.info["dpi"] == (300, 300)
        assert reopened.size == renderer.layout.canvas_px
        assert renderer.verify_cells(reopened, raster)["stitches"] == sum(minimal.counts)
    assert Decimal(renderer.layout.width_cm) == Decimal("0.846666")
    assert Decimal(renderer.layout.scale100_cm[0]) == Decimal("28.222200")
