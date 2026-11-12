"""Native template chooser and a cancellable, revision-bound 7+5 preview surface."""

import copy
import json
from uuid import uuid4

from PySide6.QtCore import QEvent, QProcess, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.commerce import CONTENTS
from stitchpress.core.commerce_templates import templates
from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.files import atomic_write
from stitchpress.platform.launch import worker_arguments, worker_program
from stitchpress.platform.settings import settings_dir

from .commerce_templates import apply_preset, apply_template, fingerprint, load_preset


def page_name(target):
    return (
        ("方图 " + target[-1])
        if target.startswith("square")
        else CONTENTS[target.removeprefix("detail-")]
    )


class PreviewBoard(QWidget):
    ready = Signal(dict)
    source_requested = Signal()

    def __init__(self, snapshot, parent=None):
        super().__init__(parent)
        self.snapshot = snapshot
        self.catalog = False
        self.process = None
        self.pages = []
        self.cache = {}
        self.pixmap = QPixmap()
        self.loaded_sha = None
        self.observed_sha = None
        self.attempted_sha = None
        self.invalid_parameters = False
        self.loaded_message = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.status = QLabel("选择模板，预览当前作品的整套商品图片")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.source_button = QPushButton("前往作品绑定原图")
        self.source_button.clicked.connect(self.source_requested.emit)
        self.source_button.hide()
        row.addWidget(self.source_button)
        self.refresh_button = QPushButton("更新整套预览")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        self.cancel_button = QPushButton("取消预览")
        self.cancel_button.clicked.connect(self.stop)
        self.cancel_button.hide()
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        split = self.split = QSplitter()
        self.list = QListWidget()
        self.list.setObjectName("commercePages")
        self.list.setAccessibleName("商品页面缩略图")
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setFlow(QListView.Flow.TopToBottom)
        self.list.setWrapping(False)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setIconSize(QSize(94, 94))
        self.list.setMinimumWidth(160)
        self.list.setMaximumWidth(220)
        self.list.setSpacing(5)
        self.list.currentRowChanged.connect(self.select)
        split.addWidget(self.list)
        self.canvas = QLabel("生成预览后，在左侧选择页面")
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setMinimumSize(260, 280)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.canvas.setObjectName("commerceCanvas")
        self.canvas.setWordWrap(True)
        self.canvas.installEventFilter(self)
        split.addWidget(self.canvas)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.setMinimumHeight(360)
        self.timer = QTimer(self)
        self.timer.setInterval(900)
        self.timer.timeout.connect(self.check_stale)
        self.timer.start()
        QApplication.instance().aboutToQuit.connect(self.stop)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.pages and self.process is None:
            QTimer.singleShot(0, self.refresh)

    def check_stale(self):
        if not self.isVisible() or self.process is not None:
            return
        try:
            key = fingerprint(self.snapshot())
        except Exception as exc:
            self.status.setText("设置尚未完整：" + str(exc))
            self.observed_sha = None
            self.invalid_parameters = True
            return
        if key == self.loaded_sha:
            if self.invalid_parameters:
                self.status.setText(self.loaded_message)
                self.invalid_parameters = False
            return
        self.invalid_parameters = False
        if key == self.attempted_sha:
            return
        if key != self.observed_sha:
            self.observed_sha = key
            self.status.setText("设置已改变 · 停止输入后自动更新，当前画布为上次结果")
            return
        self.refresh()

    def refresh(self):
        self.stop()
        try:
            project = self.snapshot()
            key = fingerprint(project)
            self.attempted_sha = key
            if key in self.cache:
                self.present(self.cache[key])
                return
            folder = settings_dir() / "work" / ("commerce-preview-" + uuid4().hex)
            folder.mkdir(parents=True)
            project_path = folder / "snapshot.stitchpress"
            atomic_write(project_path, project.encode())
            self.output = folder / "result.json"
            self.cancel_file = folder / "cancel"
            job = dict(
                operation="commerce-sheet",
                catalog=self.catalog,
                project=str(project_path),
                output=str(self.output),
                cancel_file=str(self.cancel_file),
                base_sha=key,
            )
            job_path = folder / "job.json"
            atomic_write(job_path, canonical_bytes(job))
            process = QProcess(self)
            self.process = process
            process.setProgram(worker_program())
            process.setArguments(worker_arguments("worker", [str(job_path)]))
            process.finished.connect(lambda code, status: self.finished(process, key, code))
            process.errorOccurred.connect(lambda error: self.process_error(process))
            if self.pixmap.isNull():
                self.canvas.setText("正在生成当前模板…")
            process.start()
            self.status.setText("正在更新 12 张图片 · 当前画布保留上次结果")
            self.cancel_button.show()
        except Exception as exc:
            self.status.setText(str(exc))

    def process_error(self, process):
        if process is self.process and process.error() == QProcess.ProcessError.FailedToStart:
            self.status.setText("无法启动预览进程：" + process.errorString())
            self.process = None
            self.list.setEnabled(True)
            self.cancel_button.hide()

    def finished(self, process, expected, code):
        if process is not self.process:
            process.deleteLater()
            return
        self.process = None
        self.list.setEnabled(True)
        self.cancel_button.hide()
        try:
            if fingerprint(self.snapshot()) != expected:
                self.status.setText("参数已改变 · 即将更新至最新版本")
                return
            record = json.loads(self.output.read_bytes())
            if code != 0 or record.get("status") != "completed":
                raise ValueError(record.get("error", "预览未完成"))
            result = record["result"]
            if result["fingerprint"] != expected:
                raise ValueError("预览版本不匹配，请重试")
            self.cache[expected] = result
            # Bound the in-memory index; rendered files remain in the isolated work folder.
            if len(self.cache) > 8:
                del self.cache[next(iter(self.cache))]
            self.present(result)
        except Exception as exc:
            self.status.setText(str(exc))
        finally:
            process.deleteLater()

    def stop(self):
        if self.process is not None:
            process, self.process = self.process, None
            self.cancel_file.touch()
            process.kill()
            process.waitForFinished(1500)
            process.deleteLater()
            self.status.setText("预览已取消 · 作品未改变")
        self.list.setEnabled(True)
        self.cancel_button.hide()

    def present(self, result):
        row = max(0, self.list.currentRow())
        first = not self.pages
        self.pages = result["pages"]
        self.loaded_sha = result["fingerprint"]
        self.list.blockSignals(True)
        self.list.clear()
        for i, page in enumerate(self.pages):
            item = QListWidgetItem(f"{i + 1:02d}  {page_name(page['target'])}")
            item.setSizeHint(QSize(138, 130))
            if "thumb" in page:
                item.setIcon(QIcon(page["thumb"]))
            else:
                item.setText(item.text() + "\n需要补充素材")
                item.setToolTip(page.get("error", ""))
            self.list.addItem(item)
        errors = sum("error" in p for p in self.pages)
        self.status.setText(
            f"7 张详情 · 5 张方图 · {12 - errors}/12 可预览"
            + (" · 点击未完成页查看原因" if errors else " · 草稿效果")
        )
        self.loaded_message = self.status.text()
        selected = min(row, len(self.pages) - 1)
        self.list.setCurrentRow(selected)
        self.list.blockSignals(False)
        self.select(selected)
        if first:
            self.list.currentRowChanged.emit(selected)
        self.ready.emit(result)

    def select(self, row):
        if not 0 <= row < len(self.pages):
            return
        page = self.pages[row]
        self.source_button.setVisible(
            "error" in page and any(word in page["error"] for word in ("原图", "来源"))
        )
        self.pixmap = QPixmap(page.get("preview", ""))
        if self.pixmap.isNull():
            self.canvas.setPixmap(QPixmap())
            self.canvas.setText(page.get("error", "尚无预览"))
        else:
            self.fit()

    def fit(self):
        if not self.pixmap.isNull():
            ratio = self.canvas.devicePixelRatioF()
            size = (self.canvas.size() - QSize(20, 20)) * ratio
            shown = self.pixmap.scaled(
                size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            shown.setDevicePixelRatio(ratio)
            self.canvas.setPixmap(shown)

    def eventFilter(self, watched, event):
        if watched is self.canvas and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.fit)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)


class TemplatePicker(QDialog):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.current = editor.values()
        self.base_project = editor.snapshot()
        self.result_settings = self.current
        self.result_assets = {}
        self.entries = [("经典模板", None)] + [
            (t["name"] + "\n" + t["description"], t) for t in templates()
        ]
        folder = settings_dir() / "commerce-templates"
        self.preset_errors = []
        for path in sorted(folder.glob("*.stitchtemplate")):
            try:
                preset = load_preset(path)
                self.entries.append(
                    (
                        "我的模板 · "
                        + preset.entities["commerce_preset"]["name"]
                        + f" · v{preset.entities['commerce_preset']['version']}",
                        preset,
                    )
                )
            except Exception:
                self.preset_errors.append(path.name)
        self.setWindowTitle("选择商品模板")
        self.resize(1100, 780)
        layout = QVBoxLayout(self)
        heading = QLabel("让同一件作品，拥有不同的表达")
        heading.setProperty("role", "sectionTitle")
        layout.addWidget(heading)
        line = QHBoxLayout()
        self.keep = QCheckBox("保留当前场景、相框和背景")
        self.keep.setChecked(True)
        self.reset_text = QCheckBox("重置为模板默认文案")
        line.addWidget(self.keep)
        line.addWidget(self.reset_text)
        line.addStretch()
        layout.addLayout(line)
        body = QHBoxLayout()
        self.choices = QListWidget()
        self.choices.setObjectName("commerceTemplates")
        self.choices.setAccessibleName("整套商品模板")
        self.choices.setFixedWidth(245)
        self.choices.setWordWrap(True)
        for label, _ in self.entries:
            item = QListWidgetItem(label)
            item.setSizeHint(QSize(225, 78))
            self.choices.addItem(item)
        body.addWidget(self.choices)
        self.board = PreviewBoard(self.snapshot, self)
        self.board.catalog = True
        self.board.ready.connect(self.show_covers)
        self.choices.setIconSize(QSize(58, 78))
        body.addWidget(self.board, 1)
        layout.addLayout(body, 1)
        if self.preset_errors:
            layout.addWidget(
                QLabel(f"{len(self.preset_errors)} 个预设读取失败，已跳过；原文件保留。")
            )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("使用此模板")
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.choices.currentRowChanged.connect(self.changed)
        self.keep.toggled.connect(self.changed)
        self.reset_text.toggled.connect(self.changed)
        current_id = self.current.template["id"] if self.current.template else None
        self.choices.setCurrentRow(
            next(
                (
                    i
                    for i, (_, v) in enumerate(self.entries[:5])
                    if (v["id"] if v else None) == current_id
                ),
                0,
            )
        )
        self.finished.connect(self.board.stop)

    def show_covers(self, result):
        for index, (_, value) in enumerate(self.entries):
            if isinstance(value, dict) and value["id"] in result.get("covers", {}):
                self.choices.item(index).setIcon(QIcon(result["covers"][value["id"]]))

    def changed(self, *args):
        self.board.stop()
        choice = self.entries[max(0, self.choices.currentRow())][1]
        options = dict(keep_scenes=self.keep.isChecked(), reset_text=self.reset_text.isChecked())
        if hasattr(choice, "entities"):
            self.result_settings = apply_preset(self.current, choice, **options)
            self.result_assets = choice.assets
        else:
            self.result_settings = apply_template(self.current, choice, **options)
            self.result_assets = {}
        self.board.refresh()

    def snapshot(self):
        project = copy.deepcopy(self.base_project)
        project.entities["commerce"] = self.result_settings.to_dict()
        project.assets.update(self.result_assets)
        return project
