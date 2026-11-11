import io
from dataclasses import asdict

import pytest
from PIL import Image

from stitchpress.app.context import review_context
from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project
from stitchpress.app.workflow import begin_session, session_event, workflow_status
from stitchpress.core.errors import StitchPressError
from stitchpress.core.models import Design, ReviewRecord
from stitchpress.core.quality import inspect_design


def reviewed(project):
    design = Design.from_dict(project.entities["design"])
    return ReviewRecord(
        design.content_sha,
        "human reviewer",
        True,
        ((0, 0, 1, 1), (1, 1, 2, 2)),
        True,
        True,
        "review test",
        **review_context(project),
    )


def test_frozen_source_context_change_requires_review_even_with_identical_grid(qtbot, monkeypatch):
    window = Workbench()
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    window.draft.entities["freeze"] = asdict(reviewed(window.draft))
    assert window.review_current()
    source = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(source, "PNG")
    old = window.design.content_sha
    window.import_source(source.getvalue(), "new-source.png")
    assert window.design.content_sha == old
    assert not window.review_current() and "freeze" not in window.draft.entities
    assert any(k.startswith("freeze:") for k in window.draft.entities)
    review = reviewed(window.draft)
    context = review_context(window.draft)
    context["brief_sha"] = "a" * 64
    with pytest.raises(StitchPressError, match="已改变"):
        review.validate_context(context)
    window.close()


def test_workflow_pause_restore_foreign_fork_and_recalculated_stages():
    project = demo_project()
    session = begin_session(project, "mac-a", "existing")
    project.entities["workflow"] = session
    status = workflow_status(project, "mac-a")
    assert status["next"] == "quality"
    paused = session_event(session, "mac-a", "pause")
    with pytest.raises(StitchPressError, match="暂停"):
        session_event(paused, "mac-a", "continue")
    with pytest.raises(StitchPressError, match="其他设备"):
        session_event(paused, "windows-b", "resume")
    project.entities["workflow"] = paused
    assert workflow_status(project, "windows-b")["foreign"]
    design = Design.from_dict(project.entities["design"])
    report = inspect_design(design)
    project.entities["quality:" + report["report_sha"]] = report
    project.entities["workflow"] = session_event(paused, "mac-a", "resume")
    assert workflow_status(project, "mac-a")["next"] == "review"
    project.entities["freeze"] = asdict(reviewed(project))
    assert workflow_status(project, "mac-a")["next"] == "export"
    project.entities["export:demo"] = {
        "status": "completed",
        "snapshot": {
            "design_sha": design.content_sha,
            "demo": True,
            "targets": ["production", "reference", "pdf", "counts", "showcase", "notes"],
        },
    }
    assert not workflow_status(project, "mac-a")["completed"]
    fork = begin_session(project, "windows-b", "existing", parent=session["run_id"])
    assert fork["parent_run"] == session["run_id"] and fork["events"] == []


def test_full_package_stage_requires_current_export_settings():
    from stitchpress.app.context import export_context
    from stitchpress.app.export import export_snapshot

    project = demo_project()
    project.entities["workflow"] = begin_session(project, "mac-a", "existing")
    project.entities["export:formal"] = dict(
        status="completed", snapshot=export_snapshot(export_context(project))
    )
    status = workflow_status(project, "mac-a")
    assert status["stages"][-1]["state"] == "completed"
    project.entities["metadata"]["code"] = "changed-order"
    status = workflow_status(project, "mac-a")
    assert status["stages"][-1]["state"] == "waiting"


def test_formal_exports_and_commerce_refuse_source_unbound_review(tmp_path):
    from stitchpress.app.commerce import export_commerce
    from stitchpress.app.context import export_context
    from stitchpress.app.export import export_package

    project = demo_project()
    project.entities["freeze"] = asdict(reviewed(project))
    data = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(data, "PNG")
    project.assets["input.png"] = data.getvalue()
    project.entities["source"] = {"asset": "input.png"}
    for execute in (
        lambda: export_package(
            **export_context(project), destination=tmp_path / "formal", work=tmp_path / "work"
        ),
        lambda: export_commerce(project, tmp_path / "commerce", tmp_path / "commerce-work"),
    ):
        with pytest.raises(StitchPressError, match="已改变"):
            execute()
    assert not (tmp_path / "formal").exists() and not (tmp_path / "commerce").exists()
