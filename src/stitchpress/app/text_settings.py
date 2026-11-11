"""Explicit header/footer styles shared with final rendering."""

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
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.layout import Fabric
from stitchpress.core.text import FIELDS, SLOTS, TextLayer, expand, values_for


class TextSettingsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle("顶底栏文字与占位符")
        self.resize(760, 550)
        self.owner = owner
        layout = QVBoxLayout(self)
        note = QLabel("分别设置各区域的文字。模板留空沿用基础文字；越界或缺字会在输出前提示。")
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = QTabWidget()
        tabs.setObjectName("textSlots")
        layout.addWidget(tabs)
        self.controls = {}
        self.assets = {}
        self.extra_controls = {}
        for key, title in SLOTS.items():
            page = QWidget()
            columns = QHBoxLayout(page)
            columns.setSpacing(28)
            typography, position = QWidget(), QWidget()
            columns.addWidget(typography, 1)
            columns.addWidget(position, 1)
            form = QFormLayout(typography)
            position_form = QFormLayout(position)
            saved = TextLayer(**owner.draft.entities.get("text_layers", {}).get(key, {}))
            enabled = QCheckBox("显示此栏（关闭会清除该栏基础文字）")
            enabled.setChecked(saved.enabled)
            font = QLabel(saved.font_asset or "沿用作品公共字体")
            font.setWordWrap(True)
            choose = QPushButton("选择此栏字体…")
            choose.clicked.connect(lambda _, slot=key: self.choose_slot_font(slot))
            reset = QPushButton("沿用公共字体")
            reset.clicked.connect(lambda _, slot=key: self.reset_slot_font(slot))
            self.extra_controls[key] = dict(enabled=enabled, asset=saved.font_asset, label=font)
            form.addRow(enabled)
            form.addRow(font)
            font_actions = QHBoxLayout()
            font_actions.setSpacing(10)
            font_actions.addWidget(choose)
            font_actions.addWidget(reset)
            font_actions.addStretch()
            form.addRow(font_actions)
            fields = {
                k: QLineEdit(getattr(saved, k)) for k in ("template", "points", "dx_mm", "dy_mm")
            }
            fields["template"].setPlaceholderText(
                "{code} · {width} x {height} · {stitches} stitches"
            )
            for field_key in ("points", "dx_mm", "dy_mm"):
                fields[field_key].setMaximumWidth(150)
            align = QComboBox()
            for label, value in [("左对齐", "left"), ("居中", "center"), ("右对齐", "right")]:
                align.addItem(label, value)
            align.setCurrentIndex(align.findData(saved.align))
            colors = []
            color_row = QHBoxLayout()
            color_row.setSpacing(12)
            for label, value in zip("CMYK", saved.cmyk, strict=True):
                box = QSpinBox()
                box.setRange(0, 255)
                box.setValue(value)
                channel = QVBoxLayout()
                channel.setSpacing(4)
                channel.addWidget(QLabel(label))
                box.setMaximumWidth(85)
                channel.addWidget(box)
                color_row.addLayout(channel)
                colors.append(box)
            for label, widget in [
                ("文字模板", fields["template"]),
                ("字号 pt（6–36）", fields["points"]),
                ("对齐", align),
                ("左右偏移 mm", fields["dx_mm"]),
                ("上下偏移 mm", fields["dy_mm"]),
            ]:
                (position_form if widget in (fields["dx_mm"], fields["dy_mm"]) else form).addRow(
                    label, widget
                )
            color_row.addStretch()
            position_form.addRow(QLabel("印刷通道 · 0–255"))
            position_form.addRow(color_row)
            preview = QLabel()
            preview.setWordWrap(True)
            form.addRow("字段替换预览", preview)
            self.controls[key] = (fields, align, colors, preview)
            fields["template"].textChanged.connect(self.preview_fields)
            tabs.addTab(page, title)
        label = QLabel("支持占位符：" + "  ".join("{" + key + "}" for key in sorted(FIELDS)))
        label.setWordWrap(True)
        label.setProperty("role", "muted")
        layout.addWidget(label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.preview_fields()

    def values(self):
        result = {}
        for key, (fields, align, colors, _) in self.controls.items():
            extra = self.extra_controls[key]
            if (
                fields["template"].text().strip()
                or not extra["enabled"].isChecked()
                or extra["asset"]
            ):
                result[key] = asdict(
                    TextLayer(
                        **{k: v.text() for k, v in fields.items()},
                        enabled=extra["enabled"].isChecked(),
                        font_asset=extra["asset"],
                        align=align.currentData(),
                        cmyk=tuple(w.value() for w in colors),
                    )
                )
        return result

    def choose_slot_font(self, key):
        from stitchpress.core.identity import sha256
        from stitchpress.core.resources import load_font

        path, _ = QFileDialog.getOpenFileName(self, "选择有权使用的字体", "", "Fonts (*.ttf *.otf)")
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            load_font(24, raw)
            asset = f"resources/fonts/{sha256(raw)}.font"
            self.assets[asset] = raw
            self.extra_controls[key]["asset"] = asset
            self.extra_controls[key]["label"].setText(Path(path).name)
        except Exception as exc:
            QMessageBox.warning(self, "字体未采用", str(exc))

    def reset_slot_font(self, key):
        self.extra_controls[key]["asset"] = None
        self.extra_controls[key]["label"].setText("沿用作品公共字体")

    def preview_fields(self):
        metadata = self.owner.draft.entities["metadata"]
        values = values_for(
            self.owner.design,
            Fabric(**self.owner.draft.entities["fabric"]),
            code=metadata["code"],
            title=metadata["title"],
            page=1,
            pages="预览",
            scale="1",
        )
        for fields, _, _, preview in self.controls.values():
            try:
                preview.setText(expand(fields["template"].text(), values) or "沿用基础文字")
            except Exception as exc:
                preview.setText(str(exc))

    def validate_accept(self):
        try:
            self.values()
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "文字未保存", str(exc))


class TextFeatures:
    def edit_text_layers(self):
        try:
            dialog = TextSettingsDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            value = dialog.values()
            old = self.draft.entities.get("text_layers", {})
            if value == old:
                return
            self.draft.entities[f"text_layers:{uuid4().hex}"] = copy.deepcopy(old)
            self.draft.assets.update(dialog.assets)
            self.draft.entities["text_layers"] = value
            self.dirty = True
            self.update_document_state()
        except Exception as exc:
            self.error(exc)
