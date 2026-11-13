"""Self-drawn thumbnail selector; previews use the same scene/frame drawing routines."""

from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

from stitchpress.core.commerce import FRAMES, SCENES, CommerceRenderer, SceneSettings


def gallery_images(context, settings, assets, output):
    composer = CommerceRenderer(context, replace(settings, art_source="dot"), assets, demo=True)
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    result = []
    original = composer.subject
    try:
        for kind, labels in (("scene", SCENES), ("frame", FRAMES)):
            for index, name in enumerate(labels):
                strip = Image.new("RGB", (480, 160), "white")
                for slot, size in enumerate(((100, 55), (45, 85), (70, 70))):
                    subject = Image.new("RGB", size, "#e2ca9b")
                    draw = ImageDraw.Draw(subject)
                    draw.ellipse(
                        (size[0] * 0.25, size[1] * 0.2, size[0] * 0.7, size[1] * 0.8),
                        fill="#779587",
                    )
                    composer.subject = subject
                    thumb = Image.new("RGB", (160, 160), "white")
                    composer.scene(
                        thumb,
                        (0, 0, 160, 160),
                        SceneSettings(
                            scene=index if kind == "scene" else 0,
                            frame=index if kind == "frame" else 0,
                            border=20,
                            mat=15,
                        ),
                    )
                    strip.paste(thumb, (slot * 160, 0))
                    thumb.close()
                    subject.close()
                path = folder / f"{kind}-{index}.png"
                strip.save(path)
                strip.close()
                result.append(dict(kind=kind, index=index, name=name, file=str(path)))
    finally:
        original.close()
    return dict(items=result)


def choose_gallery(owner, editor):
    if not owner.require_reference_applied():
        return
    owner.start_worker("commerce-gallery", dict(include_image=False))


def show_gallery(owner, result):
    editor = owner.commerce_panel.editor
    dialog = QDialog(owner)
    dialog.setWindowTitle("场景与相框 · 横 / 竖 / 方示意")
    dialog.resize(900, 700)
    layout = QVBoxLayout(dialog)
    layout.addWidget(
        QLabel("每张缩略图展示横、竖、方三种比例；应用到指定槽位后，可刷新实际作品预览。")
    )
    form = QFormLayout()
    slot = QComboBox()
    slot.addItems(["场景槽位 1", "场景槽位 2", "场景槽位 3"])
    form.addRow("应用到", slot)
    layout.addLayout(form)
    tabs, lists = QTabWidget(), {}
    for kind, title in (("scene", f"{len(SCENES)} 个场景"), ("frame", f"{len(FRAMES)} 种相框")):
        view = QListWidget()
        view.setViewMode(QListWidget.ViewMode.IconMode)
        view.setIconSize(QSize(240, 80))
        view.setGridSize(QSize(260, 115))
        view.setResizeMode(QListWidget.ResizeMode.Adjust)
        for entry in result["items"]:
            if entry["kind"] != kind:
                continue
            with Image.open(entry["file"]) as image:
                item = QListWidgetItem(
                    QIcon(QPixmap.fromImage(ImageQt(image.convert("RGB")))), entry["name"]
                )
            item.setData(Qt.ItemDataRole.UserRole, entry["index"])
            view.addItem(item)
        lists[kind] = view
        tabs.addTab(view, title)

    def select_slot():
        controls = editor.scenes[slot.currentIndex()]
        for kind, view in lists.items():
            view.setCurrentRow(controls[kind].currentIndex())

    slot.currentIndexChanged.connect(select_slot)
    select_slot()
    layout.addWidget(tabs)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close
    )

    def apply():
        controls = editor.scenes[slot.currentIndex()]
        for kind, view in lists.items():
            if view.currentItem():
                controls[kind].setCurrentIndex(view.currentItem().data(Qt.ItemDataRole.UserRole))
        owner.commerce_panel.apply()

    buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(apply)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.exec()
