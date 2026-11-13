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
