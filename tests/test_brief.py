from dataclasses import replace

import pytest

from stitchpress.app.brief import BriefDialog
from stitchpress.app.desktop import Workbench
from stitchpress.app.store import Project
from stitchpress.core.brief import DesignBrief
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest


def test_brief_does_not_adopt_model_route_or_new_input_without_confirmation():
    brief = DesignBrief(
        input_kind="flat_image",
        route="direct_blueprint",
        source_sha="a" * 64,
        catalog_revision="b" * 64,
    )
    with pytest.raises(StitchPressError, match="人工确认"):
        brief.validate_context(source_sha="a" * 64, catalog_revision="b" * 64, capacity=66)
    confirmed = replace(brief, confirmed=True)
    with pytest.raises(StitchPressError, match="已改变"):
        confirmed.validate_context(source_sha="c" * 64, catalog_revision="b" * 64, capacity=66)
    with pytest.raises(StitchPressError, match="容量"):
        confirmed.validate_context(source_sha="a" * 64, catalog_revision="b" * 64, capacity=30)
    confirmed.validate_context(source_sha="a" * 64, catalog_revision="b" * 64, capacity=66)
    with pytest.raises(StitchPressError, match="干净平面"):
        replace(confirmed, input_kind="photo")


def test_creative_prompt_portable_and_brief_cancel_preserves_draft(qtbot, monkeypatch):
    window = Workbench()
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    qtbot.addWidget(window)
    window.prompt.setPlainText("保留四只耳朵与两只兔子的不同表情")
    window.ai_include_image.setChecked(False)
    restored = Project.decode(window.draft.encode())
    assert restored.entities["creative_prompt"]["text"].startswith("保留四只耳朵")
    before = digest(window.draft.entities)
    dialog = BriefDialog(window)
    qtbot.addWidget(dialog)
    dialog.fields["topic"].setText("候选草稿")
    dialog.reject()
    assert digest(window.draft.entities) == before
    window.set_project(restored)
    assert window.prompt.toPlainText().startswith("保留四只耳朵")
    assert not window.ai_include_image.isChecked()
    window.close()


def test_task_context_invalidates_settings_without_invalidating_quality(qtbot, monkeypatch):
    from stitchpress.app.task_history import context_fingerprint

    window = Workbench()
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    qtbot.addWidget(window)
    before = context_fingerprint(window, "preview")
    quality = context_fingerprint(window, "quality")
    window.draft.entities["reference"] = {"scale": "2"}
    assert context_fingerprint(window, "preview") != before
    assert context_fingerprint(window, "quality") == quality
    window.close()


def test_remote_history_keeps_unknown_reservations_and_counts_imported_attempts(tmp_path):
    from stitchpress.app.task_history import AIPolicy, TaskHistory
    from stitchpress.core.identity import canonical_bytes

    history = TaskHistory(tmp_path)
    policy = AIPolicy(max_attempts=3, limit="2", reservation="1")

    def begin(name, imported=()):
        folder = tmp_path / name
        folder.mkdir()
        return history.begin(
            folder,
            {
                "task_id": name,
                "operation": "analyze",
                "project_id": "p",
                "owner": "device-a",
                "context_sha": "a" * 64,
                "input_sha": "b" * 64,
            },
            policy=policy,
            imported=imported,
        )

    begin("one")
    assert history.records("p")[0][1]["status"] == "remote_unknown"
    foreign = {
        "task_id": "foreign",
        "owner": "device-b",
        "operation": "analyze",
        "context_sha": "c" * 64,
        "currency": "CNY",
        "reserved": "1",
        "actual": None,
    }
    with pytest.raises(StitchPressError, match="计划总额"):
        begin("two", [foreign])
    # A completed response without a provider bill does not release reserved money.
    (tmp_path / "one" / "result.json").write_bytes(
        canonical_bytes({"status": "completed", "result": {"usage": {"total_tokens": 42}}})
    )
    assert history.records("p")[0][1]["reserved"] == "1"
    assert history.records("p")[0][1]["usage"]["total_tokens"] == 42
    with pytest.raises(StitchPressError, match="次数上限"):
        history.begin(
            tmp_path / "two",
            {"task_id": "two", "operation": "analyze", "project_id": "p"},
            policy=AIPolicy(max_attempts=1),
        )
