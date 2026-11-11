"""Explicit header/footer styles shared with final rendering."""

import copy
from dataclasses import asdict
from uuid import uuid4

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
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
        note = QLabel(
            "留空沿用基础版。固定保留区内排字，越界或缺字会阻止输出，不会缩字号或盖住针格。字号为pt，偏移为mm。输出字体在尺寸/查图设置中导入。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        self.controls = {}
        for key, title in SLOTS.items():
            page = QWidget()
            form = QFormLayout(page)
            saved = TextLayer(**owner.draft.entities.get("text_layers", {}).get(key, {}))
            fields = {
                k: QLineEdit(getattr(saved, k)) for k in ("template", "points", "dx_mm", "dy_mm")
            }
            fields["template"].setPlaceholderText(
                "{code} · {width} x {height} · {stitches} stitches"
            )
            align = QComboBox()
            for label, value in [("左对齐", "left"), ("居中", "center"), ("右对齐", "right")]:
                align.addItem(label, value)
            align.setCurrentIndex(align.findData(saved.align))
            colors = []
            color_row = QHBoxLayout()
            for label, value in zip("CMYK", saved.cmyk, strict=True):
                box = QSpinBox()
                box.setRange(0, 255)
                box.setValue(value)
                color_row.addWidget(QLabel(label))
                color_row.addWidget(box)
                colors.append(box)
            for label, widget in [
                ("文字模板", fields["template"]),
                ("字号 pt（6–36）", fields["points"]),
                ("对齐", align),
                ("左右偏移 mm", fields["dx_mm"]),
                ("上下偏移 mm", fields["dy_mm"]),
            ]:
                form.addRow(label, widget)
            form.addRow("印刷通道 0–255", color_row)
            preview = QLabel()
            preview.setWordWrap(True)
            form.addRow("字段替换预览", preview)
            self.controls[key] = (fields, align, colors, preview)
            fields["template"].textChanged.connect(self.preview_fields)
            tabs.addTab(page, title)
        label = QLabel("支持占位符：" + "  ".join("{" + key + "}" for key in sorted(FIELDS)))
        label.setWordWrap(True)
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
            if fields["template"].text().strip():
                result[key] = asdict(
                    TextLayer(
                        **{k: v.text() for k, v in fields.items()},
                        align=align.currentData(),
                        cmyk=tuple(w.value() for w in colors),
                    )
                )
        return result

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
            self.draft.entities["text_layers"] = value
            self.dirty = True
            self.update_document_state()
        except Exception as exc:
            self.error(exc)
