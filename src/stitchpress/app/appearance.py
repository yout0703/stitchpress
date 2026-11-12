"""Shared desktop tokens. Appearance never changes design or production colors."""

import sys
from pathlib import Path
from string import Template

import qtawesome as qta
from PySide6.QtCore import QEvent, QObject, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QApplication, QLineEdit


class InputInsets(QObject):
    """Qt spin editors need explicit text margins in addition to the outer box model."""

    def eventFilter(self, watched, event):
        if (
            event.type() == QEvent.Type.Polish
            and isinstance(watched, QLineEdit)
            and isinstance(watched.parentWidget(), QAbstractSpinBox)
        ):
            watched.setTextMargins(10, 0, 4, 0)
        return False


THEMES = {
    "dark": {
        "bg": "#141a20",
        "panel": "#1c232a",
        "raised": "#242d35",
        "canvas": "#10161c",
        "line": "#35414b",
        "text": "#eef3f6",
        "muted": "#acb8c2",
        "disabled": "#75828e",
        "accent": "#86deca",
        "accent_hover": "#a3eadb",
        "accent_text": "#102d28",
        "selected": "#263e3c",
        "hover": "#2c3741",
    },
    "light": {
        "bg": "#eef2f4",
        "panel": "#ffffff",
        "raised": "#f5f8f9",
        "canvas": "#e2e8eb",
        "line": "#ccd5db",
        "text": "#1b2933",
        "muted": "#536571",
        "disabled": "#7b8993",
        "accent": "#176956",
        "accent_hover": "#105844",
        "accent_text": "#ffffff",
        "selected": "#e0f0eb",
        "hover": "#eaf0f2",
    },
}


def themed_icon(name, theme, *, primary=False):
    tokens = THEMES[theme]
    return qta.icon(
        name,
        color=tokens["accent_text"] if primary else tokens["text"],
        color_disabled=tokens["disabled"],
        color_on=tokens["accent"],
    )


def bind_icon(widget, name):
    widget.setProperty("iconName", name)
    widget.setIconSize(QSize(19, 19))


def apply_appearance(app: QApplication, root, theme):
    if not hasattr(app, "_input_insets"):
        app._input_insets = InputInsets(app)
        app.installEventFilter(app._input_insets)
    for spin in root.findChildren(QAbstractSpinBox):
        spin.lineEdit().setTextMargins(10, 0, 4, 0)
    icons = Path(__file__).parents[1] / "resources" / "icons"
    t = {
        **THEMES[theme],
        **{
            name.replace("-", "_"): (icons / f"{theme}-{name}.png").as_posix().replace('"', '\\"')
            for name in ("chevron-down", "chevron-up", "check")
        },
    }
    app.setStyle("Fusion")
    font = QFont("PingFang SC" if sys.platform == "darwin" else "Segoe UI", 10)
    font.setFamilies([font.family(), "Microsoft YaHei UI", "Noto Sans CJK SC", "sans-serif"])
    app.setFont(font)
    # Use a complete stylesheet rather than combining QPalette overrides with QSS.
    app.setStyleSheet(
        Template("""
        QWidget { color: $text; font-size: 13px; selection-background-color: $selected;
                  selection-color: $text; }
        QMainWindow, QDialog, QStackedWidget { background: $bg; }
        QFrame#documentHeader, QFrame#viewToolbar, QFrame#palettePanel,
        QFrame#selectionDock, QFrame#inspector, QFrame#pageCard { background: $panel; }
        QFrame#navigationRail { background: $panel; border-right: 1px solid $line; }
        QPushButton[role="navigation"] { text-align: left; border: none;
            padding: 9px 12px; min-height: 22px; }
        QPushButton[role="navigation"]:checked { background: $selected; color: $accent; }
        QListWidget { background: $raised; border: 1px solid $line; border-radius: 6px; }
        QFrame#sourceCard, QFrame#overviewCard { background: $panel;
            border: 1px solid $line; border-radius: 14px; }
        QWidget#editorSurface, QWidget#editorSurface QStackedWidget { background: $panel; }
        QLabel[role="emptyState"] { background: $canvas; color: $muted; font-size: 15px;
            border-radius: 10px; }
        QLabel[role="sectionLabel"] { color: $muted; font-size: 12px; font-weight: 600;
            padding-top: 8px; padding-bottom: 5px; }
        QFrame#documentHeader { border-bottom: 1px solid $line; }
        QFrame#viewToolbar { border-bottom: 1px solid $line; }
        QFrame#palettePanel { border-right: 1px solid $line; }
        QFrame#inspector { border-left: 1px solid $line; }
        QFrame#masterPanel, QFrame#sourcePanel { background: $canvas; }
        QFrame#selectionDock { border: 1px solid $line; border-radius: 10px; }
        QFrame#pageCard { border: 1px solid $line; border-radius: 10px; }
        QLabel { background: transparent; border: none; }
        QLabel[role="brand"] { font-size: 18px; font-weight: 600; }
        QLabel[role="brandSubtitle"] { font-size: 10px; color: $muted; }
        QLabel#brandMark { background: #f1f8f5; border-radius: 10px; }
        QFrame#brandDivider { background: $line; border: none; }
        QLabel[role="document"] { font-size: 14px; font-weight: 500; }
        QLabel[role="heading"] { font-size: 19px; font-weight: 600; }
        QLabel[role="pageTitle"] { font-size: 24px; font-weight: 650; }
        QLabel[role="muted"] { color: $muted; }
        QLabel[role="badge"] { color: $accent; background: $selected;
            border-radius: 5px; padding: 5px 9px; font-size: 12px; }
        QPushButton, QToolButton { background: transparent; border: 1px solid $line;
            border-radius: 6px; padding: 7px 11px; min-height: 20px; }
        QPushButton:hover, QToolButton:hover { background: $hover; }
        QPushButton:pressed, QToolButton:pressed { background: $selected; }
        QPushButton:checked, QToolButton:checked { background: $selected;
            color: $accent; border-color: $accent; }
        QPushButton[role="primary"] { background: $accent; color: $accent_text;
            border-color: $accent; font-weight: 600; }
        QPushButton[role="primary"]:hover { background: $accent_hover; }
        QPushButton[role="outline"] { color: $accent; border-color: $accent; }
        QPushButton[role="quiet"], QToolButton[role="quiet"] { border-color: transparent; }
        QPushButton[role="icon"] { padding: 6px; min-width: 23px; }
        QPushButton:disabled, QToolButton:disabled { color: $disabled;
            background: $raised; border-color: $line; }
        QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus,
        QAbstractSpinBox:focus, QTextEdit:focus, QTableWidget:focus { border: 1px solid $accent; }
        QLineEdit, QComboBox, QAbstractSpinBox, QTextEdit { background: $raised;
            color: $text; border: 1px solid $line; border-radius: 7px;
            padding: 8px 10px; min-height: 18px; }
        QLineEdit, QTextEdit { placeholder-text-color: $muted; }
        QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover { border-color: $muted; }
        QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus { border-color: $accent; }
        QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {
            color: $disabled; background: $bg; border-color: $line; }
        QComboBox { padding-right: 30px; }
        QComboBox::drop-down { width: 28px; border: none; }
        QComboBox::down-arrow { image: url("$chevron_down"); width: 13px; height: 13px; }
        QComboBox QAbstractItemView { background: $panel; color: $text;
            selection-background-color: $selected; selection-color: $text;
            border: 1px solid $line; }
        QAbstractSpinBox { padding: 6px 30px 6px 10px; }
        QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
            subcontrol-origin: border; width: 24px; right: 1px;
            border: none; border-left: 1px solid $line; background: transparent; }
        QAbstractSpinBox::up-button { subcontrol-position: top right; top: 1px;
            border-top-right-radius: 6px; }
        QAbstractSpinBox::down-button { subcontrol-position: bottom right; bottom: 1px;
            border-bottom-right-radius: 6px; }
        QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {
            background: $hover; }
        QAbstractSpinBox::up-arrow { image: url("$chevron_up"); width: 11px; height: 11px; }
        QAbstractSpinBox::down-arrow { image: url("$chevron_down"); width: 11px; height: 11px; }
        QLineEdit#qt_spinbox_lineedit, QLineEdit#qt_spinbox_lineedit:focus,
        QComboBox QLineEdit, QComboBox QLineEdit:focus {
            background: transparent; border: none; border-radius: 0;
            padding: 0; margin: 0; min-height: 0; }
        QCheckBox { spacing: 8px; padding: 5px 0; }
        QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
            border: 1px solid $muted; background: $raised; }
        QCheckBox::indicator:checked { background: $accent; border: 1px solid $accent;
            image: url("$check"); }
        QTableWidget { background: $panel; alternate-background-color: $raised;
            border: none; gridline-color: $line; outline: none; }
        QTableWidget::item { padding: 4px; border-bottom: 1px solid $line; }
        QTableWidget::item:selected { background: $selected; color: $text; }
        QHeaderView::section { background: $panel; color: $muted;
            padding: 7px 4px; border: none; font-size: 11px; }
        QTableCornerButton::section { background: $panel; border: none; }
        QGraphicsView { background: $canvas; border: none; }
        QScrollArea { background: transparent; border: none; }
        QScrollArea > QWidget > QWidget { background: $bg; }
        QScrollArea > QWidget > QWidget#editorSurface,
        QWidget#editorSurface QStackedWidget { background: $panel; }
        QScrollBar:vertical { background: transparent; width: 9px; margin: 0; }
        QScrollBar:horizontal { background: transparent; height: 9px; margin: 0; }
        QScrollBar::handle { background: $disabled; border-radius: 4px; min-height: 28px;
            min-width: 28px; }
        QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
        QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
        QLabel#commerceCanvas { background: $canvas; border-radius: 8px; }
        QWidget#commerceInspector, QWidget#fabricPreview { background: $panel; }
        QSplitter::handle { background: $line; width: 1px; }
        QGroupBox { border: 1px solid $line; border-radius: 6px;
            margin-top: 18px; padding: 14px 10px 10px; font-weight: 600; }
        QGroupBox::title { subcontrol-origin: margin; left: 12px; }
        QMenuBar::item:selected { background: $selected; }
        QMenuBar::item { padding: 4px 10px; }
        QMenu, QMenuBar { background: $panel; color: $text;
            border: 1px solid $line; padding: 5px; }
        QMenu::item { padding: 9px 20px; border-radius: 4px; }
        QMenu::item:selected { background: $selected; color: $accent; }
        QToolButton::menu-indicator { image: url("$chevron_down");
            subcontrol-origin: padding; subcontrol-position: right center;
            width: 12px; height: 12px; right: 6px; }
        QToolButton[role="quiet"] { padding-right: 24px; }
        QListWidget#symbolGallery { background: $panel; padding: 8px; }
        QListWidget#symbolGallery::item { border: 1px solid transparent;
            border-radius: 6px; padding: 8px; }
        QListWidget#symbolGallery::item:selected { background: $selected; border-color: $accent; }
        QListWidget#symbolGallery::item:hover { background: $hover; }
        QTabWidget::pane { border: none; background: $panel; top: 8px; }
        QTabBar::tab { background: transparent; color: $muted; padding: 10px 16px;
            border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { color: $accent; border-bottom: 2px solid $accent;
            font-weight: 600; }
        QTabBar::tab:hover { background: $hover; }
        QStatusBar { background: $bg; color: $muted; font-size: 11px; }
        QStatusBar::item { border: none; }
        QToolTip { background: $raised; color: $text; border: 1px solid $line;
            padding: 6px; }
        """).substitute(t)
    )
    for button in root.findChildren(QAbstractButton):
        if name := button.property("iconName"):
            button.setIcon(themed_icon(name, theme, primary=button.property("role") == "primary"))
