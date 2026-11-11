from dataclasses import replace

from PySide6.QtCore import Qt

from stitchpress.app.candidates import CandidateDialog
from stitchpress.app.desktop import Workbench
from stitchpress.app.revisions import compare_candidate
from stitchpress.app.store import Project
from stitchpress.core.models import PatchCell, apply_patch


def make_candidate_window(qtbot, monkeypatch):
    window = Workbench()
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    base = replace(window.design, origin="converted")
    window.adopt_design(base)
    changed = next(
        (x, y) for y, row in enumerate(base.labels) for x, index in enumerate(row) if index == 0
    )
    x, y = changed
    patched = apply_patch(
        base, base_sha=base.content_sha, cells=(PatchCell(x, y, 0, 1),), allowed=frozenset({(x, y)})
    )
    window.adopt_design(patched)
    key = window.record_candidate(
        {
            "design": base.to_dict(),
            "conversion": {"options": window.conversion_options(), "result_sha": base.content_sha},
        }
    )
    return window, key


def test_candidate_comparison_survives_reopen_and_cancel_preserves_active_master(
    qtbot, monkeypatch
):
    window, key = make_candidate_window(qtbot, monkeypatch)
    restored = Project.decode(window.draft.encode())
    report = compare_candidate(restored, key)
    assert len(report["patch_chain"]) == 1
    assert report["migration"]["entries"][0]["status"] == "ready"
    before = window.design.content_sha
    dialog = CandidateDialog(window, report)
    qtbot.addWidget(dialog)
    dialog.check_ready()
    assert dialog.table.item(0, 0).checkState() == Qt.CheckState.Checked
    dialog.reject()
    assert window.design.content_sha == before
    window.close()


def test_conflicting_migration_items_are_not_checkable(qtbot, monkeypatch):
    window, key = make_candidate_window(qtbot, monkeypatch)
    report = compare_candidate(window.draft, key)
    report["migration"]["entries"][0]["status"] = "conflict"
    dialog = CandidateDialog(window, report)
    qtbot.addWidget(dialog)
    dialog.check_ready()
    assert dialog.table.item(0, 0).checkState() != Qt.CheckState.Checked
    dialog.reject()
    window.close()


def test_large_migration_table_preserves_selected_cells_across_pages(qtbot, monkeypatch):
    window, key = make_candidate_window(qtbot, monkeypatch)
    report = compare_candidate(window.draft, key)
    sample = report["migration"]["entries"][0]
    report["migration"]["entries"] = [{**sample, "x": i % 48, "y": i // 48} for i in range(2001)]
    dialog = CandidateDialog(window, report)
    qtbot.addWidget(dialog)
    dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    first = tuple(dialog.table.item(0, 0).data(Qt.ItemDataRole.UserRole))
    dialog.page.setValue(2)
    assert dialog.table.rowCount() == 1
    last = tuple(dialog.table.item(0, 0).data(Qt.ItemDataRole.UserRole))
    dialog.check_ready()
    dialog.page.setValue(1)
    assert dialog.table.item(0, 0).checkState() == Qt.CheckState.Checked
    dialog.migrate()
    assert set(dialog.selected) == {first, last}
    window.close()


def test_candidate_from_other_source_cannot_be_adopted_into_current_branch(qtbot, monkeypatch):
    import pytest
    from PySide6.QtWidgets import QDialog

    from stitchpress.core.errors import StitchPressError

    window, key = make_candidate_window(qtbot, monkeypatch)
    window.draft.entities[key]["conversion"]["source_sha"] = "a" * 64
    report = compare_candidate(window.draft, key)
    before = window.design.content_sha

    def accept(dialog):
        dialog.action = "adopt"
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CandidateDialog, "exec", accept)
    with pytest.raises(StitchPressError, match="其他源图"):
        window.show_candidate_comparison(report)
    assert window.design.content_sha == before
    assert not any(k.startswith("candidate-decision:") for k in window.draft.entities)
    window.close()
