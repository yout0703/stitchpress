"""Independent scene settings and explicit 7+5 target selection."""

import copy
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.commerce import (
    CONTENTS,
    FRAMES,
    SCENES,
    TARGETS,
    CommerceSettings,
    SceneSettings,
)
from stitchpress.core.conversion import normalize_image
from stitchpress.core.identity import sha256
from stitchpress.platform.settings import settings_dir


class CommerceDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.saved = CommerceSettings(**owner.draft.entities.get("commerce", {}))
        self.assets = {}
        self.setWindowTitle("商品模板 · 三场景与7+5")
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        note = QLabel("三张场景分别配置；可从图库挑选，也可导入自己的背景。")
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = self.tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        self.scenes = []
        for i, saved in enumerate(self.saved.scenes):
            spec = SceneSettings(**saved)
            page = QWidget()
            columns = QHBoxLayout(page)
            columns.setSpacing(28)
            left, right = QWidget(), QWidget()
            columns.addWidget(left, 1)
            columns.addWidget(right, 1)
            form = QFormLayout(left)
            right_form = QFormLayout(right)
            scene, frame = QComboBox(), QComboBox()
            scene.addItems(SCENES)
            frame.addItems(FRAMES)
            scene.setCurrentIndex(spec.scene)
            frame.setCurrentIndex(spec.frame)
            fields = {key: QLineEdit(getattr(spec, key)) for key in ("scale", "dx", "dy")}
            border, mat = QSpinBox(), QSpinBox()
            for spin, value in ((border, spec.border), (mat, spec.mat)):
                spin.setRange(0, 100)
                spin.setValue(value)
            matcolor = QLineEdit(",".join(str(n) for n in spec.mat_color))
            background = QLabel("已绑定作品背景" if spec.background_asset else "使用内置示意场景")
            controls = {
                "scene": scene,
                "frame": frame,
                "fields": fields,
                "border": border,
                "mat": mat,
                "mat_color": matcolor,
                "asset": spec.background_asset,
                "background": background,
            }
            self.scenes.append(controls)
            for name, widget in [
                ("场景", scene),
                ("相框", frame),
                ("画作缩放", fields["scale"]),
                ("左右偏移（-0.5–0.5）", fields["dx"]),
                ("上下偏移（-0.5–0.5）", fields["dy"]),
                ("框边设计像素", border),
                ("卡纸设计像素", mat),
                ("卡纸RGB（逗号分隔）", matcolor),
                ("背景", background),
            ]:
                (right_form if widget in (border, mat, matcolor, background) else form).addRow(
                    name, widget
                )
            import_button = QPushButton("导入自选背景…")
            import_button.clicked.connect(lambda _, index=i: self.import_background(index))
            reset = QPushButton("恢复内置背景")
            reset.clicked.connect(lambda _, index=i: self.reset_background(index))
            actions = QHBoxLayout()
            actions.addWidget(import_button)
            actions.addWidget(reset)
            actions.addStretch()
            right_form.addRow(actions)
            tabs.addTab(page, f"场景 {i + 1}")
        style = QWidget()
        form = QFormLayout(style)
        self.text_fields = {
            k: QLineEdit(getattr(self.saved, k))
            for k in ("title", "footer", "closeup_x", "closeup_y", "closeup_zoom")
        }
        self.care = QTextEdit(self.saved.care)
        self.care.setMaximumHeight(100)
        self.art_source = QComboBox()
        for text, value in (
            ("对应原图", "original"),
            ("钻石点图", "dot"),
            ("X针格模拟", "simulation"),
        ):
            self.art_source.addItem(text, value)
        self.art_source.setCurrentIndex(self.art_source.findData(self.saved.art_source))
        form.addRow("场景画作来源", self.art_source)
        sizing = QWidget()
        sizing_form = QFormLayout(sizing)
        self.detail_fields = {}
        for key, title, minimum, maximum in (
            ("detail_width", "详情宽 px", 600, 4800),
            ("detail_height", "详情高 px", 600, 4800),
            ("detail_dpi", "详情 DPI", 72, 600),
        ):
            field = QSpinBox()
            field.setRange(minimum, maximum)
            field.setValue(getattr(self.saved, key))
            self.detail_fields[key] = field
            sizing_form.addRow(title, field)
        self.size, self.dpi = QSpinBox(), QSpinBox()
        self.size.setRange(20, 80)
        self.size.setValue(self.saved.font_pixels)
        self.dpi.setRange(72, 600)
        self.dpi.setValue(self.saved.square_dpi)
        self.colors = {
            k: QLineEdit(",".join(str(n) for n in getattr(self.saved, k)))
            for k in ("background", "ink", "accent")
        }
        for name, key in [
            ("标题模板", "title"),
            ("页脚模板", "footer"),
            ("局部中心X（0–1）", "closeup_x"),
            ("局部中心Y（0–1）", "closeup_y"),
            ("局部放大倍率", "closeup_zoom"),
        ]:
            form.addRow(name, self.text_fields[key])
        form.addRow("标题设计像素", self.size)
        sizing_form.addRow("方图 DPI · 800×800 px", self.dpi)
        for name, key in [("底色RGB", "background"), ("文字RGB", "ink"), ("点缀RGB", "accent")]:
            form.addRow(name, self.colors[key])
        form.addRow("注意事项（自行核对材料依据）", self.care)
        help_label = QLabel(
            "标题/页脚支持 {code}、{title}、{width}、{height}、{colors}、{stitches}、"
            "{width_cm}、{height_cm} 等；输出字体沿用作品设置。来源对比须有原图。"
        )
        help_label.setWordWrap(True)
        form.addRow(help_label)
        tabs.addTab(style, "内容与样式")
        tabs.addTab(sizing, "输出尺寸")
        square = QWidget()
        form = QFormLayout(square)
        self.squares = []
        for i, value in enumerate(self.saved.squares):
            combo = QComboBox()
            for key, label in CONTENTS.items():
                combo.addItem(label, key)
            combo.setCurrentIndex(combo.findData(value))
            self.squares.append(combo)
            form.addRow(f"方图 {i + 1}", combo)
        tabs.addTab(square, "五张方图内容")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def import_background(self, index):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入有权使用的背景", "", "Images (*.png *.jpg *.jpeg *.webp *.tif *.tiff)"
        )
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            normalized, _ = normalize_image(raw)
            normalized.close()
            name = f"resources/commerce-backgrounds/{sha256(raw)}{Path(path).suffix.lower()}"
            self.assets[name] = raw
            self.scenes[index]["asset"] = name
            self.scenes[index]["background"].setText(Path(path).name)
        except Exception as exc:
            QMessageBox.warning(self, "背景未导入", str(exc))

    def reset_background(self, index):
        self.scenes[index]["asset"] = None
        self.scenes[index]["background"].setText("使用内置示意场景")

    @staticmethod
    def rgb(field):
        return tuple(int(n.strip()) for n in field.text().split(","))

    def values(self):
        scenes = []
        for controls in self.scenes:
            scenes.append(
                asdict(
                    SceneSettings(
                        scene=controls["scene"].currentIndex(),
                        frame=controls["frame"].currentIndex(),
                        **{k: v.text().strip() for k, v in controls["fields"].items()},
                        border=controls["border"].value(),
                        mat=controls["mat"].value(),
                        background_asset=controls["asset"],
                        mat_color=self.rgb(controls["mat_color"]),
                    )
                )
            )
        return CommerceSettings(
            scenes=tuple(scenes),
            squares=tuple(w.currentData() for w in self.squares),
            **{k: v.text() for k, v in self.text_fields.items()},
            care=self.care.toPlainText(),
            font_pixels=self.size.value(),
            square_dpi=self.dpi.value(),
            art_source=self.art_source.currentData(),
            **{k: field.value() for k, field in self.detail_fields.items()},
            **{k: self.rgb(v) for k, v in self.colors.items()},
        )

    def validate_accept(self):
        try:
            self.values()
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "模板未保存", str(exc))


class CommerceFeatures:
    def edit_commerce(self):
        try:
            dialog = CommerceDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            value = dialog.values().to_dict()
            if old := self.draft.entities.get("commerce"):
                self.draft.entities[f"commerce:{uuid4().hex}"] = copy.deepcopy(old)
            self.draft.entities["commerce"] = value
            self.draft.assets.update(dialog.assets)
            self.dirty = True
            self.update_document_state()
        except Exception as exc:
            self.error(exc)

    def preview_commerce(self):
        names = [
            f"{target} · "
            + (
                f"方图 {target[-1]}"
                if target.startswith("square")
                else CONTENTS[target.removeprefix("detail-")]
            )
            for target in TARGETS
        ]
        selected, accepted = QInputDialog.getItem(
            self, "商品实际预览", "选择一项（使用已保存的作品模板）", names, 0, False
        )
        if accepted:
            try:
                self.start_worker(
                    "commerce-preview",
                    {
                        "target": TARGETS[names.index(selected)],
                        "demo": not bool(self.draft.entities.get("freeze")),
                    },
                )
            except Exception as exc:
                self.error(exc)

    def show_commerce_preview(self, result):
        from PIL import Image

        dialog = QDialog(self)
        dialog.setWindowTitle("商品实际预览 · 示意场景/母版模拟")
        dialog.resize(880, 850)
        layout = QVBoxLayout(dialog)
        label = QLabel()
        label.setWordWrap(True)
        label.setText(
            f"{result['checks']['content']} · "
            f"{result['checks']['size'][0]}×{result['checks']['size'][1]} px · 母版模拟，非实绣照片"
        )
        layout.addWidget(label)
        canvas = type(self.canvas)()
        layout.addWidget(canvas, 1)
        with Image.open(result["preview"]) as image:
            canvas.set_image(image.convert("RGB"))
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, canvas.fit)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def export_commerce_files(self):
        try:
            dialog = QDialog(self)
            dialog.setWindowTitle("导出商品素材 · 独立目标")
            layout = QVBoxLayout(dialog)
            boxes = {}
            saved = self.draft.entities.get("commerce_targets", {}).get("items", TARGETS)
            shortcuts = QHBoxLayout()
            for name, items in [
                ("全套12张", TARGETS),
                ("7张详情", TARGETS[:7]),
                ("5张方图", TARGETS[7:]),
            ]:
                button = QPushButton(name)
                button.clicked.connect(
                    lambda _, chosen=items: [
                        box.setChecked(key in chosen) for key, box in boxes.items()
                    ]
                )
                shortcuts.addWidget(button)
            layout.addLayout(shortcuts)
            for target in TARGETS:
                box = QCheckBox(target)
                box.setChecked(target in saved)
                boxes[target] = box
                layout.addWidget(box)
            note = QLabel(
                "详情默认1200×1600，可自定义；方图800×800；可只选单项。未审核母版仅能在交付页的演示模式导出，图片会标明DRAFT。来源对比需原图。"
            )
            note.setWordWrap(True)
            layout.addWidget(note)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            chosen = [key for key, box in boxes.items() if box.isChecked()]
            if not chosen:
                raise ValueError("至少选择一个目标")
            parent = QFileDialog.getExistingDirectory(self, "商品素材另存位置（自动建立新版本）")
            if not parent:
                return
            self.draft.entities["commerce_targets"] = {"items": chosen}
            self.dirty = True
            self.update_document_state()
            destination = Path(parent) / ("StitchPress-Commerce-" + uuid4().hex[:8])
            cancel = settings_dir() / "work" / ("commerce-cancel-" + uuid4().hex)
            self.start_worker(
                "commerce-export",
                {
                    "targets": chosen,
                    "target": str(destination),
                    "demo": self.demo_mode.isChecked(),
                    "cancel_file": str(cancel),
                },
            )
        except Exception as exc:
            self.error(exc)
