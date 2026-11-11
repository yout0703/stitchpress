import copy
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QProcess, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsScene,
    QGraphicsView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.configuration import ReferenceSettings, ShowcaseSettings
from stitchpress.core.conversion import normalize_image
from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.identity import canonical_bytes, sha256
from stitchpress.core.importers import import_oxs
from stitchpress.core.layout import Fabric, physical_layout
from stitchpress.core.masks import annotation_masks
from stitchpress.core.models import Design, PatchCell, ReviewRecord, apply_patch
from stitchpress.core.render import screen_rgb
from stitchpress.core.resources import assign_symbols, library_from_assets, natural_color_key
from stitchpress.core.xsd import import_xsd
from stitchpress.platform.files import atomic_write
from stitchpress.platform.launch import worker_arguments, worker_program
from stitchpress.platform.settings import load_settings, save_secret, save_settings, settings_dir

from .ai import ImageServiceProfile, ServiceProfile
from .appearance import THEMES, apply_appearance
from .assets import merge_assets, remap_assets
from .brief import BriefFeatures
from .commerce_ui import CommerceFeatures
from .fixtures import demo_project
from .grid_item import StitchGridItem
from .help_feedback import HelpFeedbackFeatures
from .local_features import LocalFeatures, PagePreview
from .materials_ui import MaterialFeatures
from .store import Coordinator, Project
from .symbols import SymbolFeatures
from .task_history import AIPolicy, TaskHistory, context_fingerprint
from .task_ui import TaskFeatures, instance_id
from .text_settings import TextFeatures
from .workspace import build_workspace


class Canvas(QGraphicsView):
    region_selected = Signal(QRectF)
    zoom_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._start = None
        self.selection_item = None
        self._selection = None
        self._regions = []
        self._region_items = []
        self._auto_fit = True
        self.selection_color = "#86deca"
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def set_image(self, image, fit=False):
        old_rect = self.sceneRect()
        self.scene().clear()
        self.selection_item = None
        self._region_items = []
        self.scene().addPixmap(QPixmap.fromImage(ImageQt(image)))
        self.setSceneRect(QRectF(0, 0, image.width, image.height))
        if old_rect != self.sceneRect():
            self._selection = None
        self.draw_selection(self._selection)
        self.draw_regions(self._regions)
        if fit or self._auto_fit:
            self.fit()

    def set_design(self, design, highlight=None, fit=False):
        old_rect = self.sceneRect()
        self.scene().clear()
        self.selection_item = None
        self._region_items = []
        item = StitchGridItem(design, highlight)
        self.scene().addItem(item)
        self.setSceneRect(item.boundingRect())
        if old_rect != self.sceneRect():
            self._selection = None
        self.draw_selection(self._selection)
        self.draw_regions(self._regions)
        self.setToolTip("缩小时显示线色色块概览，放大后按当前视窗绘制X针；选区始终使用原针格坐标")
        if fit or self._auto_fit:
            self.fit()

    def fit(self):
        self._auto_fit = True
        if not self.sceneRect().isEmpty():
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self.zoom_changed.emit(self.transform().m11() * 100)

    def zoom(self, factor):
        self._auto_fit = False
        current = self.transform().m11()
        target = max(0.03, min(30, current * factor))
        self.scale(target / current, target / current)
        self.zoom_changed.emit(self.transform().m11() * 100)

    def actual_pixels(self):
        self._auto_fit = False
        self.resetTransform()
        self.zoom_changed.emit(100)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._auto_fit:
            self.fit()

    def wheelEvent(self, event):
        self.zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
        event.accept()

    def draw_selection(self, rect):
        if self.selection_item is not None:
            self.scene().removeItem(self.selection_item)
            self.selection_item = None
        self._selection = rect
        if rect is not None:
            pen = QPen(QColor(self.selection_color), 2)
            pen.setCosmetic(True)
            self.selection_item = self.scene().addRect(rect, pen)

    def draw_regions(self, regions):
        for item in self._region_items:
            self.scene().removeItem(item)
        self._region_items = []
        self._regions = regions
        for rect, color in regions:
            pen = QPen(QColor(color), 2, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._region_items.append(self.scene().addRect(rect, pen))

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.dragMode() == QGraphicsView.DragMode.RubberBandDrag
        ):
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
                self.draw_selection(rect)


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


class Workbench(
    LocalFeatures,
    SymbolFeatures,
    BriefFeatures,
    TaskFeatures,
    TextFeatures,
    CommerceFeatures,
    MaterialFeatures,
    HelpFeedbackFeatures,
    QMainWindow,
):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("StitchPress · 绣印工坊 · 开发试验版")
        self.resize(1440, 1024)
        self.setMinimumSize(1100, 720)
        chosen_theme = load_settings().get("appearance", {}).get("theme", "dark")
        self.theme = chosen_theme if chosen_theme in THEMES else "dark"
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
        self.set_theme(self.theme, persist=False)
        self.refresh(fit=True)
        self.init_local_features()
        self.restore_ai_state()
        self.sync_task_records()

    def build_ui(self):
        build_workspace(self, Canvas)

    def set_theme(self, name, *, persist=True):
        if name not in THEMES:
            return
        self.theme = name
        self.theme_button.setText("白天" if name == "dark" else "夜间")
        self.theme_button.setProperty(
            "iconName", "mdi6.white-balance-sunny" if name == "dark" else "mdi6.weather-night"
        )
        apply_appearance(QApplication.instance(), self, name)
        for canvas in (self.canvas, self.source_canvas):
            canvas.selection_color = THEMES[name]["accent"]
            canvas.draw_selection(canvas._selection)
        if persist:
            try:
                settings = load_settings()
                settings.setdefault("appearance", {})["theme"] = name
                save_settings(settings)
            except Exception as exc:
                self.error(exc)

    def toggle_theme(self):
        self.set_theme("light" if self.theme == "dark" else "dark")

    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        self.workbench_nav.setChecked(index == 0)
        self.ai_nav.setChecked(index == 1)
        self.export_nav.setChecked(index == 2)
        self.view_controls.setVisible(index == 0)

    def set_canvas_tool(self, pan):
        self.canvas.setDragMode(
            QGraphicsView.DragMode.ScrollHandDrag if pan else QGraphicsView.DragMode.RubberBandDrag
        )

    def toggle_source(self):
        self.source_panel.setVisible(self.compare_button.isChecked())
        self.canvas_splitter.setSizes([1, 1])
        QTimer.singleShot(0, self.source_canvas.fit)

    def toggle_inspector(self):
        visible = self.inspector.isHidden()
        self.inspector.setVisible(visible)
        self.inspector_button.blockSignals(True)
        self.inspector_button.setChecked(visible)
        self.inspector_button.blockSignals(False)
        if visible:
            self.workspace_splitter.setSizes([290, max(300, self.width() - 610), 320])

    def filter_palette(self):
        query = self.palette_search.text().strip().casefold()
        visible = 0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            match = not query or query in item.toolTip().casefold()
            self.table.setRowHidden(row, not match)
            visible += match
        self.palette_count.setText(
            f"共 {self.table.rowCount()} 色"
            if not query
            else f"找到 {visible} / {self.table.rowCount()} 色"
        )

    def clear_highlight(self):
        self.table.clearSelection()

    def update_document_state(self):
        metadata = self.draft.entities["metadata"]
        title = f"{metadata['title']} / {metadata['code']}"
        self.document_title.setText(title)
        self.document_title.setToolTip(title)
        state = "有未保存修改" if self.dirty else "已保存" if self.project_path else "示例 · 未保存"
        self.document_state.setText(state)

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
            metadata = {
                **self.draft.entities["metadata"],
                "title": self.title_edit.text(),
                "code": self.code_edit.text(),
            }
            if self.draft.entities["metadata"] != metadata or current != asdict(fabric):
                self.draft.entities["metadata"] = metadata
                self.draft.entities["fabric"] = asdict(fabric)
                self.dirty = True
                self.refresh()
            return True
        except Exception as exc:
            self.error(exc)
            return False

    def delivery_options_summary(self):
        reference = ReferenceSettings(**self.draft.entities.get("reference", {}))
        showcase = ShowcaseSettings(**self.draft.entities.get("showcase", {}))
        style = {"symbols": "黑白符号", "pale": "浅底符号", "color": "原色符号"}[reference.style]
        return (
            f"查图 {reference.paper if reference.paper != 'custom' else '自定义'} · "
            f"{'横向' if reference.landscape else '竖向'} · {reference.scale}:1 · "
            f"重叠 {reference.overlap} 格 · {style}\n"
            f"展示 {'X针格' if showcase.style == 'x' else '点图'} · "
            f"每格 {showcase.cell}px · "
            + ("厂家基础查图" if reference.factory_compatible else "自定义查图设置")
        )

    def refresh(self, fit=False):
        self._refreshing = True
        try:
            design = self.design
            fabric = Fabric(**self.draft.entities["fabric"])
            library = library_from_assets(self.draft.entities["library"], self.draft.assets)
            mapping = assign_symbols(design, library, self.draft.entities.get("mapping"))
            self.draft.entities["mapping"] = mapping
            self.color_limit.setMaximum(len(library.symbols))
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
            self.metrics.setText(
                f"{design.width} × {design.height} 格   |   {len(mapping)} 色   |   "
                f"{physical.width_cm} × {physical.height_cm} cm"
            )
            self.metrics.setToolTip(f"实际格距 {fabric.sx} × {fabric.sy} cm / 格")
            self.export_summary.setText(
                f"{metadata['title']} / {metadata['code']}\n"
                f"{design.width} × {design.height} 格 · "
                f"{sum(design.counts):,} 针 · {len(mapping)} 色\n"
                f"画心 {physical.width_cm} × {physical.height_cm} cm · "
                f"生产 {physical.canvas_px[0]} × {physical.canvas_px[1]} px · {fabric.dpi} DPI\n"
                + self.delivery_options_summary()
            )
            self.export_review.setText(
                "已记录当前母版审核" if self.draft.entities.get("freeze") else "待审查与冻结"
            )
            self.update_document_state()
            selected_items = self.table.selectedItems()
            selected_index = (
                selected_items[0].data(Qt.ItemDataRole.UserRole) if selected_items else None
            )
            self.canvas.set_design(design, highlight=selected_index, fit=fit)
            self.refresh_annotations()
            patch_index = self.patch_color.currentData()
            self.table.setRowCount(0)
            self.patch_color.clear()
            self.patch_color.addItem("留空（不绣）", -1)
            counts = design.counts
            for i in sorted(
                range(len(design.palette)),
                key=lambda index: natural_color_key(design.palette[index]),
            ):
                color = design.palette[i]
                self.patch_color.addItem(f"{color.brand} {color.code}  {color.name}", i)
                if not counts[i]:
                    continue
                row = self.table.rowCount()
                self.table.insertRow(row)
                for col, value in enumerate(
                    ("", f"{color.brand} {color.code}", f"{counts[i]:,}", "")
                ):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, i)
                    item.setToolTip(f"{color.brand} {color.code} · {color.name} · {counts[i]:,} 针")
                    if col == 0:
                        chip = QPixmap(24, 24)
                        chip.fill(QColor(*color.rgb))
                        item.setIcon(QIcon(chip))
                    if col == 2:
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                        )
                    if col == 3:
                        symbol = library.by_key[mapping[color.key]]
                        rgb = screen_rgb(symbol.image(), library.icc)
                        item.setIcon(QIcon(QPixmap.fromImage(ImageQt(rgb))))
                        item.setToolTip(f"{color.brand} {color.code} · 符号 {symbol.key}")
                    self.table.setItem(row, col, item)
                self.table.setRowHeight(row, 42)
                if i == selected_index:
                    self.table.selectRow(row)
            if patch_index is not None:
                self.patch_color.setCurrentIndex(max(0, self.patch_color.findData(patch_index)))
            self.patch_button.setEnabled(self.region is not None)
            self.patch_color.setEnabled(self.region is not None)
            self.undo_button.setEnabled(bool(self.history))
            self.symbol_button.setEnabled(bool(self.table.selectedItems()))
            self.filter_palette()
        finally:
            self._refreshing = False

    def actual_pixels(self):
        self.canvas.actual_pixels()

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
            self.restore_ai_state()
        except Exception as exc:
            self.error(exc)

    def highlight(self):
        if self._refreshing:
            return
        selected = self.table.selectedItems()
        color = selected[0].data(Qt.ItemDataRole.UserRole) if selected else None
        self.canvas.set_design(self.design, highlight=color)
        self.symbol_button.setEnabled(color is not None)
        if color is not None:
            self.patch_color.setCurrentIndex(self.patch_color.findData(color))

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
                f"{region[2] - region[0]} × {region[3] - region[1]} 格 · "
                f"已查看 {len(self.review_regions)} 处局部"
            )
            self.patch_color.setEnabled(True)
            self.patch_button.setEnabled(True)
        else:
            self.region = None
            self.region_label.setText("选区未覆盖针格，请在母版内重新框选")
            self.patch_color.setEnabled(False)
            self.patch_button.setEnabled(False)

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
            masks = annotation_masks(
                self.draft.entities.get("annotations"),
                design.width,
                design.height,
                sha256(self.source_image) if self.source_image else None,
            )
            protected = frozenset(
                (int(x), int(y)) for y, x in zip(*masks["protected"].nonzero(), strict=True)
            )
            result = apply_patch(
                design,
                base_sha=design.content_sha,
                cells=cells,
                allowed=frozenset((x, y) for y in range(y0, y1) for x in range(x0, x1)),
                capacity=len(library.symbols),
                protected=protected,
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
            self.region = None
            self.canvas.draw_selection(None)
            self.region_label.setText("拖动框选针格，检查关键局部")
            self.dirty = True
            self.refresh()

    def adopt_design(self, design):
        """Validate before changing the active revision; failed imports cannot destroy it."""
        library = library_from_assets(self.draft.entities["library"], self.draft.assets)
        mapping = assign_symbols(design, library, self.draft.entities.get("mapping"))
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
        self.region_label.setText("拖动框选针格，检查关键局部")
        self.canvas.draw_selection(None)
        self.table.clearSelection()
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
        return (not self.dirty and not self.has_pending_form()) or QMessageBox.question(
            self,
            "未保存修改",
            "放弃当前尚未保存的修改？",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) == QMessageBox.StandardButton.Discard

    def set_project(self, project, path=None, coordinator=None):
        design = Design.from_dict(project.entities["design"])
        Fabric(**project.entities["fabric"])
        library = library_from_assets(project.entities["library"], project.assets)
        assign_symbols(design, library, project.entities.get("mapping"))
        if any(
            not isinstance(project.entities.get("metadata", {}).get(k), str)
            for k in ("title", "code")
        ):
            raise StitchPressError("作品名称/客户号字段损坏")
        if self.coordinator:
            self.coordinator.close()
        if hasattr(self, "recovery"):
            self.recovery.clear()
            self._last_recovery_sha = None
        self.coordinator, self.project_path = coordinator, path
        self.draft = copy.deepcopy(project)
        self.baseline = copy.deepcopy(project)
        self.region, self.review_regions, self.history = None, [], []
        self.canvas.draw_selection(None)
        self.region_label.setText("拖动框选针格，检查关键局部")
        self.table.clearSelection()
        self.source_image = None
        self.source_canvas.scene().clear()
        self.source_canvas.hide()
        self.source_empty.show()
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
        self.restore_conversion_controls()
        self.restore_ai_state()
        self.sync_task_records()

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
            self.recovery.clear()
            self._last_recovery_sha = None
            self.update_document_state()
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
            for key in ("source", "annotations", "input_profile"):
                if old := self.draft.entities.pop(key, None):
                    self.draft.entities[f"{key}:{uuid4().hex}"] = old
            self.source_image = None
            self.source_canvas.scene().clear()
            self.source_canvas.hide()
            self.source_empty.show()
            self.source_label.setText("工程已导入，可另选对应原图；原输入已保留在修订记录")
            self.draft.assets[f"sources/{sha256(data)}{Path(path).suffix.lower()}"] = data
            self.review_regions = []
            self.dirty = True
            self.refresh(fit=True)
            self.restore_conversion_controls()
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
            existing = {
                k: v
                for k, v in self.draft.entities.get("mapping", {}).items()
                if v in library.by_key
            }
            mapping = assign_symbols(self.design, library, existing)
            self.draft.assets, aliases = merge_assets(self.draft.assets, resource.assets)
            for key in ("library", "catalog", "source_profile"):
                if key in resource.entities:
                    self.draft.entities[key] = remap_assets(resource.entities[key], aliases)
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
                self.import_source(data, Path(path).name)
            except Exception as exc:
                self.error(exc)

    def import_source(self, data, filename):
        import io

        from PIL import Image

        if len(data) > 64 * 1024**2:
            raise StitchPressError("单张原图超过64MiB项目资源上限")
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000 or getattr(image, "n_frames", 1) != 1:
                raise StitchPressError("原图须为单帧且不超过4000万像素")
            image.load()
        if self.source_image is None or sha256(data) != sha256(self.source_image):
            for key in ("source", "input_profile"):
                if old := self.draft.entities.pop(key, None):
                    self.draft.entities[f"{key}:{uuid4().hex}"] = old
        self.source_image = data
        name = f"sources/{sha256(data)}{Path(filename).suffix.lower()}"
        self.draft.assets[name] = data
        self.draft.entities["source"] = {"asset": name, "role": "unreviewed-input"}
        try:
            self.display_source(data)
            self.source_label.setText(filename + "\n直接转换仅适合干净蓝图，需人工审核")
        except StitchPressError as exc:
            self.source_canvas.scene().clear()
            self.source_canvas.hide()
            self.source_empty.show()
            self.source_label.setText(filename + "\n原件已保存，等待修正输入ICC：" + str(exc))
        self.capture_conversion_options()
        self.dirty = True
        self.update_document_state()
        self.restore_ai_state()
        self.compare_button.setChecked(True)
        self.toggle_source()

    def display_source(self, data):
        from PIL import Image

        input_profile = self.draft.entities.get("input_profile")
        normalized, _ = normalize_image(
            data, self.draft.assets[input_profile["asset"]] if input_profile else None
        )
        display = Image.new("RGBA", normalized.size, "white")
        display.alpha_composite(normalized)
        display.thumbnail((4096, 4096))
        self.source_empty.hide()
        self.source_canvas.show()
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
        snapshot = folder / "input.stitchpress"
        payload = self.draft.encode()
        atomic_write(snapshot, payload)
        job["project"] = str(snapshot)
        if self.source_image and job.get("include_image", True):
            atomic_write(folder / "source.image", self.source_image)
            job["image"] = str(folder / "source.image")
            if profile := self.draft.entities.get("input_profile"):
                atomic_write(folder / "input.icc", self.draft.assets[profile["asset"]])
                job["input_icc"] = str(folder / "input.icc")
        if operation == "convert":
            catalog = self.draft.entities.get("catalog")
            if not catalog:
                raise StitchPressError("请先导入本地线色/符号资源包，再转换客户蓝图")
            atomic_write(folder / "catalog.json", self.draft.assets[catalog["asset"]])
            job["catalog"] = str(folder / "catalog.json")
            job["catalog_brand"] = catalog.get("brand", "DMC")
            job["annotations"] = self.draft.entities.get("annotations")
            if profile := self.draft.entities.get("input_profile"):
                atomic_write(folder / "input.icc", self.draft.assets[profile["asset"]])
                job["input_icc"] = str(folder / "input.icc")
        job_path = folder / "job.json"
        atomic_write(job_path, canonical_bytes(job))
        context_sha = context_fingerprint(self, operation, job.get("include_image", True))
        task = TaskHistory(settings_dir() / "work").begin(
            folder,
            {
                "task_id": folder.name,
                "operation": operation,
                "project_id": self.draft.project_id,
                "owner": instance_id(),
                "context_sha": context_sha,
                "input_sha": sha256(payload),
                "include_image": job.get("include_image", True),
            },
            policy=AIPolicy(**self.draft.entities.get("ai_policy", {})),
            imported=self.draft.entities.get("ai_usage", {}).get("tasks", {}).values(),
        )
        if operation == "analyze":
            self.mirror_ai_task(task)
            self.checkpoint_draft()
        self.job = {
            "operation": operation,
            "output": output,
            "base_sha": job["base_sha"],
            "project_id": self.draft.project_id,
            "source_sha": sha256(self.source_image) if self.source_image else None,
            "include_image": job.get("include_image", True),
            "context_sha": context_sha,
            "folder": folder,
            **({"cancel_file": Path(job["cancel_file"])} if job.get("cancel_file") else {}),
        }
        self.process = QProcess(self)
        self.process.setProgram(worker_program())
        self.process.setArguments(worker_arguments("worker", [str(job_path)]))
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.finished.connect(self.worker_finished)
        self.process.errorOccurred.connect(self.worker_error)
        self.process.readyReadStandardError.connect(self.read_stderr)
        self.process.start()
        self.cancel_button.setEnabled(True)
        self.statusBar().showMessage("后台任务运行中；结果不会自动覆盖其他分支")

    def convert(self):
        try:
            if not self.source_image:
                raise StitchPressError("请先选择干净蓝图")
            self.capture_conversion_options()
            if brief := self.draft.entities.get("brief"):
                from stitchpress.core.brief import DesignBrief

                catalog = self.draft.entities.get("catalog")
                DesignBrief(**brief).validate_context(
                    source_sha=sha256(self.source_image),
                    catalog_revision=sha256(self.draft.assets[catalog["asset"]])
                    if catalog
                    else None,
                    capacity=self.color_limit.maximum(),
                )
                if (brief["width"], brief["height"], brief["max_colors"]) != (
                    self.grid_w.value(),
                    self.grid_h.value(),
                    self.color_limit.value(),
                ):
                    raise StitchPressError("转换参数与已确认设计要求不一致，请先更新设计要求")
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
            from stitchpress.platform.settings import load_secret

            if not load_secret(ServiceProfile(**config["ai_profile"]).credential_key):
                raise StitchPressError("请先在AI配置中保存本机密钥，尚未发送请求")
            self.capture_creative_prompt()
            prompt = self.prompt.toPlainText()
            if brief := self.draft.entities.get("brief"):
                prompt += (
                    "\n以下是用户保存的设计要求，请指出冲突和缺项，不改写为已确认事实：\n"
                    + json.dumps(brief, ensure_ascii=False)
                )
            self.start_worker(
                "analyze",
                {
                    "profile": config["ai_profile"],
                    "prompt": prompt,
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
        if self.job and self.job.get("folder"):
            TaskHistory.finish(
                self.job["folder"],
                "remote_unknown" if self.job["operation"] == "analyze" else "failed",
            )
        self.log.append(f"后台任务未启动或异常退出：{self.process.errorString()}")
        self.cancel_button.setEnabled(False)
        self.statusBar().showMessage("后台任务失败；原母版和已有完整交付未改变")

    def worker_finished(self, exit_code, exit_status):
        self.cancel_button.setEnabled(False)
        if self.preview_dialog:
            self.preview_dialog.render.setEnabled(True)
        job = self.job
        if not job:
            return
        try:
            if job["operation"] == "batch":
                state_path = job["work"] / "queue.json"
                state = (
                    json.loads(state_path.read_bytes())
                    if state_path.exists()
                    else {"status": "failed", "jobs": []}
                )
                done = sum(j["status"] == "completed" for j in state["jobs"])
                self.statusBar().showMessage(
                    f"批次 {state['status']} · 完成 {done}/{len(state['jobs'])}；可从母版菜单恢复"
                )
                return
            if job["operation"] == "export":
                from .jobs import find_receipt

                receipt = find_receipt(job["work"], job["destination"])
                success = exit_code == 0 and receipt is not None
                if success and job["project_id"] == self.draft.project_id:
                    self.draft.entities[f"export:{receipt['job_id']}"] = receipt
                    self.dirty = True
                    self.update_document_state()
                self.statusBar().showMessage(
                    "导出完成 · 待人工外观检查" if success else "导出失败或已取消 · 未发布完整交付"
                )
                return
            if not job["output"].exists():
                raise StitchPressError(
                    "AI进程退出，结果/费用未知，未自动重试"
                    if job["operation"] == "analyze"
                    else "本地工作进程退出，未取得完成结果；已有作品未改变"
                )
            result = json.loads(job["output"].read_bytes())
            if job.get("folder"):
                TaskHistory.finish(job["folder"], result["status"])
                if job["operation"] == "analyze" and job["project_id"] == self.draft.project_id:
                    self.sync_task_records()
            if result["status"] != "completed":
                raise StitchPressError(result.get("error", "任务失败"))
            if job["operation"] == "zip":
                self.statusBar().showMessage(
                    f"ZIP已完成，核对 {result['result']['files']} 个成员SHA与CRC"
                )
                return
            if job["operation"] == "symbol-card":
                self.statusBar().showMessage("符号速查卡已导出；绑定与当前任务快照一致")
                return
            if job["operation"] == "commerce-export":
                receipt = result["result"]
                if job["project_id"] == self.draft.project_id:
                    self.draft.entities[f"export:{receipt['job_id']}"] = receipt
                    self.dirty = True
                    self.update_document_state()
                self.statusBar().showMessage(
                    f"商品素材已完成 {receipt['files']} 张，复用 {receipt['reused']}；"
                    "待人工外观检查"
                )
                return
            same = (
                job["project_id"] == self.draft.project_id
                and result["base_sha"] == self.design.content_sha
                and (
                    not job["include_image"]
                    or job["source_sha"]
                    == (sha256(self.source_image) if self.source_image else None)
                )
                and job.get("context_sha")
                == context_fingerprint(self, job["operation"], job["include_image"])
            )
            if not same:
                self.log.append(f"旧分支候选已保留：{job['output']}；当前母版未更改")
                return
            if job["operation"] == "plan":
                self.statusBar().showMessage("分页计划已完成，正在查看实际布局")
                self.preview_dialog = PagePreview(self, result["result"])
                self.preview_dialog.exec()
                self.preview_dialog = None
                return
            if job["operation"] == "preview":
                if self.preview_dialog:
                    self.preview_dialog.show_result(result["result"])
                self.statusBar().showMessage("查图页预览已完成并核对针格")
                return
            if job["operation"] == "quality":
                report = result["result"]
                self.draft.entities[f"quality:{report['report_sha']}"] = report
                self.dirty = True
                self.update_document_state()
                self.show_quality(report)
                self.statusBar().showMessage("母版数字检查已完成；仍需人工审查")
                return
            if job["operation"] == "showcase-preview":
                self.show_simulation(result["result"])
                self.statusBar().showMessage("母版模拟预览已完成")
                return
            if job["operation"] == "commerce-preview":
                self.show_commerce_preview(result["result"])
                self.statusBar().showMessage("商品预览已完成；场景为示意，主体来自母版模拟")
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
                    self.draft.entities["active_conversion"] = candidate["conversion"]
            self.dirty = True
            self.update_document_state()
            self.statusBar().showMessage("任务完成；候选/分析已保留，生产资格仍取决于母版审核")
        except Exception as exc:
            self.error(exc)

    def export(self, targets=None):
        try:
            if not isinstance(targets, (list, tuple, set, frozenset)):
                targets = None
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
                "--cache",
                str(settings_dir() / "cache" / "exports"),
            ]
            if self.demo_mode.isChecked():
                args.append("--demo")
            if targets:
                args += ["--targets", ",".join(sorted(targets))]
            self.job = {
                "operation": "export",
                "cancel_file": cancel_file,
                "destination": destination,
                "work": folder,
                "project_id": self.draft.project_id,
            }
            self.process = QProcess(self)
            self.process.setProgram(worker_program())
            self.process.setArguments(worker_arguments("cli", args))
            self.process.readyReadStandardOutput.connect(self.read_progress)
            self.process.readyReadStandardError.connect(self.read_stderr)
            self.process.errorOccurred.connect(self.worker_error)
            self.process.finished.connect(self.worker_finished)
            self.process.start()
            self.cancel_button.setEnabled(True)
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
        if self.job["operation"] in {"export", "batch", "commerce-export"}:
            atomic_write(self.job["cancel_file"], b"cancel")
            self.log.append("已请求安全取消，等待当前文件写入/核验结束")
        elif self.job["operation"] == "analyze":
            self.log.append(
                "AI请求可能已提交：关闭/中断不能保证停止计费，请等待结果或超时；不自动重试"
            )
        else:
            self.process.terminate()
            self.log.append("已停止当前本地任务；已有作品保持可用")

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
        self.recovery_timer.stop()
        self.recovery.clear()
        event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("StitchPress")
    app.setOrganizationName("StitchPress")
    window = Workbench()
    window.show()
    QTimer.singleShot(0, window.canvas.fit)
    return app.exec()
