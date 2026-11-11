from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QPushButton, QTableWidget

from stitchpress.app.desktop import Workbench
from stitchpress.app.store import Project
from stitchpress.core.errors import StitchPressError
from stitchpress.core.masks import annotation_masks, polygon_mask


def test_concave_polygon_cell_centers_winding_and_portable_identity():
    points = [[0, 0], [4, 0], [4, 1], [1, 1], [1, 4], [0, 4]]
    expected = np.array([[1, 1, 1, 1], [1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]], bool)
    assert np.array_equal(polygon_mask(points, 4, 4), expected)
    assert np.array_equal(polygon_mask(points[::-1], 4, 4), expected)
    triangle = [[0, 0], [4, 0], [0, 4]]
    # Exact center convention: left intersection included; right intersection excluded.
    assert polygon_mask(triangle, 4, 4).tolist() == [
        [True, True, True, False],
        [True, True, False, False],
        [True, False, False, False],
        [False, False, False, False],
    ]
    from stitchpress.app.fixtures import demo_project

    project = demo_project()
    project.entities["annotations"] = dict(
        schema_version=1,
        grid=[4, 4],
        source_sha=None,
        regions=[dict(shape="polygon", kind="protected", points=points)],
    )
    restored = Project.decode(project.encode())
    assert np.array_equal(
        annotation_masks(restored.entities["annotations"], 4, 4)["protected"], expected
    )
    with pytest.raises(StitchPressError, match="来源"):
        annotation_masks(restored.entities["annotations"], 4, 4, "a" * 64)


def test_polygon_rejects_invalid_vertices_and_subject_blank_overlap():
    for points in ([[[0, 0], [1, 1], [2, 2]]],):
        with pytest.raises(StitchPressError):
            polygon_mask(points, 4, 4)
    with pytest.raises(StitchPressError, match="面积"):
        polygon_mask([[0, 0], [1, 1], [2, 2]], 4, 4)
    with pytest.raises(StitchPressError, match="整数"):
        polygon_mask([[0, 0], [4.0, 0], [0, 4]], 4, 4)
    record = dict(
        schema_version=1,
        grid=[4, 4],
        source_sha=None,
        regions=[
            dict(kind="subject", shape="polygon", points=[[0, 0], [4, 0], [0, 4]]),
            dict(kind="blank", region=[0, 0, 1, 1]),
        ],
    )
    with pytest.raises(StitchPressError, match="冲突"):
        annotation_masks(record, 4, 4)


def test_polygon_canvas_cancel_commit_and_individual_removal_cancel(qtbot, monkeypatch):
    window = Workbench()
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "confirm_discard", lambda: True)
    before = deepcopy(window.draft.entities)
    window.start_polygon_mask("protected")
    window.canvas._polygon_points = [(10, 10), (30, 10), (10, 30)]
    qtbot.keyClick(window.canvas, Qt.Key.Key_Escape)
    assert window.canvas._polygon_kind is None and window.draft.entities == before
    dx = (window.canvas.sceneRect().width() - window.design.width * 18) // 2
    dy = (window.canvas.sceneRect().height() - window.design.height * 18) // 2
    window.start_polygon_mask("protected")
    window.canvas._polygon_points = [(dx, dy), (dx + 54, dy), (dx, dy + 54)]
    window.canvas.finish_polygon()
    assert window.draft.entities["annotations"]["regions"][0]["points"] == [[0, 0], [3, 0], [0, 3]]
    saved = deepcopy(window.draft.entities)

    def remove_then_cancel(dialog):
        table = dialog.findChild(QTableWidget)
        table.selectRow(0)
        next(b for b in dialog.findChildren(QPushButton) if b.text() == "移除所选标注").click()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", remove_then_cancel)
    window.manage_annotations()
    assert window.draft.entities == saved
    assert Project.decode(window.draft.encode()).entities["annotations"] == saved["annotations"]
    window.close()
