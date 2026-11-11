import io
import json
from copy import deepcopy

import pytest
from PIL import Image

from stitchpress.app.blueprints import BlueprintDialog
from stitchpress.app.desktop import Workbench
from stitchpress.app.store import Project
from stitchpress.app.worker import run
from stitchpress.core.blueprints import check_blueprint, prepare_blueprint
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import sha256


def blue(color="red"):
    data = io.BytesIO()
    Image.new("RGB", (48, 64), color).save(data, "PNG")
    return data.getvalue()


def result_for(tmp_path):
    raw = blue()
    normalized, record = prepare_blueprint(
        raw, origin="external-ai", note="Imported, not a verified provider call"
    )
    original = tmp_path / "original.png"
    original.write_bytes(raw)
    target = tmp_path / "normalized.png"
    target.write_bytes(normalized)
    return {
        "record": record,
        "original_path": str(original),
        "normalized_path": str(target),
        "label": "source-test",
    }


def test_blueprint_candidate_originals_adoption_and_cancel_are_portable(
    qtbot, tmp_path, monkeypatch
):
    window = Workbench()
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    before = window.design.content_sha
    result = result_for(tmp_path)
    key = window.attach_blueprint(result, {"base_sha": before, "source_sha": None})
    restored = Project.decode(window.draft.encode())
    assert restored.entities[key]["provider_call_verified"] is False
    dialog = BlueprintDialog(window, key)
    qtbot.addWidget(dialog)
    dialog.reject()
    assert window.source_image is None and window.design.content_sha == before
    window.adopt_blueprint(key)
    assert window.draft.entities["brief"]["confirmed"]
    assert window.draft.entities["brief"]["source_sha"] == sha256(window.source_image)
    assert window.design.content_sha == before
    assert restored.assets[restored.entities[key]["original_asset"]] == blue()
    window.close()


def test_tampered_blueprint_does_not_modify_current_assets_or_entities(
    qtbot, tmp_path, monkeypatch
):
    window = Workbench()
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    result = result_for(tmp_path)
    before = deepcopy(window.draft)
    (tmp_path / "normalized.png").write_bytes(blue("blue"))
    with pytest.raises(StitchPressError, match="已改变"):
        window.attach_blueprint(result, {"base_sha": window.design.content_sha, "source_sha": None})
    assert window.draft.entities == before.entities and window.draft.assets == before.assets
    window.close()


def test_blueprint_import_worker_preserves_raw_and_normalized_files(tmp_path):
    from stitchpress.app.fixtures import demo_project

    project = tmp_path / "input.stitchpress"
    project.write_bytes(demo_project().encode())
    image = tmp_path / "raw.png"
    image.write_bytes(blue())
    job = {
        "operation": "blueprint-import",
        "file": str(image),
        "project": str(project),
        "output": str(tmp_path / "result.json"),
        "origin": "customer-image",
    }
    task = tmp_path / "job.json"
    task.write_text(json.dumps(job))
    assert run(task) == 0
    result = json.loads((tmp_path / "result.json").read_bytes())["result"]
    assert (tmp_path / "blueprint-original.image").read_bytes() == blue()
    assert result["record"]["normalized_size"] == [48, 64]
    normalized = (tmp_path / "blueprint-normalized.png").read_bytes()
    record = {**result["record"], "original_asset": "raw", "normalized_asset": "normalized"}
    check_blueprint(record, {"raw": blue(), "normalized": normalized})
