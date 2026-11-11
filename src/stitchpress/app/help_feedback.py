"""Local usage guidance and separately scoped customer/factory observations."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from stitchpress.core.identity import sha256

HELP = (Path(__file__).parents[1] / "resources" / "help" / "使用说明.txt").read_text(
    encoding="utf-8"
)


class HelpFeedbackFeatures:
    def help_local(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("使用帮助与当前范围")
        dialog.resize(850, 760)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(HELP)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def feedback_local(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("本地反馈与附件")
        dialog.resize(740, 650)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        layout.addLayout(form)
        kind = QComboBox()
        kind.addItems(["使用与交互", "图纸/母版外观", "交付文件", "试印观察", "洗水观察"])
        reviewer = QLineEdit()
        note = QTextEdit()
        attachments = QLabel("尚未添加附件")
        attachments.setWordWrap(True)
        form.addRow("观察类型", kind)
        form.addRow("记录人", reviewer)
        form.addRow("事实/问题/结论（不会自动更改通过状态）", note)
        form.addRow("附件", attachments)
        assets = {}
        records = []

        def add():
            paths, _ = QFileDialog.getOpenFileNames(dialog, "选择本地验收依据，不上传到外部服务")
            try:
                for name in paths:
                    path = Path(name)
                    if path.stat().st_size > 16 * 1024**2 or len(records) >= 10:
                        raise ValueError("最多10件附件，每件不超过16MiB")
                    raw = path.read_bytes()
                    asset = f"evidence/{sha256(raw)}{path.suffix.lower()[:12]}"
                    if sum(len(v) for v in assets.values()) + len(raw) > 64 * 1024**2:
                        raise ValueError("本次附件总量不能超过64MiB")
                    assets[asset] = raw
                    records.append(
                        {
                            "asset": asset,
                            "filename": path.name,
                            "sha256": sha256(raw),
                            "bytes": len(raw),
                        }
                    )
                attachments.setText("\n".join(r["filename"] for r in records) or "尚未添加附件")
            except Exception as exc:
                QMessageBox.warning(dialog, "部分附件未添加", str(exc))

        button = QPushButton("添加附件…")
        button.clicked.connect(add)
        layout.addWidget(button)
        previous = QTextEdit()
        previous.setReadOnly(True)
        history = [v for k, v in self.draft.entities.items() if k.startswith("feedback:")]
        previous.setPlainText(
            "\n\n".join(
                f"{v['at']} · {v['kind']} · {v['reviewer']}\n{v['note']}" for v in history[-10:]
            )
            or "暂无已保存反馈"
        )
        layout.addWidget(previous, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.rejected.connect(dialog.reject)

        def save():
            try:
                if (
                    not reviewer.text().strip()
                    or not note.toPlainText().strip()
                    or len(note.toPlainText()) > 8000
                ):
                    raise ValueError("请填写记录人和8000字以内事实记录")
                import copy

                from .store import Project

                draft = copy.deepcopy(self.draft)
                record = {
                    "at": datetime.now(UTC).isoformat(),
                    "kind": kind.currentText(),
                    "reviewer": reviewer.text().strip(),
                    "note": note.toPlainText(),
                    "design_sha": self.design.content_sha,
                    "attachments": records,
                    "status_effect": "none",
                }
                draft.assets.update(assets)
                draft.entities[f"feedback:{uuid4().hex}"] = record
                Project.decode(draft.encode())
                self.draft = draft
                self.dirty = True
                self.update_document_state()
                dialog.accept()
            except Exception as exc:
                QMessageBox.warning(dialog, "反馈未保存", str(exc))

        buttons.accepted.connect(save)
        layout.addWidget(buttons)
        dialog.exec()
