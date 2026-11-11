"""Visible call limits and a task inbox that survives window/app restarts."""

import copy
import json
from dataclasses import asdict
from uuid import uuid4

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from stitchpress.core.errors import StitchPressError
from stitchpress.platform.settings import load_settings, save_settings, settings_dir

from .store import Project
from .task_history import AIPolicy, TaskHistory, context_fingerprint


def instance_id():
    config = load_settings()
    if not config.get("instance_id"):
        config["instance_id"] = uuid4().hex
        save_settings(config)
    return config["instance_id"]


class TaskFeatures:
    def sync_task_records(self):
        for _, record in TaskHistory(settings_dir() / "work").records(self.draft.project_id):
            if record["operation"] == "analyze":
                previous = (
                    self.draft.entities.get("ai_usage", {}).get("tasks", {}).get(record["task_id"])
                )
                if previous != record:
                    self.mirror_ai_task(record)

    def ai_plan(self):
        try:
            current = AIPolicy(**self.draft.entities.get("ai_policy", {}))
            dialog = QDialog(self)
            dialog.setWindowTitle("AI调用计划")
            form = QFormLayout(dialog)
            attempts, currency = QSpinBox(), QComboBox()
            attempts.setRange(1, 100)
            attempts.setValue(current.max_attempts)
            currency.addItems(["CNY", "USD"])
            currency.setCurrentText(current.currency)
            total, reserve = QLineEdit(current.limit or ""), QLineEdit(current.reservation or "")
            total.setPlaceholderText("可留空：此时只限制次数")
            reserve.setPlaceholderText("每次预留的计划金额，须自行确认口径")
            for name, widget in [
                ("作品总调用次数上限", attempts),
                ("金额单位", currency),
                ("计划总额", total),
                ("每次预留", reserve),
            ]:
                form.addRow(name, widget)
            note = QLabel(
                "次数含失败、未决与其他设备的既有任务。金额是本地计划额度，不是供应商硬性账单上限；没有真实价格不能保证最终费用。未知费用继续占预留，不会自动重试。"
            )
            note.setWordWrap(True)
            form.addRow(note)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.rejected.connect(dialog.reject)

            def accept():
                try:
                    value = AIPolicy(
                        attempts.value(),
                        currency.currentText(),
                        total.text().strip() or None,
                        reserve.text().strip() or None,
                    )
                    self.draft.entities["ai_policy"] = asdict(value)
                    self.dirty = True
                    self.update_document_state()
                    dialog.accept()
                except Exception as exc:
                    QMessageBox.warning(dialog, "计划未保存", str(exc))

            buttons.accepted.connect(accept)
            form.addRow(buttons)
            dialog.exec()
        except Exception as exc:
            self.error(exc)

    def mirror_ai_task(self, record):
        portable = {k: v for k, v in record.items() if k not in {"output", "input", "folder"}}
        self.draft.entities.setdefault("ai_usage", {"tasks": {}})["tasks"][record["task_id"]] = (
            portable
        )
        self.dirty = True
        self.update_document_state()

    def task_inbox(self):
        history = TaskHistory(settings_dir() / "work")
        records = history.records(self.draft.project_id)
        local = {r["task_id"] for _, r in records}
        records += [
            (None, r)
            for r in self.draft.entities.get("ai_usage", {}).get("tasks", {}).values()
            if r["task_id"] not in local
        ]
        if not records:
            return self.error("当前作品还没有后台任务记录")
        dialog = QDialog(self)
        dialog.setWindowTitle("任务记录与恢复")
        dialog.resize(880, 720)
        layout = QVBoxLayout(dialog)
        notice = QLabel(
            "中断的远端任务保持结果/费用未知，不自动接管或重新发送。可以恢复原输入草稿后查看候选；不会直接覆盖当前作品。"
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        table = QTableWidget(len(records), 4)
        table.setHorizontalHeaderLabels(["阶段", "状态", "时间", "费用/计划预留"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        for i, (_, record) in enumerate(records):
            cost = (
                f"{record.get('actual') or '未知'} / {record.get('reserved') or '未设'} "
                f"{record.get('currency', '')}"
                if record["operation"] == "analyze"
                else "本地任务"
            )
            for col, text in enumerate(
                (record["operation"], record["status"], record["created_at"][:19], cost)
            ):
                table.setItem(i, col, QTableWidgetItem(text))
        layout.addWidget(table)
        details = QTextEdit()
        details.setReadOnly(True)
        layout.addWidget(details, 1)

        def selected():
            if not table.selectedItems():
                raise StitchPressError("请先选择任务")
            return records[table.selectedItems()[0].row()]

        def show():
            path, record = selected()
            value = copy.deepcopy(record)
            if path and (path.parent / "result.json").exists():
                value["response"] = json.loads((path.parent / "result.json").read_bytes())
            details.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))

        table.itemSelectionChanged.connect(show)

        def restore():
            try:
                path, _ = selected()
                if not path or not (path.parent / "input.stitchpress").exists():
                    raise StitchPressError("此记录没有本机输入快照；跨设备任务不会自动接管")
                from stitchpress.core.identity import sha256

                payload = (path.parent / "input.stitchpress").read_bytes()
                if sha256(payload) != selected()[1]["input_sha"]:
                    raise StitchPressError("任务输入快照已改变，不能恢复")
                project = Project.decode(payload)
                if self.confirm_discard():
                    self.set_project(project)
                    self.dirty = True
                    self.update_document_state()
                    dialog.accept()
            except Exception as exc:
                QMessageBox.warning(dialog, "未恢复输入", str(exc))

        def attach():
            try:
                path, record = selected()
                if not path or record["operation"] not in {"analyze", "convert"}:
                    raise StitchPressError("请选择有本机结果的分析或转换任务")
                if record["context_sha"] != context_fingerprint(
                    self, record["operation"], record.get("include_image", True)
                ):
                    raise StitchPressError(
                        "输入或规格已改变；请先恢复此任务输入草稿，不自动套用旧结果"
                    )
                response = json.loads((path.parent / "result.json").read_bytes())
                if response["status"] != "completed":
                    raise StitchPressError("此任务未取得完整结果，不能采用")
                result = response["result"]
                if record["operation"] == "analyze":
                    self.draft.entities[f"ai:{record['task_id']}"] = result
                    self.ai_result.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    self.draft.entities[f"candidate:{record['task_id']}"] = result
                    from stitchpress.core.models import Design

                    if (
                        QMessageBox.question(
                            dialog, "采用恢复的候选？", "旧母版与补丁保留；仍须重新人工审核。"
                        )
                        == QMessageBox.StandardButton.Yes
                    ):
                        self.adopt_design(Design.from_dict(result["design"]))
                        self.draft.entities["active_conversion"] = result["conversion"]
                self.dirty = True
                self.update_document_state()
                dialog.accept()
            except Exception as exc:
                QMessageBox.warning(dialog, "未采用结果", str(exc))

        def reconcile():
            try:
                path, _ = selected()
                if not path:
                    raise StitchPressError("其他设备任务不能由本机接管")
                amount, accepted = QInputDialog.getText(
                    dialog, "核对实际费用", "已向供应商核对的实际金额（不改次数）"
                )
                if not accepted:
                    return
                note, accepted = QInputDialog.getText(
                    dialog, "核对依据", "填写账单/请求对应关系，不填写密钥"
                )
                if not accepted:
                    return
                record = history.reconcile(path, owner=instance_id(), actual=amount, note=note)
                self.mirror_ai_task(record)
                dialog.accept()
            except Exception as exc:
                QMessageBox.warning(dialog, "未修改费用", str(exc))

        for text, callback in [
            ("恢复所选任务输入为未保存草稿", restore),
            ("查看并采用完整分析/转换候选", attach),
            ("核对所选AI任务的实际费用…", reconcile),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            layout.addWidget(button)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        table.selectRow(0)
        dialog.exec()
