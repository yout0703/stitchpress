import copy
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QProcess, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.conversion import normalize_image
from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.identity import canonical_bytes, sha256
from stitchpress.core.importers import import_oxs
from stitchpress.core.layout import Fabric, physical_layout
from stitchpress.core.models import Design, PatchCell, ReviewRecord, apply_patch
from stitchpress.core.render import screen_rgb, stitch_preview
from stitchpress.core.resources import assign_symbols, library_from_assets
from stitchpress.core.xsd import import_xsd
from stitchpress.platform.files import atomic_write
from stitchpress.platform.launch import worker_arguments, worker_program
from stitchpress.platform.settings import load_settings, save_secret, save_settings, settings_dir

from .ai import ImageServiceProfile, ServiceProfile
from .fixtures import demo_project
from .store import Coordinator, Project


class Canvas(QGraphicsView):
    region_selected = Signal(QRectF)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._start = None
        self.selection_item = None

    def set_image(self, image, fit=False):
        self.scene().clear()
        self.selection_item = None
        self.scene().addPixmap(QPixmap.fromImage(ImageQt(image)))
        self.setSceneRect(QRectF(0, 0, image.width, image.height))
        if fit:
            self.fit()

    def fit(self):
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        if (
            0.03 < self.transform().m11() < 30
            or (self.transform().m11() <= 0.03 and event.angleDelta().y() > 0)
            or (self.transform().m11() >= 30 and event.angleDelta().y() < 0)
        ):
            factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = self.mapToScene(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._start is not None:
            end = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._start, end).normalized().intersected(self.sceneRect())
            self._start = None
            if rect.width() > 2 and rect.height() > 2:
                self.region_selected.emit(rect)
                if self.selection_item:
                    self.scene().removeItem(self.selection_item)
                pen = QPen(QColor("#df763b"), 2)
                pen.setCosmetic(True)
                self.selection_item = self.scene().addRect(rect, pen)


class AISettings(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 服务配置")
        self.resize(620, 540)
        self.config = load_settings()
        profile = ServiceProfile(**self.config.get("ai_profile", {}))
        layout = QVBoxLayout(self)
        note = QLabel(
            "默认 DeepSeek Flash 用于需求与图片分析。\n"
            "密钥保存于本机系统安全存储；不会写入作品、日志或交付包。\n"
            "图像生成与编辑服务尚待接入，配置文本模型不会启用这些能力。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.endpoint = QLineEdit(profile.endpoint)
        self.model = QLineEdit(profile.model)
        self.reference = QLineEdit(profile.credential_ref)
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("填写新密钥；留空保留系统安全存储中的原值")
        self.vision = QCheckBox("发送图片供模型理解")
        self.vision.setChecked(profile.vision)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 300)
        self.timeout.setValue(profile.timeout_seconds)
        for label, widget in (
            ("服务根地址", self.endpoint),
            ("模型 ID", self.model),
            ("本机凭据标识", self.reference),
            ("API 密钥", self.secret),
            ("能力", self.vision),
            ("超时 / 秒", self.timeout),
        ):
            form.addRow(label, widget)
        service_tabs = QTabWidget()
        text_page = QWidget()
        text_page.setLayout(form)
        service_tabs.addTab(text_page, "文字 / 图片理解")
        image_page = QWidget()
        image_form = QFormLayout(image_page)
        image_profile = ImageServiceProfile(**self.config.get("image_profile", {}))
        self.image_name = QLineEdit(image_profile.name)
        self.image_endpoint = QLineEdit(image_profile.endpoint)
        self.generation_model = QLineEdit(image_profile.generation_model)
        self.editing_model = QLineEdit(image_profile.editing_model)
        self.image_reference = QLineEdit(image_profile.credential_ref)
        self.image_secret = QLineEdit()
        self.image_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.image_secret.setPlaceholderText("留空保留原值；密钥只进入本机系统安全存储")
        for label, field in (
            ("供应商名称", self.image_name),
            ("服务根地址", self.image_endpoint),
            ("生成模型 ID", self.generation_model),
            ("编辑模型 ID", self.editing_model),
            ("本机凭据标识", self.image_reference),
            ("API 密钥", self.image_secret),
        ):
            image_form.addRow(label, field)
        pending = QLabel(
            "待供应商接口确认与实际验证。当前仅保存连接配置，尚不能发起图像生成/编辑。"
        )
        pending.setWordWrap(True)
        image_form.addRow(pending)
        service_tabs.addTab(image_page, "图像生成 / 编辑")
        layout.addWidget(service_tabs)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        try:
            profile = ServiceProfile(
                endpoint=self.endpoint.text().strip(),
                model=self.model.text().strip(),
                credential_ref=self.reference.text().strip(),
                vision=self.vision.isChecked(),
                timeout_seconds=self.timeout.value(),
            )
            image_profile = ImageServiceProfile(
                name=self.image_name.text().strip(),
                endpoint=self.image_endpoint.text().strip(),
                generation_model=self.generation_model.text().strip(),
                editing_model=self.editing_model.text().strip(),
                credential_ref=self.image_reference.text().strip(),
            )
            if self.image_secret.text() and not image_profile.endpoint:
                raise StitchPressError("保存图像服务密钥前请先填写服务地址")
            if self.secret.text():
                save_secret(profile.credential_key, self.secret.text())
            if self.image_secret.text():
                save_secret(image_profile.credential_key, self.image_secret.text())
            self.config["ai_profile"] = profile.to_dict()
            self.config["image_profile"] = image_profile.to_dict()
            self.config.setdefault("installation_id", uuid4().hex)
            save_settings(self.config)
            self.secret.clear()
            self.image_secret.clear()
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "未保存配置", str(exc))


class Workbench(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("StitchPress · 绣印工坊 · 开发试验版")
        self.resize(1320, 860)
        self.coordinator = None
        self.project_path = None
        self.draft = demo_project()
        self.baseline = copy.deepcopy(self.draft)
        self.process = None
        self.job = None
        self.region = None
        self.review_regions = []
        self.dirty = False
        self.source_image = None
        self.history = []
        self._refreshing = False
        self.build_ui()
        self.refresh(fit=True)

    def build_ui(self):
        toolbar = QToolBar("文件与视图")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        actions = [
            ("新建示例", self.new_demo, QKeySequence.StandardKey.New),
            ("打开作品", self.open_project, QKeySequence.StandardKey.Open),
            ("保存", self.save, QKeySequence.StandardKey.Save),
            ("另存", self.save_as, QKeySequence.StandardKey.SaveAs),
            ("导入 XSD / OXS / JSON", self.import_design, None),
            ("客户资源", self.import_resources, None),
            ("AI 配置", self.ai_settings, None),
        ]
        for text, callback, shortcut in actions:
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(shortcut)
            action.triggered.connect(callback)
            toolbar.addAction(action)
        splitter = QSplitter()
        self.setCentralWidget(splitter)
        panel = QWidget()
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(390)
        pl = QVBoxLayout(panel)
        pl.addWidget(QLabel("STITCHPRESS  /  作品工作台"))
        self.title_edit, self.code_edit = QLineEdit(), QLineEdit()
        form = QFormLayout()
        form.addRow("作品名称", self.title_edit)
        form.addRow("客户货号", self.code_edit)
        self.sx, self.sy = QLineEdit("0.181"), QLineEdit("0.181")
        form.addRow("横向格距 cm", self.sx)
        form.addRow("纵向格距 cm", self.sy)
        self.margins = {key: QLineEdit("5.430") for key in ("left", "top", "right", "bottom")}
        for label, key in (
            ("左留白 cm", "left"),
            ("上留白 cm", "top"),
            ("右留白 cm", "right"),
            ("下留白 cm", "bottom"),
        ):
            form.addRow(label, self.margins[key])
        pl.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        pl.addWidget(self.summary)
        for widget in (self.title_edit, self.code_edit, self.sx, self.sy, *self.margins.values()):
            widget.editingFinished.connect(self.apply_fields)
        source_box = QGroupBox("图片 → 针格候选")
        sf = QFormLayout(source_box)
        self.grid_w, self.grid_h, self.color_limit = QSpinBox(), QSpinBox(), QSpinBox()
        for widget, value, limit in (
            (self.grid_w, 160, 1000),
            (self.grid_h, 160, 1000),
            (self.color_limit, 32, 66),
        ):
            widget.setRange(1, limit)
            widget.setValue(value)
        sf.addRow("针格宽", self.grid_w)
        sf.addRow("针格高", self.grid_h)
        sf.addRow("最多颜色", self.color_limit)
        self.alpha = QComboBox()
        self.alpha.addItems(["alpha <128 留空", "合成白底 / 全部有针"])
        self.alpha.setToolTip("阈值处理透明边缘；不把不透明白色当作空白。合成白底模式全格有针。")
        sf.addRow("半透明处理", self.alpha)
        self.source_label = QLabel("尚未选择蓝图；干净平面图可直接转换")
        self.source_label.setWordWrap(True)
        sf.addRow(self.source_label)
        sf.addRow(self.button("选择图片", self.choose_image))
        sf.addRow(self.button("本地转换候选", self.convert))
        pl.addWidget(source_box)
        local = QGroupBox("局部审查与修订")
        ll = QVBoxLayout(local)
        self.region_label = QLabel("在图上拖动选择针格区域")
        self.region_label.setWordWrap(True)
        ll.addWidget(self.region_label)
        self.patch_color = QComboBox()
        ll.addWidget(self.patch_color)
        ll.addWidget(self.button("将选区改为此线色", self.patch_region))
        ll.addWidget(self.button("撤销上次局部修订", self.undo_patch))
        ll.addWidget(self.button("审核并冻结母版", self.freeze))
        pl.addWidget(local)
        pl.addStretch()
        sidebar = QScrollArea()
        sidebar.setWidgetResizable(True)
        sidebar.setWidget(panel)
        sidebar.setMinimumWidth(310)
        sidebar.setMaximumWidth(410)
        splitter.addWidget(sidebar)
        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        center = QWidget()
        cl = QVBoxLayout(center)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("X 针格模拟 · 3:4 · 非实绣照片"))
        self.canvas = Canvas()
        self.canvas.region_selected.connect(self.select_region)
        controls.addStretch()
        controls.addWidget(self.button("适应窗口", self.canvas.fit))
        controls.addWidget(self.button("100% 像素", self.actual_pixels))
        cl.addLayout(controls)
        cl.addWidget(self.canvas)
        self.tabs.addTab(center, "母版预览")
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["No.", "线号", "线色", "针数", "符号 ID"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setIconSize(QSize(32, 32))
        self.table.itemSelectionChanged.connect(self.highlight)
        symbols_tab = QWidget()
        sl = QVBoxLayout(symbols_tab)
        sl.addWidget(self.table)
        sl.addWidget(self.button("调整选中线色的符号 / 事务交换", self.change_symbol))
        self.tabs.addTab(symbols_tab, "线色与符号")
        self.source_canvas = Canvas()
        self.tabs.addTab(self.source_canvas, "对照原图")
        ai_tab = QWidget()
        al = QVBoxLayout(ai_tab)
        al.addWidget(QLabel("DeepSeek Flash：文字/图片需求分析（点击后会调用你配置的API账号）"))
        self.prompt = QTextEdit()
        self.prompt.setPlaceholderText("描述必须保留的对象、构图、负空间和允许简化的部分……")
        self.prompt.setMaximumHeight(160)
        al.addWidget(self.prompt)
        self.ai_include_image = QCheckBox("本次附带当前对照图片，发送到已配置的AI服务")
        self.ai_include_image.setChecked(True)
        al.addWidget(self.ai_include_image)
        al.addWidget(self.button("分析设计要求 · 单次调用 · 费用以服务账单为准", self.run_ai))
        self.ai_result = QTextEdit()
        self.ai_result.setReadOnly(True)
        self.ai_result.setPlaceholderText("尚未配置/调用。模型建议不会自动改针格或冻结母版。")
        al.addWidget(self.ai_result)
        self.tabs.addTab(ai_tab, "AI 制版助手")
        export_tab = QWidget()
        el = QVBoxLayout(export_tab)
        note = QLabel(
            "整包：生产TIFF + N张A4查图 + PDF + 配线Excel + X模拟 + 中英文说明。\n"
            "示例库只供演示。正式导出需要客户资源及当前母版审核记录。\n"
            "当前未完成Windows和工厂验证；PDF外部渲染/Excel实机结果分别登记。"
        )
        note.setWordWrap(True)
        el.addWidget(note)
        self.demo_mode = QCheckBox("演示模式（不能作为正式生产验收）")
        self.demo_mode.setChecked(True)
        el.addWidget(self.demo_mode)
        el.addWidget(self.button("导出 N+6 整包", self.export))
        el.addWidget(self.button("取消当前本地任务", self.cancel_job))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        el.addWidget(self.log)
        self.tabs.addTab(export_tab, "导出与任务")
        splitter.setSizes([330, 990])
        self.statusBar().showMessage("开发试验版 · 示例不含客户私有素材 · 尚未经过生产验收")

    @staticmethod
    def button(text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        return button

    @property
    def design(self):
        return Design.from_dict(self.draft.entities["design"])

    def error(self, exc):
        QMessageBox.warning(self, "操作未完成", str(exc))

    def apply_fields(self):
        if self._refreshing:
            return True
        try:
            current = self.draft.entities["fabric"]
            fabric = Fabric(
                **{
                    **current,
                    "sx": self.sx.text(),
                    "sy": self.sy.text(),
                    **{k: widget.text() for k, widget in self.margins.items()},
                }
            )
            metadata = {"title": self.title_edit.text(), "code": self.code_edit.text()}
            if self.draft.entities["metadata"] != metadata or current != asdict(fabric):
                self.draft.entities["metadata"] = metadata
                self.draft.entities["fabric"] = asdict(fabric)
                self.dirty = True
                self.refresh()
            return True
        except Exception as exc:
            self.error(exc)
            return False

    def refresh(self, fit=False):
        self._refreshing = True
        try:
            design = self.design
            fabric = Fabric(**self.draft.entities["fabric"])
            library = library_from_assets(self.draft.entities["library"], self.draft.assets)
            mapping = assign_symbols(design, library, self.draft.entities.get("mapping"))
            self.draft.entities["mapping"] = mapping
            metadata = self.draft.entities["metadata"]
            self.title_edit.setText(metadata["title"])
            self.code_edit.setText(metadata["code"])
            self.sx.setText(fabric.sx)
            self.sy.setText(fabric.sy)
            for key, widget in self.margins.items():
                widget.setText(getattr(fabric, key))
            physical = physical_layout(design.width, design.height, fabric)
            self.summary.setText(
                f"{design.width} × {design.height} 格  /  {sum(design.counts):,} 针\n"
                f"{len(mapping)} 色  /  画心 {physical.width_cm} × "
                f"{physical.height_cm} cm\n"
                f"生产 {physical.canvas_px[0]} × {physical.canvas_px[1]} px\n"
                + ("已记录母版审核" if self.draft.entities.get("freeze") else "待审查/冻结")
            )
            self.canvas.set_image(stitch_preview(design), fit=fit)
            self.table.setRowCount(0)
            self.patch_color.clear()
            self.patch_color.addItem("无针 / 留空", -1)
            for i, color in enumerate(design.palette):
                self.patch_color.addItem(f"{color.brand} {color.code}", i)
                if not design.counts[i]:
                    continue
                row = self.table.rowCount()
                self.table.insertRow(row)
                for col, value in enumerate(
                    (row + 1, color.code, "●", design.counts[i], mapping[color.key])
                ):
                    item = QTableWidgetItem(str(value))
                    item.setData(Qt.ItemDataRole.UserRole, i)
                    if col == 2:
                        item.setForeground(QColor(*color.rgb))
                    if col == 4:
                        symbol = library.by_key[mapping[color.key]]
                        rgb = screen_rgb(symbol.image(), library.icc)
                        item.setIcon(QIcon(QPixmap.fromImage(ImageQt(rgb))))
                    self.table.setItem(row, col, item)
                self.table.setRowHeight(row, 44)
            self.table.resizeColumnsToContents()
        finally:
            self._refreshing = False

    def actual_pixels(self):
        self.canvas.resetTransform()

    def change_symbol(self):
        selected = self.table.selectedItems()
        if not selected:
            return self.error("请先选择一行线色")
        try:
            color = self.design.palette[selected[0].data(Qt.ItemDataRole.UserRole)]
            library = library_from_assets(self.draft.entities["library"], self.draft.assets)
            keys = [s.key for s in library.symbols]
            mapping = self.draft.entities["mapping"].copy()
            chosen, accepted = QInputDialog.getItem(
                self,
                "绑定符号",
                "已被占用的符号将整体交换",
                keys,
                keys.index(mapping[color.key]),
                False,
            )
            if not accepted or chosen == mapping[color.key]:
                return
            other = next((k for k, v in mapping.items() if v == chosen), None)
            if other is not None:
                mapping[other] = mapping[color.key]
            mapping[color.key] = chosen
            assign_symbols(self.design, library, mapping)
            self.draft.entities[f"mapping:{uuid4().hex}"] = {
                "design_sha": self.design.content_sha,
                "library_revision": library.revision,
                "before": self.draft.entities["mapping"],
                "after": mapping,
                "manual": True,
            }
            self.draft.entities["mapping"] = mapping
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def highlight(self):
        if self._refreshing:
            return
        selected = self.table.selectedItems()
        color = selected[0].data(Qt.ItemDataRole.UserRole) if selected else None
        self.canvas.set_image(stitch_preview(self.design, highlight=color))

    def select_region(self, rect):
        design = self.design
        image_w, image_h = self.canvas.sceneRect().width(), self.canvas.sceneRect().height()
        dx, dy = (image_w - design.width * 18) // 2, (image_h - design.height * 18) // 2
        region = (
            max(0, math.floor((rect.left() - dx) / 18)),
            max(0, math.floor((rect.top() - dy) / 18)),
            min(design.width, math.ceil((rect.right() - dx) / 18)),
            min(design.height, math.ceil((rect.bottom() - dy) / 18)),
        )
        if region[0] < region[2] and region[1] < region[3]:
            self.region = region
            if region not in self.review_regions:
                self.review_regions.append(region)
            self.region_label.setText(
                f"列 {region[0] + 1}–{region[2]} / 行 {region[1] + 1}–{region[3]}\n"
                f"已查看 {len(self.review_regions)} 处不同局部"
            )

    def patch_region(self):
        try:
            if not self.region:
                raise StitchPressError("请先在图上拖动选择区域")
            design = self.design
            x0, y0, x1, y1 = self.region
            after = self.patch_color.currentData()
            cells = tuple(
                PatchCell(x, y, design.labels[y][x], after)
                for y in range(y0, y1)
                for x in range(x0, x1)
                if design.labels[y][x] != after
            )
            library = library_from_assets(self.draft.entities["library"], self.draft.assets)
            result = apply_patch(
                design,
                base_sha=design.content_sha,
                cells=cells,
                allowed=frozenset((x, y) for y in range(y0, y1) for x in range(x0, x1)),
                capacity=len(library.symbols),
            )
            self.adopt_design(result)
            self.draft.entities[f"patch:{result.revision_id}"] = {
                "base_sha": design.content_sha,
                "result_sha": result.content_sha,
                "region": (x0, y0, x1, y1),
                "cells": [asdict(cell) for cell in cells],
            }
        except Exception as exc:
            self.error(exc)

    def undo_patch(self):
        if self.history:
            previous = self.history.pop()
            self.draft.entities["design"] = previous["design"]
            self.draft.entities["mapping"] = previous["mapping"]
            self.draft.entities["freeze"] = previous["freeze"]
            self.review_regions = []
            self.dirty = True
            self.refresh()

    def adopt_design(self, design):
        """Validate before changing the active revision; failed imports cannot destroy it."""
        library = library_from_assets(self.draft.entities["library"], self.draft.assets)
        mapping = assign_symbols(design, library, self.draft.entities.get("mapping"))
        stitch_preview(design)  # Enforce preview resource bounds before mutating current data.
        old = {
            "design": self.design.to_dict(),
            "mapping": copy.deepcopy(self.draft.entities.get("mapping")),
            "freeze": copy.deepcopy(self.draft.entities.get("freeze")),
        }
        self.history.append(old)
        self.draft.entities[f"revision:{self.design.revision_id}"] = old
        self.draft.entities["design"] = design.to_dict()
        self.draft.entities["mapping"] = mapping
        self.draft.entities.pop("freeze", None)
        self.region, self.review_regions = None, []
        self.region_label.setText("在图上拖动选择针格区域")
        self.dirty = True
        self.refresh(fit=True)

    def freeze(self):
        try:
            if len(self.review_regions) < 2:
                raise StitchPressError("请在图上选择并检查至少两处不同局部，再登记整体/负空间审核")
            dialog = QDialog(self)
            dialog.setWindowTitle("登记当前母版审核")
            form = QFormLayout(dialog)
            reviewer = QLineEdit()
            notes = QLineEdit()
            form.addRow("审核人", reviewer)
            form.addRow("审查说明", notes)
            checks = [
                QCheckBox(t)
                for t in ("已审整体和关键对象", "已审应留空的负空间", "已核对来源与当前母版")
            ]
            for item in checks:
                form.addRow(item)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            record = ReviewRecord(
                self.design.content_sha,
                reviewer.text(),
                checks[0].isChecked(),
                tuple(self.review_regions),
                checks[1].isChecked(),
                checks[2].isChecked(),
                notes.text(),
            )
            record.validate(self.design)
            self.draft.entities["freeze"] = asdict(record)
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def confirm_discard(self):
        return (
            not self.dirty
            or QMessageBox.question(
                self,
                "未保存修改",
                "放弃当前尚未保存的修改？",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Discard
        )

    def set_project(self, project, path=None, coordinator=None):
        design = Design.from_dict(project.entities["design"])
        Fabric(**project.entities["fabric"])
        library = library_from_assets(project.entities["library"], project.assets)
        assign_symbols(design, library, project.entities.get("mapping"))
        stitch_preview(design)
        if any(
            not isinstance(project.entities.get("metadata", {}).get(k), str)
            for k in ("title", "code")
        ):
            raise StitchPressError("作品名称/客户号字段损坏")
        if self.coordinator:
            self.coordinator.close()
        self.coordinator, self.project_path = coordinator, path
        self.draft = copy.deepcopy(project)
        self.baseline = copy.deepcopy(project)
        self.region, self.review_regions, self.history = None, [], []
        self.source_image = None
        self.source_canvas.scene().clear()
        self.source_label.setText("尚未选择蓝图；干净平面图可直接转换")
        source = self.draft.entities.get("source")
        if source:
            self.source_image = self.draft.assets.get(source["asset"])
            if self.source_image:
                try:
                    self.display_source(self.source_image)
                    self.source_label.setText("已恢复对照图；显示已归正并转换到sRGB")
                except StitchPressError:
                    self.source_label.setText("源图显示需要修正ICC；原件已保留，母版仍可使用")
        self.dirty = False
        self.refresh(fit=True)

    def new_demo(self):
        if self.confirm_discard():
            self.set_project(demo_project())

    def open_project(self):
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开作品", "", "StitchPress (*.stitchpress)")
        if not path:
            return
        coordinator = None
        try:
            if self.project_path and Path(path).resolve() == self.project_path.resolve():
                return
            coordinator = Coordinator(Path(path))
            self.set_project(coordinator.project, Path(path), coordinator)
        except ConflictError:
            self.set_project(Project.decode(Path(path).read_bytes()))
            self.statusBar().showMessage("作品由另一实例写入，当前为副本；请另存")
        except Exception as exc:
            if coordinator is not None and coordinator is not self.coordinator:
                coordinator.close()
            self.error(exc)

    def save(self):
        if not self.apply_fields():
            return False
        if self.coordinator is None:
            return self.save_as()
        try:
            changes = {
                k: v for k, v in self.draft.entities.items() if v != self.baseline.entities.get(k)
            }
            # Removing freeze is represented explicitly so saves cannot retain old approval.
            for key in self.baseline.entities.keys() - self.draft.entities.keys():
                changes[key] = None
            result = self.coordinator.commit(
                expected_seq=self.baseline.commit_seq,
                reads={k: self.baseline.revision(k) for k in changes},
                changes=changes,
                assets=self.draft.assets,
            )
            self.baseline = copy.deepcopy(result)
            self.draft = copy.deepcopy(result)
            self.dirty = False
            self.statusBar().showMessage(f"已保存 · 提交 {result.commit_seq}")
            return True
        except Exception as exc:
            self.error(exc)
            return False

    def save_as(self):
        if not self.apply_fields():
            return False
        path, _ = QFileDialog.getSaveFileName(
            self, "另存作品", "作品.stitchpress", "StitchPress (*.stitchpress)"
        )
        if not path:
            return False
        try:
            target = Path(path)
            if target.suffix.lower() != ".stitchpress":
                target = target.with_name(target.name + ".stitchpress")
            if target.exists():
                raise StitchPressError("另存不能覆盖已有作品，请选择新文件名")
            draft = copy.deepcopy(self.draft)
            draft.project_id = uuid4().hex
            coordinator = Coordinator(target, draft)
            try:
                coordinator.commit(expected_seq=draft.commit_seq, reads={}, changes={})
            except BaseException:
                coordinator.close()
                raise
            self.set_project(coordinator.project, target, coordinator)
            return True
        except Exception as exc:
            self.error(exc)
            return False

    def import_design(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入全针母版", "", "XSD / OXS / JSON (*.xsd *.oxs *.json)"
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            suffix = Path(path).suffix.lower()
            design = (
                import_oxs(data)
                if suffix == ".oxs"
                else import_xsd(data)
                if suffix == ".xsd"
                else Design.from_dict(json.loads(data))
            )
            self.adopt_design(design)
            self.draft.assets[f"sources/{sha256(data)}{Path(path).suffix.lower()}"] = data
            self.review_regions = []
            self.dirty = True
            self.refresh(fit=True)
        except Exception as exc:
            self.error(exc)

    def import_resources(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入已核验的本地资源包", "", "StitchPress (*.stitchpress)"
        )
        if not path:
            return
        try:
            resource = Project.decode(Path(path).read_bytes())
            library = library_from_assets(resource.entities["library"], resource.assets)
            mapping = assign_symbols(self.design, library)
            self.draft.assets.update(resource.assets)
            for key in ("library", "catalog", "source_profile"):
                if key in resource.entities:
                    self.draft.entities[key] = resource.entities[key]
            self.draft.entities["mapping"] = mapping
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def choose_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择干净蓝图 / 对照原图", "", "Images (*.png *.jpg *.jpeg *.tif *.tiff *.webp)"
        )
        if path:
            try:
                data = Path(path).read_bytes()
                self.display_source(data)
            except Exception as exc:
                self.error(exc)
                return
            self.source_image = data
            name = f"sources/{sha256(self.source_image)}{Path(path).suffix.lower()}"
            self.draft.assets[name] = self.source_image
            self.draft.entities["source"] = {"asset": name, "role": "unreviewed-input"}
            self.source_label.setText(Path(path).name + "\n直接转换仅适合干净蓝图，需人工审核")
            self.dirty = True

    def display_source(self, data):
        from PIL import Image

        normalized, _ = normalize_image(data)
        display = Image.new("RGBA", normalized.size, "white")
        display.alpha_composite(normalized)
        display.thumbnail((4096, 4096))
        self.source_canvas.set_image(display.convert("RGB"), fit=True)

    def ai_settings(self):
        AISettings(self).exec()

    def start_worker(self, operation, job):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            raise StitchPressError("当前已有任务运行，请先等待或安全取消")
        folder = settings_dir() / "work" / uuid4().hex
        folder.mkdir(parents=True)
        output = folder / "result.json"
        job.update(operation=operation, output=str(output), base_sha=self.design.content_sha)
        if self.source_image and job.get("include_image", True):
            atomic_write(folder / "source.image", self.source_image)
            job["image"] = str(folder / "source.image")
        if operation == "convert":
            catalog = self.draft.entities.get("catalog")
            if not catalog:
                raise StitchPressError("请先导入本地线色/符号资源包，再转换客户蓝图")
            atomic_write(folder / "catalog.json", self.draft.assets[catalog["asset"]])
            job["catalog"] = str(folder / "catalog.json")
        job_path = folder / "job.json"
        atomic_write(job_path, canonical_bytes(job))
        self.job = {
            "operation": operation,
            "output": output,
            "base_sha": job["base_sha"],
            "project_id": self.draft.project_id,
            "source_sha": sha256(self.source_image) if self.source_image else None,
            "include_image": job.get("include_image", True),
        }
        self.process = QProcess(self)
        self.process.setProgram(worker_program())
        self.process.setArguments(worker_arguments("worker", [str(job_path)]))
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.finished.connect(self.worker_finished)
        self.process.errorOccurred.connect(self.worker_error)
        self.process.readyReadStandardError.connect(self.read_stderr)
        self.process.start()
        self.statusBar().showMessage("后台任务运行中；结果不会自动覆盖其他分支")

    def convert(self):
        try:
            if not self.source_image:
                raise StitchPressError("请先选择干净蓝图")
            self.start_worker(
                "convert",
                {
                    "options": {
                        "width": self.grid_w.value(),
                        "height": self.grid_h.value(),
                        "max_colors": self.color_limit.value(),
                        "alpha_mode": "threshold"
                        if self.alpha.currentIndex() == 0
                        else "composite",
                    }
                },
            )
        except Exception as exc:
            self.error(exc)

    def run_ai(self):
        try:
            if not self.prompt.toPlainText().strip():
                raise StitchPressError("请输入本次分析的设计要求")
            config = load_settings()
            if "ai_profile" not in config:
                raise StitchPressError("请先打开AI配置，保存服务参数与API密钥")
            self.start_worker(
                "analyze",
                {
                    "profile": config["ai_profile"],
                    "prompt": self.prompt.toPlainText(),
                    "include_image": self.ai_include_image.isChecked(),
                },
            )
        except Exception as exc:
            self.error(exc)

    def read_progress(self):
        if self.process:
            content = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
            self.log.append(content.strip())

    def read_stderr(self):
        if self.process:
            message = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
            self.log.append(message.strip()[:2000])

    def worker_error(self, error):
        self.log.append(f"后台任务未启动或异常退出：{self.process.errorString()}")
        self.statusBar().showMessage("后台任务失败；原母版和已有完整交付未改变")

    def worker_finished(self, exit_code, exit_status):
        job = self.job
        if not job:
            return
        try:
            if job["operation"] == "export":
                success = exit_code == 0 and job["destination"].is_dir()
                self.statusBar().showMessage(
                    "导出完成 · 待人工外观检查" if success else "导出失败或已取消 · 未发布完整交付"
                )
                return
            if not job["output"].exists():
                raise StitchPressError("工作进程退出，结果未保存；远端AI可能仍计费，未自动重试")
            result = json.loads(job["output"].read_bytes())
            if result["status"] != "completed":
                raise StitchPressError(result.get("error", "任务失败"))
            same = (
                job["project_id"] == self.draft.project_id
                and result["base_sha"] == self.design.content_sha
                and (
                    not job["include_image"]
                    or job["source_sha"]
                    == (sha256(self.source_image) if self.source_image else None)
                )
            )
            if not same:
                self.log.append(f"旧分支候选已保留：{job['output']}；当前母版未更改")
                return
            if job["operation"] == "analyze":
                self.ai_result.setPlainText(
                    json.dumps(result["result"], ensure_ascii=False, indent=2)
                )
                self.draft.entities[f"ai:{uuid4().hex}"] = result["result"]
            else:
                candidate = result["result"]
                self.draft.entities[f"candidate:{uuid4().hex}"] = candidate
                answer = QMessageBox.question(
                    self,
                    "采用针格候选？",
                    "采用后仍须审查白色主体、负空间与关键细节。\n"
                    "原母版和局部修订会保留；本次不会自动迁移原针格补丁。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self.adopt_design(Design.from_dict(candidate["design"]))
            self.dirty = True
            self.statusBar().showMessage("任务完成；候选/分析已保留，生产资格仍取决于母版审核")
        except Exception as exc:
            self.error(exc)

    def export(self):
        try:
            if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
                raise StitchPressError("已有后台任务运行，请先等待")
            if not self.apply_fields():
                return
            if not self.demo_mode.isChecked() and not self.draft.entities.get("freeze"):
                raise StitchPressError("正式生产前请完成当前母版审核冻结")
            parent = QFileDialog.getExistingDirectory(self, "选择导出父目录（自动新建版本目录）")
            if not parent:
                return
            folder = settings_dir() / "work" / uuid4().hex
            folder.mkdir(parents=True)
            snapshot = folder / "snapshot.stitchpress"
            atomic_write(snapshot, self.draft.encode())
            destination = Path(parent) / ("StitchPress-" + uuid4().hex[:8])
            cancel_file = folder / "cancel"
            args = [
                "export",
                str(snapshot),
                str(destination),
                "--work",
                str(folder),
                "--cancel-file",
                str(cancel_file),
            ]
            if self.demo_mode.isChecked():
                args.append("--demo")
            self.job = {
                "operation": "export",
                "cancel_file": cancel_file,
                "destination": destination,
            }
            self.process = QProcess(self)
            self.process.setProgram(worker_program())
            self.process.setArguments(worker_arguments("cli", args))
            self.process.readyReadStandardOutput.connect(self.read_progress)
            self.process.readyReadStandardError.connect(self.read_stderr)
            self.process.errorOccurred.connect(self.worker_error)
            self.process.finished.connect(self.worker_finished)
            self.process.start()
            self.log.append(f"开始导出：{destination}")
            atomic_write(
                folder / "launch.json",
                canonical_bytes(
                    {"program": self.process.program(), "arguments": self.process.arguments()}
                ),
            )
        except Exception as exc:
            self.error(exc)

    def cancel_job(self):
        if not self.process or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        if self.job["operation"] == "export":
            atomic_write(self.job["cancel_file"], b"cancel")
            self.log.append("已请求安全取消，等待当前文件写入/核验结束")
        elif self.job["operation"] == "analyze":
            self.log.append(
                "AI请求可能已提交：关闭/中断不能保证停止计费，请等待结果或超时；不自动重试"
            )
        else:
            self.process.terminate()
            self.log.append("已停止本地转换；未发布候选")

    def closeEvent(self, event):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            self.error("后台任务仍在运行，请等待结束或先安全取消")
            event.ignore()
            return
        if not self.confirm_discard():
            event.ignore()
            return
        if self.coordinator:
            self.coordinator.close()
            self.coordinator = None
        event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("StitchPress")
    app.setOrganizationName("StitchPress")
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QMainWindow, QWidget { background: #f5f3ef; color: #25392f; }
        QToolBar { spacing: 9px; padding: 9px; border-bottom: 1px solid #d5dbd3; }
        QGroupBox { border: 1px solid #d3d9cf; border-radius: 7px; margin-top: 15px;
                    padding: 12px 8px 8px; font-weight: 600; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; }
        QPushButton { background: #e5ece3; border: 1px solid #bccbb8; border-radius: 5px;
                      padding: 7px 11px; }
        QPushButton:hover { background: #d4e1d0; }
        QLineEdit, QTextEdit, QSpinBox, QComboBox, QTableWidget { background: white;
            border: 1px solid #cbd4c6; border-radius: 4px; padding: 4px; }
        QTabBar::tab { padding: 10px 18px; }
        QTabBar::tab:selected { background: #dce8d7; }
        QGraphicsView { background: #e0e3db; border: none; }
        QStatusBar { background: #e4ebe0; }
    """)
    window = Workbench()
    window.show()
    QTimer.singleShot(0, window.canvas.fit)
    return app.exec()
