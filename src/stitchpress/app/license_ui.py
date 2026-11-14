"""Offline activation and a live expiry gate; existing customer files stay untouched."""

import copy
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_STORED

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.files import atomic_write
from stitchpress.platform.licensing import (
    device_request,
    license_manager,
    license_status,
    read_license_file,
)
from stitchpress.platform.settings import settings_dir

FORM_TYPES = (QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox)


def capture_pending_forms(owner, draft):
    records = {}
    for panel in getattr(owner, "reference_panels", ()):
        editor = panel.editor
        widgets = []
        for cls in FORM_TYPES:
            for index, field in enumerate(editor.findChildren(cls)):
                if cls is QLineEdit and isinstance(field.parent(), (QSpinBox, QDoubleSpinBox)):
                    continue
                value = (
                    field.toPlainText()
                    if cls is QTextEdit
                    else field.currentIndex()
                    if cls is QComboBox
                    else field.isChecked()
                    if cls is QCheckBox
                    else field.value()
                    if cls in (QSpinBox, QDoubleSpinBox)
                    else field.text()
                )
                widgets.append({"class": cls.__name__, "index": index, "value": value})
        record = {"widgets": widgets, "assets": {}}
        for name, data in getattr(editor, "assets", {}).items():
            key = "trial-pending/" + panel.kind + "/asset-" + uuid4().hex
            draft.assets[key] = data
            record["assets"][name] = key
        if getattr(editor, "font_data", None):
            key = "trial-pending/" + panel.kind + "/font.bin"
            draft.assets[key] = editor.font_data
            record["font_data"] = key
        records[panel.kind] = record
    records["_post"] = {
        "targets": {key: box.isChecked() for key, box in owner.post_targets.items()},
        "jpeg_quality": owner.jpeg_quality.value(),
        "tiff_container": owner.tiff_container.currentIndex(),
        "bigtiff_confirmed": owner.bigtiff_confirm.isChecked(),
        "effect_cell": owner.effect_cell.value(),
        "thread_rows": [
            {
                **{
                    key: (
                        owner.thread_table.item(i, j).text()
                        if owner.thread_table.item(i, j)
                        else ""
                    )
                    for j, key in enumerate(("ct", "stitches_per_metre", "source_note"))
                },
                "confirmed": owner.thread_table.cellWidget(i, 3).isChecked(),
            }
            for i in range(owner.thread_table.rowCount())
        ],
    }
    return records


def restore_pending_forms(owner, records):
    for panel in getattr(owner, "reference_panels", ()):
        record = records.get(panel.kind)
        if not record:
            continue
        for item in record["widgets"]:
            cls = next(cls for cls in FORM_TYPES if cls.__name__ == item["class"])
            fields = panel.editor.findChildren(cls)
            if item["index"] >= len(fields):
                raise ValueError("恢复表单结构已改变，请保留原草稿并使用对应版本恢复")
            field, value = fields[item["index"]], item["value"]
            if cls is QTextEdit:
                field.setPlainText(value)
            elif cls is QComboBox:
                field.setCurrentIndex(value)
            elif cls is QCheckBox:
                field.setChecked(value)
            elif cls in (QSpinBox, QDoubleSpinBox):
                field.setValue(value)
            else:
                field.setText(value)
        if "font_data" in record:
            panel.editor.font_data = owner.draft.assets[record["font_data"]]
        if record["assets"]:
            panel.editor.assets = {
                name: owner.draft.assets[key] for name, key in record["assets"].items()
            }
        panel.update_state()
    if post := records.get("_post"):
        for key, value in post["targets"].items():
            owner.post_targets[key].setChecked(value)
        owner.jpeg_quality.setValue(post["jpeg_quality"])
        owner.tiff_container.setCurrentIndex(post["tiff_container"])
        owner.bigtiff_confirm.setChecked(post["bigtiff_confirmed"])
        owner.effect_cell.setValue(post["effect_cell"])
        owner.thread_table.setRowCount(0)
        for values in post["thread_rows"]:
            owner.add_thread_row(plan=values)


def describe(status):
    if status.expires_at is None:
        return status.message
    stamp = datetime.fromtimestamp(status.expires_at).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    countdown = ""
    if status.code == "valid":
        import time

        seconds = max(0, int(status.expires_at - time.time()))
        days, remaining = divmod(seconds, 86400)
        hours, remaining = divmod(remaining, 3600)
        minutes = remaining // 60
        countdown = f" · 剩余 {days}天 {hours}小时 {minutes}分"
    return f"{status.message}{countdown}\n到期时间：{stamp}\n授权客户：{status.customer}"


class LicenseDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("StitchPress · 离线七天试用")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        title = QLabel("开始您的七天试用")
        title.setProperty("role", "sectionTitle")
        layout.addWidget(title)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        note = QLabel(
            "无需联网。导出本机机器码交给发行方，再导入收到的授权文件。\n"
            "授权从签发起连续七天有效，到期后业务功能锁定，原有工程和成品保留。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        for label, callback in (
            ("导出本机机器码…", self.export_request),
            ("导入试用授权…", self.import_license),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.continue_button = QPushButton("进入软件")
        self.continue_button.setProperty("role", "primary")
        self.continue_button.clicked.connect(self.continue_trial)
        layout.addWidget(self.continue_button)
        leave = QPushButton("关闭")
        leave.clicked.connect(self.reject)
        layout.addWidget(leave)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.finished.connect(self.timer.stop)
        self.refresh()

    def refresh(self):
        status = license_status()
        self.status.setText(describe(status))
        self.continue_button.setEnabled(status.allowed)

    def continue_trial(self):
        if license_status().allowed:
            self.accept()
        else:
            self.refresh()

    def export_request(self):
        try:
            name, _ = QFileDialog.getSaveFileName(
                self, "保存本机机器码", "StitchPress-机器码.json", "JSON (*.json)"
            )
            if name:
                atomic_write(Path(name), canonical_bytes(device_request()))
        except Exception as exc:
            QMessageBox.warning(self, "机器码未导出", str(exc))

    def import_license(self):
        try:
            name, _ = QFileDialog.getOpenFileName(
                self, "导入试用授权", "", "试用授权 (*.spl-license)"
            )
            if name:
                license_manager().install(read_license_file(name))
                self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "未替换授权", str(exc))


class TrialController:
    def __init__(self, owner):
        self.owner = owner
        self.locked = False
        self.recovery_path = None
        self.disabled_dialogs = []
        self.disabled_actions = []
        self.action = owner.menuBar().addAction("试用授权…")
        self.action.triggered.connect(self.show_license)
        self.label = QLabel()
        self.label.setMaximumWidth(620)
        owner.statusBar().addPermanentWidget(self.label)
        self.timer = QTimer(owner)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def show_license(self):
        dialog = LicenseDialog(self.owner)
        dialog.exec()
        dialog.deleteLater()
        self.refresh()

    def protect_draft(self):
        """An uncompressed recovery copy, never an overwrite of the customer's file."""
        owner = self.owner
        draft = copy.deepcopy(owner.draft)
        draft.entities["recovery_form"] = owner.pending_form()
        # Preserve un-applied settings as well, even if temporarily invalid.
        draft.entities["trial_pending_forms"] = capture_pending_forms(owner, draft)
        folder = settings_dir() / "recovery"
        path = folder / ("trial-expiry-" + uuid4().hex + ".stitchpress")
        payload = draft.encode(compression=ZIP_STORED)
        atomic_write(path, payload)
        from stitchpress.core.identity import sha256

        atomic_write(
            path.with_suffix(".json"),
            canonical_bytes(
                {
                    "schema_version": 1,
                    "project_id": draft.project_id,
                    "title": draft.entities["metadata"]["title"],
                    "original_path": str(owner.project_path) if owner.project_path else None,
                    "created_at": datetime.now().astimezone().isoformat(),
                    "sha256": sha256(payload),
                }
            ),
        )
        self.recovery_path = path

    def refresh(self):
        status = license_status()
        message = describe(status)
        self.label.setText(message.split("\n", 1)[0])
        self.label.setToolTip(message)
        if status.allowed:
            if self.locked:
                self.owner.centralWidget().setEnabled(True)
                for action in self.disabled_actions:
                    action.setEnabled(True)
                for dialog in self.disabled_dialogs:
                    try:
                        dialog.setEnabled(True)
                    except RuntimeError:
                        pass
                self.disabled_actions.clear()
                self.disabled_dialogs.clear()
                self.locked = False
                from .commerce_studio import PreviewBoard

                for board in self.owner.findChildren(PreviewBoard):
                    board._shutdown = False
                    if board.isVisible():
                        board.timer.start()
                self.owner.statusBar().showMessage("授权有效，已恢复业务操作；恢复草稿保留")
            return
        if self.locked:
            return
        self.locked = True
        owner = self.owner
        owner.centralWidget().setEnabled(False)
        for action in owner.findChildren(QAction):
            if action is not self.action and action.isEnabled():
                self.disabled_actions.append(action)
                action.setEnabled(False)
        for dialog in owner.findChildren(QDialog):
            if dialog.isEnabled() and not isinstance(dialog, LicenseDialog):
                self.disabled_dialogs.append(dialog)
                dialog.setEnabled(False)
        from .commerce_studio import PreviewBoard

        for board in owner.findChildren(PreviewBoard):
            board.shutdown()
        from .background_load import cancel_load

        cancel_load(owner)
        if owner.process is not None:
            owner.cancel_job()
        owner.checkpoint_draft()
        try:
            self.protect_draft()
            owner.statusBar().showMessage("试用已锁定，当前草稿已另存至恢复目录")
        except Exception as exc:
            owner.statusBar().showMessage(f"恢复副本保存失败：{exc}；请保留窗口并恢复授权后保存")
