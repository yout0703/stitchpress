import json

import pytest

from stitchpress.app.fixtures import demo_project
from stitchpress.app.jobs import run_batch, verify_delivery
from stitchpress.app.store import Project
from stitchpress.core.errors import StitchPressError
from stitchpress.core.models import Design


def create_batch(tmp_path, count=2):
    items = []
    for i in range(count):
        path = tmp_path / f"source-{i}.stitchpress"
        path.write_bytes(demo_project(6, 8, 2).encode())
        items.append({"project": path.name, "destination": f"delivery-{i}", "demo": True})
    recipe = tmp_path / "batch.json"
    recipe.write_text(json.dumps({"schema_version": 1, "items": items}))
    return recipe


def test_cancelled_queue_resumes_immutable_inputs_not_changed_source_files(tmp_path):
    recipe = create_batch(tmp_path, 1)
    work = tmp_path / "work"
    state = run_batch(recipe, work, cancel=lambda: True)
    original = Project.decode((work / "inputs/000.stitchpress").read_bytes())
    expected_sha = Design.from_dict(original.entities["design"]).content_sha
    assert state["status"] == "cancelled"
    (tmp_path / "source-0.stitchpress").write_bytes(demo_project(12, 16, 3).encode())
    restored = run_batch(recipe, work, cache=tmp_path / "cache", resume=True)
    assert restored["status"] == "completed"
    assert restored["jobs"][0]["receipt"]["snapshot"]["design_sha"] == expected_sha
    with pytest.raises(StitchPressError, match="显式恢复"):
        run_batch(recipe, work)


def test_resume_does_not_regenerate_completed_items_and_catches_modified_delivery(tmp_path):
    recipe = create_batch(tmp_path)
    work = tmp_path / "work"
    stopped = False

    def progress(event):
        nonlocal stopped
        if event["stage"] == "publishing" and event["item"] == 1:
            stopped = True

    first = run_batch(
        recipe, work, cache=tmp_path / "cache", cancel=lambda: stopped, progress=progress
    )
    assert first["status"] == "cancelled"
    assert first["jobs"][0]["status"] == "completed"
    events = []
    second = run_batch(recipe, work, cache=tmp_path / "cache", resume=True, progress=events.append)
    assert second["status"] == "completed"
    assert all(e["item"] == 2 for e in events)
    receipt = second["jobs"][0]["receipt"]
    artifact = receipt["artifacts"][0]
    (tmp_path / "delivery-0" / artifact["name"]).write_bytes(b"modified")
    with pytest.raises(StitchPressError, match="交付已改变"):
        run_batch(recipe, work, resume=True)
    assert json.loads((work / "queue.json").read_bytes())["status"] == "blocked"


def test_batch_never_overwrites_existing_or_duplicate_destination(tmp_path):
    recipe = create_batch(tmp_path)
    value = json.loads(recipe.read_bytes())
    value["items"][1]["destination"] = value["items"][0]["destination"]
    recipe.write_text(json.dumps(value))
    with pytest.raises(StitchPressError, match="重复"):
        run_batch(recipe, tmp_path / "work")


def test_receipt_verifies_exact_file_allowlist_and_keeps_human_status_pending(tmp_path):
    recipe = create_batch(tmp_path, 1)
    result = run_batch(recipe, tmp_path / "work")
    receipt = result["jobs"][0]["receipt"]
    folder = tmp_path / "delivery-0"
    assert verify_delivery(folder, receipt)["visual"] == "pending"
    (folder / "private-engineering.json").write_text("must not be in delivery")
    with pytest.raises(StitchPressError, match="清单不一致"):
        verify_delivery(folder, receipt)
