import io
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QTimer

from stitchpress.app.background_load import cancel_load, start_load
from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project
from stitchpress.app.store import Coordinator, Project
from stitchpress.core.configuration import ReferenceSettings
from stitchpress.core.conversion import source_preview
from stitchpress.core.errors import ConflictError
from stitchpress.core.identity import image_pixel_sha, sha256
from stitchpress.core.importers import export_oxs, import_oxs
from stitchpress.core.layout import Fabric
from stitchpress.core.models import new_design
from stitchpress.core.reading import reading_layout
from stitchpress.core.render import Renderer, stitch_preview
from stitchpress.core.resources import assign_symbols, demo_library
from stitchpress.core.streaming import reference_overview, save_reference_stream


@pytest.mark.parametrize("style", ["symbols", "pale", "color"])
@pytest.mark.parametrize("bigtiff", [False, True])
def test_reading_strips_match_full_raster_and_every_cell(tmp_path, minimal, style, bigtiff):
    library = demo_library(2)
    r = Renderer(minimal, library, assign_symbols(minimal, library), Fabric(), code="客户001")
    reading, plan = reading_layout(r, ReferenceSettings(dpi=72))
    expected, _ = reading.reference(plan.pages[0], plan, style=style)
    path = tmp_path / "whole.tif"
    checks = save_reference_stream(reading, path, plan.pages[0], plan, style=style, bigtiff=bigtiff)
    assert checks["counts"] == list(minimal.counts)
    assert checks["maximum_band_bytes"] <= 8 * 1024**2
    with Image.open(path) as actual:
        assert actual.tobytes() == expected.tobytes()
    overview, info = reference_overview(reading, plan.pages[0], plan, style=style, max_edge=600)
    xs = np.minimum(
        expected.width - 1,
        ((np.arange(overview.width) + 0.5) * expected.width / overview.width).astype(int),
    )
    ys = np.minimum(
        expected.height - 1,
        ((np.arange(overview.height) + 0.5) * expected.height / overview.height).astype(int),
    )
    assert np.array_equal(
        np.asarray(overview), np.asarray(expected.convert("RGB"))[ys[:, None], xs[None, :]]
    )
    assert info["partial"] is False
    overview.close()
    expected.close()


def test_million_cell_oxs_roundtrip_exceeds_both_old_limits(minimal):
    row = tuple(i % 2 for i in range(1100))
    design = new_design(1100, 1000, minimal.palette, (row,) * 1000)
    raw = export_oxs(design)
    assert len(raw) > 16 * 1024**2 and design.width * design.height > 1_000_000
    restored = import_oxs(raw)
    assert restored.labels == design.labels and restored.counts == (550000, 550000)
    assert [(c.brand, c.code, c.rgb) for c in restored.palette] == [
        (c.brand, c.code, c.rgb) for c in design.palette
    ]  # OXS embeds its own catalog revision; grid/thread semantics must remain identical.
    image = stitch_preview(restored, max_edge=1200)
    assert max(image.size) <= 1200 and image.width * 4 == image.height * 3
    image.close()


def test_source_preview_resizes_before_normalization_without_changing_source():
    source = Image.new("RGB", (6500, 6500), (80, 120, 160))  # 42.25MP, beyond old 40MP.
    buffer = io.BytesIO()
    source.save(buffer, "JPEG")
    source.close()
    raw = buffer.getvalue()
    before = sha256(raw)
    preview = source_preview(raw, max_edge=800)
    assert preview.size == (800, 800) and sha256(raw) == before
    assert image_pixel_sha(preview) == sha256(preview.tobytes())
    preview.close()


def test_encoded_verification_does_not_decode_duplicate_assets_and_rejects_tamper(monkeypatch):
    project = Project.create("stream check")
    project.assets["large.bin"] = b"abc" * 100000
    monkeypatch.setattr(Project, "decode", lambda *_: pytest.fail("duplicated full Project"))
    payload = project.encode()
    modified = io.BytesIO()
    with ZipFile(io.BytesIO(payload)) as source, ZipFile(modified, "w") as target:
        for entry in source.infolist():
            target.writestr(
                entry.filename, b"modified" if entry.filename == "large.bin" else source.read(entry)
            )
    with pytest.raises(ConflictError, match="SHA"):
        project.verify_encoded(modified.getvalue())


def test_worker_loaded_source_must_not_change_before_lock(tmp_path):
    path = tmp_path / "source.stitchpress"
    path.write_bytes(demo_project().encode())
    project = Project.decode(path.read_bytes())
    path.write_bytes(Project.create("external replacement").encode())
    with pytest.raises(ConflictError, match="修改"):
        Coordinator.from_loaded(path, project)
    assert Project.decode(path.read_bytes()).entities["metadata"]["title"] == "external replacement"


def test_background_import_keeps_events_responsive_and_cancel_preserves_draft(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr("stitchpress.app.background_load.settings_dir", lambda: tmp_path)
    w = Workbench()
    qtbot.addWidget(w)
    errors = []
    monkeypatch.setattr(w, "error", errors.append)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    start_load(w, Path("fixtures/s01-independent.xsd"), "import-pattern")
    qtbot.waitUntil(lambda: getattr(w, "_load_process", None) is None, timeout=60000)
    assert not errors and len(ticks) > 2 and sum(w.design.counts) == 6
    before = w.design.content_sha
    start_load(w, Path("fixtures/s01-independent.xsd"), "import-pattern")
    cancel_load(w)
    qtbot.waitUntil(lambda: getattr(w, "_load_process", None) is None, timeout=60000)
    assert w.design.content_sha == before and not errors
    timer.stop()
    w.dirty = False
    w.close()


def test_background_open_adopts_locked_file_without_redecoding(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("stitchpress.app.background_load.settings_dir", lambda: tmp_path)
    path = tmp_path / "打开作品.stitchpress"
    project = demo_project()
    path.write_bytes(project.encode())
    w = Workbench()
    qtbot.addWidget(w)
    errors = []
    monkeypatch.setattr(w, "error", errors.append)
    start_load(w, path, "load-project")
    qtbot.waitUntil(lambda: getattr(w, "_load_process", None) is None, timeout=60000)
    assert not errors and w.coordinator is not None
    assert w.draft.project_id == project.project_id and w.project_path == path
    w.dirty = False
    w.close()
