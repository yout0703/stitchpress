"""Repeated windows inherit the existing theme without global style resets."""

from PySide6.QtWidgets import QAbstractSpinBox, QPushButton, QSpinBox, QVBoxLayout, QWidget

from stitchpress.app.appearance import apply_appearance, bind_icon


def test_new_windows_reuse_global_appearance_and_only_real_theme_changes_repolish(
    qtbot, qapp, monkeypatch
):
    first = QWidget()
    qtbot.addWidget(first)
    apply_appearance(qapp, first, "light")
    calls = []
    original = qapp.setStyleSheet

    def forbidden(*args):
        raise AssertionError("不能因新建窗口反复重装全局样式和字体")

    def replace_sheet(value):
        calls.append(value)
        original(value)

    monkeypatch.setattr(qapp, "setStyle", forbidden)
    monkeypatch.setattr(qapp, "setFont", forbidden)
    monkeypatch.setattr(qapp, "setStyleSheet", replace_sheet)
    second = QWidget()
    qtbot.addWidget(second)
    layout = QVBoxLayout(second)
    button = QPushButton("测试")
    bind_icon(button, "mdi6.palette-outline")
    layout.addWidget(button)
    layout.addWidget(QSpinBox())
    apply_appearance(qapp, second, "light")
    assert not calls and not button.icon().isNull()
    assert second.findChild(QAbstractSpinBox).lineEdit().textMargins().left() == 10
    apply_appearance(qapp, second, "dark")
    assert len(calls) == 1
    apply_appearance(qapp, first, "dark")
    assert len(calls) == 1


def test_window_close_stops_inline_preview_and_queued_refresh(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QProcess

    from stitchpress.app.desktop import Workbench

    monkeypatch.setattr("stitchpress.app.commerce_studio.settings_dir", lambda: tmp_path)
    w = Workbench()
    qtbot.addWidget(w)
    w.show()
    w.show_page(8)
    board = w.commerce_panel.editor.board
    board.refresh()
    process = board.process
    assert process is not None
    w.dirty = False
    w.close()
    assert board.process is None and not board.timer.isActive()
    assert process.state() == QProcess.ProcessState.NotRunning
    qtbot.wait(50)
    board.refresh()
    assert board.process is None
