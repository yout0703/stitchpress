"""Local requirements editor and explicit input routing."""

import copy
from uuid import uuid4

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from stitchpress.core.brief import INPUT_KINDS, ROUTES, DesignBrief
from stitchpress.core.identity import sha256


class BriefDialog(QDialog):
    def __init__(self, owner, initial=None):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("设计要求与输入路径")
        self.resize(680, 820)
        layout = QVBoxLayout(self)
        note = QLabel(
            "这些是可修改的方案规格。模型建议只填为草稿；确认后也仍需审查转换母版。更换图片/目录后须重新核对。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        layout.addLayout(form)
        current = (
            initial
            or owner.draft.entities.get("brief")
            or {
                "width": owner.grid_w.value(),
                "height": owner.grid_h.value(),
                "max_colors": owner.color_limit.value(),
            }
        )
        self.original = DesignBrief(**current)
        self.kind, self.route, self.background = QComboBox(), QComboBox(), QComboBox()
        for k, v in INPUT_KINDS.items():
            self.kind.addItem(v, k)
        for k, v in ROUTES.items():
            self.route.addItem(v, k)
        for k, v in [
            ("unconfirmed", "尚未确认"),
            ("full", "满绣"),
            ("selective", "非满绣（负空间另行标注）"),
        ]:
            self.background.addItem(v, k)
        self.fields = {
            k: QLineEdit(getattr(self.original, k))
            for k in ("topic", "composition", "must_keep", "simplification")
        }
        self.objects = QTextEdit("\n".join(self.original.key_objects))
        self.objects.setMaximumHeight(100)
        self.width, self.height, self.colors = QSpinBox(), QSpinBox(), QSpinBox()
        for widget, maximum, value in (
            (self.width, 1000, self.original.width),
            (self.height, 1000, self.original.height),
            (self.colors, 512, self.original.max_colors),
        ):
            widget.setRange(1, maximum)
            widget.setValue(value)
        self.confirmed = QCheckBox("我已核对输入、规格与采用路径")
        self.confirmed.setChecked(
            self.original.confirmed and self.original.source_sha == self.current_source()
        )
        self.kind.setCurrentIndex(self.kind.findData(self.original.input_kind))
        self.route.setCurrentIndex(self.route.findData(self.original.route))
        self.background.setCurrentIndex(self.background.findData(self.original.background))
        for label, widget in [
            ("输入类型", self.kind),
            ("下一步路径", self.route),
            ("主题", self.fields["topic"]),
            ("关键对象（每行一项）", self.objects),
            ("构图与相对位置", self.fields["composition"]),
            ("必须保留", self.fields["must_keep"]),
            ("允许简化", self.fields["simplification"]),
            ("背景", self.background),
            ("针格宽", self.width),
            ("针格高", self.height),
            ("最多颜色", self.colors),
            ("人工确认", self.confirmed),
        ]:
            form.addRow(label, widget)
        catalog = owner.draft.entities.get("catalog")
        self.catalog_revision = sha256(owner.draft.assets[catalog["asset"]]) if catalog else None
        self.confirmed.setChecked(
            self.original.confirmed
            and self.original.source_sha == self.current_source()
            and self.original.catalog_revision == self.catalog_revision
        )
        label = QLabel(
            f"当前输入：{(self.current_source() or '无图片')[:16]}\n"
            f"线色目录：{(self.catalog_revision or '尚未导入')[:16]} · "
            "仅支持全针；实体尺寸沿用当前作品布料配置。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def current_source(self):
        return sha256(self.owner.source_image) if self.owner.source_image else None

    def value(self):
        return DesignBrief(
            input_kind=self.kind.currentData(),
            route=self.route.currentData(),
            **{k: w.text().strip() for k, w in self.fields.items()},
            key_objects=tuple(
                s.strip() for s in self.objects.toPlainText().splitlines() if s.strip()
            ),
            background=self.background.currentData(),
            width=self.width.value(),
            height=self.height.value(),
            max_colors=self.colors.value(),
            source_sha=self.current_source(),
            catalog_revision=self.catalog_revision,
            confirmed=self.confirmed.isChecked(),
        )

    def accept_valid(self):
        try:
            value = self.value()
            if value.max_colors > self.owner.color_limit.maximum():
                raise ValueError("色数上限超过当前符号库容量")
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "设计要求未保存", str(exc))


class BriefFeatures:
    def edit_brief(self, initial=None):
        try:
            dialog = BriefDialog(self, initial if isinstance(initial, dict) else None)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            brief = dialog.value()
            if self.draft.entities.get("brief") != brief.to_dict():
                self.invalidate_review("设计要求改变")
            if old := self.draft.entities.get("brief"):
                self.draft.entities[f"brief:{uuid4().hex}"] = copy.deepcopy(old)
            self.draft.entities["brief"] = brief.to_dict()
            self.grid_w.setValue(brief.width)
            self.grid_h.setValue(brief.height)
            self.color_limit.setValue(brief.max_colors)
            self.capture_conversion_options()
            if brief.confirmed and brief.route == "direct_blueprint":
                self.draft.entities["source"]["role"] = "human-adopted-clean-blueprint"
                self.draft.entities["source"]["brief_revision"] = brief.revision
            self.dirty = True
            self.restore_ai_state()
            self.update_document_state()
        except Exception as exc:
            self.error(exc)

    def capture_creative_prompt(self):
        if self._refreshing:
            return
        record = {
            "text": self.prompt.toPlainText(),
            "include_image": self.ai_include_image.isChecked(),
        }
        if record != self.draft.entities.get("creative_prompt"):
            self.draft.entities["creative_prompt"] = record
            self.dirty = True
            self.update_document_state()

    def restore_ai_state(self):
        record = self.draft.entities.get("creative_prompt", {})
        self.prompt.blockSignals(True)
        self.ai_include_image.blockSignals(True)
        self.prompt.setPlainText(record.get("text", ""))
        self.ai_include_image.setChecked(record.get("include_image", True))
        self.prompt.blockSignals(False)
        self.ai_include_image.blockSignals(False)
        brief = self.draft.entities.get("brief")
        source_sha = sha256(self.source_image) if self.source_image else None
        catalog = self.draft.entities.get("catalog")
        catalog_sha = sha256(self.draft.assets[catalog["asset"]]) if catalog else None
        self.brief_summary.setText(
            "尚未保存结构化设计要求；可先填写本地方案，再决定是否调用AI。"
            if not brief
            else f"{INPUT_KINDS[brief['input_kind']]} · {ROUTES[brief['route']]} · "
            f"{brief['width']}×{brief['height']}格 · 最多{brief['max_colors']}色 · "
            + (
                "输入/目录已改变，待复核"
                if brief["source_sha"] != source_sha or brief["catalog_revision"] != catalog_sha
                else "人工已确认"
                if brief["confirmed"]
                else "草稿待确认"
            )
        )

    def analysis_to_brief(self):
        records = [(k, v) for k, v in self.draft.entities.items() if k.startswith("ai:")]
        if not records:
            return self.error("当前作品还没有分析结果")
        result = records[-1][1]["result"]
        initial = {
            **self.draft.entities.get("brief", {}),
            "topic": result["summary"],
            "key_objects": result["key_objects"],
            "route": result["route"],
            "confirmed": False,
        }
        self.edit_brief(initial)
