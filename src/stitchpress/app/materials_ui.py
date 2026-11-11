"""No numeric defaults for customer thread-length formulas."""

import copy
from dataclasses import asdict
from uuid import uuid4

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from stitchpress.core.materials import MaterialRule


class MaterialFeatures:
    def material_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("配线规则 · 多CT口径")
        dialog.resize(880, 650)
        layout = QVBoxLayout(dialog)
        note = QLabel(
            "未配置时只输出准确针数。一工作米指按所填股数合成的一米工作线；每米针数已包含该股数口径，不能再乘一次。原始单股米数=包装后工作米×股数。每种线色分别加损耗、向上取整包装。此规则只估算材料，不改母版实际格距。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        rules = copy.deepcopy(self.draft.entities.get("material_rules", {}).get("rules", []))
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["规则/CT", "股数", "全针/工作米", "损耗/包装工作米"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)

        def refresh():
            table.setRowCount(len(rules))
            for i, item in enumerate(rules):
                rule = MaterialRule(**item)
                for j, value in enumerate(
                    (
                        rule.name + " / " + rule.nominal_ct,
                        str(rule.strands),
                        rule.stitches_per_work_metre,
                        rule.loss_percent + "% / " + rule.pack_work_metres,
                    )
                ):
                    table.setItem(i, j, QTableWidgetItem(value))

        def add():
            child = QDialog(dialog)
            child.setWindowTitle("新增已确认材料口径")
            form = QFormLayout(child)
            fields = {
                k: QLineEdit()
                for k in (
                    "name",
                    "nominal_ct",
                    "stitches_per_work_metre",
                    "loss_percent",
                    "pack_work_metres",
                    "source_note",
                )
            }
            for name, key in [
                ("规则名称", "name"),
                ("名义CT", "nominal_ct"),
                ("每工作米可绣全针数", "stitches_per_work_metre"),
                ("损耗百分比（无损耗填0）", "loss_percent"),
                ("每包装工作米数", "pack_work_metres"),
                ("口径/厂家来源", "source_note"),
            ]:
                form.addRow(name, fields[key])
            strands = QSpinBox()
            strands.setRange(1, 12)
            form.addRow("合股数量", strands)
            confirmed = QCheckBox("我已核对每米、股数、损耗与包装口径")
            form.addRow(confirmed)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.rejected.connect(child.reject)

            def accept():
                try:
                    rule = MaterialRule(
                        **{k: v.text().strip() for k, v in fields.items()},
                        strands=strands.value(),
                        confirmed=confirmed.isChecked(),
                    )
                    rules.append(asdict(rule))
                    child.accept()
                    refresh()
                except Exception as exc:
                    QMessageBox.warning(child, "规则未添加", str(exc))

            buttons.accepted.connect(accept)
            form.addRow(buttons)
            child.exec()

        def remove():
            row = table.currentRow()
            if row >= 0:
                rules.pop(row)
                refresh()

        for text, callback in [
            ("新增口径…", add),
            ("移除所选口径（保存后旧规则保留为历史）", remove),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            layout.addWidget(button)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        refresh()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if old := self.draft.entities.get("material_rules"):
                self.draft.entities[f"material_rules:{uuid4().hex}"] = copy.deepcopy(old)
            self.draft.entities["material_rules"] = {"rules": rules}
            self.dirty = True
            self.update_document_state()
