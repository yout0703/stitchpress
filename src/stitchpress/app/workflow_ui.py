"""Visible stage checkpoints, with explicit continuation and foreign-device forks."""

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from stitchpress.core.errors import StitchPressError

from .task_ui import instance_id
from .workflow import ROUTES, begin_session, session_event, workflow_status


class WorkflowFeatures:
    def workflow_board(self):
        try:
            dialog = QDialog(self)
            dialog.setWindowTitle("制版流程与检查点")
            dialog.resize(980, 680)
            layout = QVBoxLayout(dialog)
            notice = QLabel(
                "流程状态来自当前作品的真实文件/矩阵/检查和审核；继续仅执行一个本地步骤，不自动采用候选、不跳过人工冻结，不调用模型。旧流程和AI费用保留。"
            )
            notice.setWordWrap(True)
            layout.addWidget(notice)
            route = QComboBox()
            for key, label in ROUTES.items():
                route.addItem(label, key)
            current = self.draft.entities.get("workflow")
            if current:
                route.setCurrentIndex(route.findData(current["route"]))
            row = QHBoxLayout()
            row.addWidget(route)
            create = QPushButton("新建/本机接续流程")
            row.addWidget(create)
            layout.addLayout(row)
            table = QTableWidget(0, 3)
            table.setHorizontalHeaderLabels(["阶段", "状态", "依据/下一步"])
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.horizontalHeader().setStretchLastSection(True)
            layout.addWidget(table, 1)
            state_label = QLabel()
            state_label.setWordWrap(True)
            layout.addWidget(state_label)
            actions = QHBoxLayout()
            pause = QPushButton("暂停")
            resume = QPushButton("恢复")
            advance = QPushButton("继续下一本地步骤")
            for button in (pause, resume, advance):
                actions.addWidget(button)
            layout.addLayout(actions)

            def refresh():
                status = workflow_status(self.draft, instance_id())
                table.setRowCount(len(status["stages"]))
                for i, stage in enumerate(status["stages"]):
                    for j, text in enumerate(
                        (
                            stage["label"],
                            "沿用已有工程"
                            if status["route"] == "existing" and i < 3
                            else "已完成"
                            if stage["state"] == "completed"
                            else "待处理",
                            stage["detail"],
                        )
                    ):
                        table.setItem(i, j, QTableWidgetItem(text))
                    table.setRowHeight(i, 56)
                state_label.setText(
                    "尚未开始流程"
                    if not status["stages"]
                    else "来自其他设备：请新建本机接续流程，不自动接管远端任务"
                    if status["foreign"]
                    else "已暂停"
                    if status["paused"]
                    else "本地整包阶段已完成；R1、外观和工厂验收仍独立保留"
                    if status["completed"]
                    else "当前待处理："
                    + next(s["label"] for s in status["stages"] if s["key"] == status["next"])
                )
                pause.setEnabled(
                    bool(status["stages"]) and not status["foreign"] and not status["paused"]
                )
                resume.setEnabled(
                    bool(status["stages"]) and not status["foreign"] and status["paused"]
                )
                advance.setEnabled(
                    bool(status["stages"])
                    and not status["foreign"]
                    and not status["paused"]
                    and not status["completed"]
                )

            def begin():
                old = self.draft.entities.get("workflow")
                if old:
                    self.draft.entities["workflow:" + old["run_id"]] = old
                self.draft.entities["workflow"] = begin_session(
                    self.draft,
                    instance_id(),
                    route.currentData(),
                    parent=old["run_id"] if old else None,
                )
                self.dirty = True
                self.update_document_state()
                refresh()

            def event(name):
                try:
                    self.draft.entities["workflow"] = session_event(
                        self.draft.entities["workflow"],
                        instance_id(),
                        name,
                        input_sha=self.design.content_sha,
                    )
                    self.dirty = True
                    self.update_document_state()
                    refresh()
                except Exception as exc:
                    QMessageBox.warning(dialog, "流程未修改", str(exc))

            def step():
                status = workflow_status(self.draft, instance_id())
                event("continue")
                dialog.accept()
                callbacks = {
                    "brief": self.edit_brief,
                    "blueprint": self.import_blueprint_candidate,
                    "convert": self.convert,
                    "quality": self.inspect_master,
                    "review": self.freeze,
                    "export": self.export,
                }
                if status["next"] not in callbacks:
                    raise StitchPressError("没有可继续的本地阶段")
                callbacks[status["next"]]()

            create.clicked.connect(begin)
            pause.clicked.connect(lambda: event("pause"))
            resume.clicked.connect(lambda: event("resume"))
            advance.clicked.connect(step)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            refresh()
            dialog.exec()
        except Exception as exc:
            self.error(exc)
