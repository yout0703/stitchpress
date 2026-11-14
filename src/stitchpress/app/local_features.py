"""Project recovery, SKU settings and local review tools for the native workbench."""

import copy
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_STORED

from PIL import Image
from PySide6.QtCore import QProcess, QRectF, QTimer
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.configuration import (
    Preset,
    ReferenceSettings,
    ShowcaseSettings,
    resize_fabric,
)
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, safe_filename, sha256
from stitchpress.core.importers import export_oxs, import_oxs
from stitchpress.core.layout import Fabric
from stitchpress.core.masks import annotation_masks
from stitchpress.core.resources import load_font
from stitchpress.platform.files import atomic_write
from stitchpress.platform.launch import worker_arguments, worker_program
from stitchpress.platform.settings import settings_dir

from .assets import merge_assets, remap_assets
from .recovery import PresetStore, RecoveryStore
from .store import Project


class SpecificationDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("尺寸、查图与预设")
        self.resize(640, 690)
        self.font_data = None
        self.font_name = None
        self.font_reset = False
        self.preset_store = PresetStore(settings_dir() / "presets")
        layout = QVBoxLayout(self)
        tabs = self.tabs = QTabWidget()
        layout.addWidget(tabs)
        fabric_page = QWidget()
        fabric_columns = QHBoxLayout(fabric_page)
        fabric_columns.setSpacing(28)
        dimensions, margins = QWidget(), QWidget()
        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.addWidget(dimensions)
        settings_layout.addWidget(margins)
        settings_layout.addStretch()
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setWidget(settings)
        settings_scroll.setMinimumWidth(440)
        fabric_columns.addWidget(settings_scroll, 1)
        from .fabric_preview import FabricPreview

        self.fabric_preview = FabricPreview()
        fabric_columns.addWidget(self.fabric_preview, 1)
        form = QFormLayout(dimensions)
        heading = QLabel("画心尺寸")
        heading.setProperty("role", "sectionLabel")
        form.addRow(heading)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(16)
        self.fabric_fields = {
            k: QLineEdit() for k in ("sx", "sy", "left", "top", "right", "bottom", "nominal_ct")
        }
        for field in self.fabric_fields.values():
            field.setMaximumWidth(110)
            field.textChanged.connect(field.setToolTip)
        self.margin_unit = QComboBox()
        for text, unit in (("厘米 cm", "cm"), ("毫米 mm", "mm"), ("实际针格数", "cells")):
            self.margin_unit.addItem(text, unit)
        self.margin_unit.setMaximumWidth(180)
        self._margin_unit = "cm"
        self.dpi = QSpinBox()
        self.dpi.setRange(72, 1200)
        self.dpi.setSuffix(" DPI")
        self.dpi.setMaximumWidth(180)
        theoretical = QPushButton("按布号计算")
        theoretical.clicked.connect(self.theoretical)
        line = QHBoxLayout()
        line.setSpacing(10)
        line.addWidget(self.fabric_fields["nominal_ct"])
        line.addWidget(theoretical)
        line.addStretch()
        form.addRow("名义布号 CT", line)
        quick = QHBoxLayout()
        for ct in (11, 14, 16, 18):
            button = QPushButton(f"{ct} CT")
            button.setToolTip("使用理论格距，不改变针数与留白")
            button.clicked.connect(lambda _, value=ct: self.choose_ct(value))
            quick.addWidget(button)
        quick.addStretch()
        form.addRow("常用布号", quick)
        self.fabric_fields["nominal_ct"].setAccessibleName("名义布号 CT")
        self.target_width = QLineEdit()
        self.target_width.setMaximumWidth(180)
        self.target_width.setPlaceholderText("画心宽度 cm")
        self.target_width.setAccessibleName("目标画心宽度 cm")
        target = QPushButton("按宽度计算")
        target.clicked.connect(self.apply_width)
        target.setToolTip("保持横纵实际格距比例，不改变针数")
        line = QHBoxLayout()
        line.setSpacing(10)
        line.addWidget(self.target_width)
        line.addWidget(target)
        line.addStretch()
        form.addRow("目标宽度 cm", line)
        line = QHBoxLayout()
        line.setSpacing(10)
        for key, title in (("sx", "横向"), ("sy", "纵向")):
            line.addWidget(QLabel(title))
            line.addWidget(self.fabric_fields[key])
            self.fabric_fields[key].setAccessibleName(title + "实际格距 cm")
        line.addStretch()
        form.addRow("实际格距 cm", line)
        self.effective_ct = QLabel()
        self.effective_ct.setWordWrap(True)
        self.effective_ct.setMinimumHeight(54)
        self.effective_ct.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        form.addRow("等效 CT", self.effective_ct)
        for key in ("sx", "sy"):
            self.fabric_fields[key].textChanged.connect(self.update_effective_ct)
        form = QFormLayout(margins)
        heading = QLabel("布边与输出")
        heading.setProperty("role", "sectionLabel")
        form.addRow(heading)
        form.addRow("留白单位", self.margin_unit)
        for items in ((("left", "左"), ("right", "右")), (("top", "上"), ("bottom", "下"))):
            line = QHBoxLayout()
            line.setSpacing(10)
            for key, title in items:
                line.addWidget(QLabel(title))
                line.addWidget(self.fabric_fields[key])
                self.fabric_fields[key].setAccessibleName(title + "留白")
            line.addStretch()
            form.addRow("四边留白", line)
        form.addRow("生产分辨率", self.dpi)
        note = QLabel(
            "名义CT与等效CT分别记录。尺寸改变不增减针数；查图分辨率在阅读图纸中单独设置。"
        )
        note.setWordWrap(True)
        form.addRow(note)
        tabs.addTab(fabric_page, "尺寸与布料")
        reference_page = QWidget()
        columns = QHBoxLayout(reference_page)
        columns.setSpacing(28)
        left_column, right_column = QWidget(), QWidget()
        columns.addWidget(left_column, 1)
        columns.addWidget(right_column, 1)
        form = QFormLayout(left_column)
        self.paper = QComboBox()
        self.paper.addItems(["A4", "A3", "A2", "custom", "whole"])
        self.paper.setItemText(3, "自定义")
        self.paper.setItemData(3, "custom")
        self.paper.setItemText(4, "整张查图（按母版尺寸）")
        self.custom_w, self.custom_h = QLineEdit(), QLineEdit()
        self.landscape = QCheckBox("横向（交换宽高）")
        self.scale = QLineEdit()
        self.scale.setMaximumWidth(140)
        self.overlap = QSpinBox()
        self.overlap.setRange(0, 30)
        self.style = QComboBox()
        for text, value in (
            ("符号库原色", "symbols"),
            ("浅色底＋符号", "pale"),
            ("原色底＋符号", "color"),
        ):
            self.style.addItem(text, value)
        self.reading_dpi = QSpinBox()
        self.reading_dpi.setRange(0, 1200)
        self.reading_dpi.setSpecialValueText("沿用生产DPI")
        self.reading_cell = QLineEdit()
        self.reading_cell.setPlaceholderText("自动：格距 × 倍率")
        self.reading_cell.setMaximumWidth(200)
        self.reading_cell.setToolTip("留空沿用实际格距 × 阅读倍率；填写时为 0.5–10 mm")
        self.reading_margins = [QLineEdit() for _ in range(4)]

        def section(title):
            heading = QLabel(title)
            heading.setProperty("role", "sectionLabel")
            form.addRow(heading)

        def pair(items):
            line = QHBoxLayout()
            line.setSpacing(10)
            for title, field in items:
                line.addWidget(QLabel(title))
                field.setMaximumWidth(100)
                field.setAccessibleName(title)
                line.addWidget(field)
            line.addStretch()
            return line

        section("页面与方向")
        form.addRow("纸张", self.paper)
        form.addRow("方向", self.landscape)
        form.addRow("自定义尺寸 cm", pair((("宽", self.custom_w), ("高", self.custom_h))))
        section("格宽与清晰度")
        form.addRow("查图 DPI", self.reading_dpi)
        form.addRow("每格 mm · 0.5–10", self.reading_cell)
        form.addRow("阅读倍率 · 1–4", self.scale)
        form.addRow("重叠格数", self.overlap)
        form = QFormLayout(right_column)
        section("页边距与样式")
        for i in range(4):
            self.reading_margins[i].setPlaceholderText("默认")
            self.reading_margins[i].setToolTip("5–50 mm；留空沿用基础页边距")
        form.addRow(
            "页边距 mm", pair((("左", self.reading_margins[0]), ("右", self.reading_margins[2])))
        )
        form.addRow("", pair((("上", self.reading_margins[1]), ("下", self.reading_margins[3]))))
        form.addRow("查图样式", self.style)
        self.minor_grid = QCheckBox("显示逐格细线（十格线始终显示）")
        form.addRow("网格", self.minor_grid)
        section("输出字体")
        font_button = QPushButton("导入字体…")
        font_button.clicked.connect(self.choose_font)
        saved_font = owner.draft.entities.get("output_font") or {}
        self.font_label = QLabel(
            f"作品自定义字体：{saved_font.get('name', '已导入字体')}"
            if saved_font
            else "内置 Noto Sans CJK SC · 支持常用中文和英文，无需安装系统字体"
        )
        self.font_label.setWordWrap(True)
        font_actions = QHBoxLayout()
        font_actions.setSpacing(12)
        font_actions.addWidget(font_button)
        self.reset_font_button = QPushButton("恢复内置字体")
        self.reset_font_button.clicked.connect(self.reset_output_font)
        font_actions.addWidget(self.reset_font_button)
        font_actions.addStretch()
        form.addRow(font_actions)
        form.addRow(self.font_label)
        note = QLabel("页面与格宽设置只影响阅读图纸；公共输出字体也用于生产图和符号表。")
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        form.addRow(note)
        tabs.addTab(reference_page, "查图与字体")
        showcase_page = QWidget()
        showcase_form = QFormLayout(showcase_page)
        self.showcase_style = QComboBox()
        self.showcase_style.addItem("X 针格模拟（交付默认）", "x")
        self.showcase_style.addItem("点图（展示用途）", "dot")
        self.showcase_cell = QSpinBox()
        self.showcase_cell.setRange(6, 36)
        saved_showcase = ShowcaseSettings(**owner.draft.entities.get("showcase", {}))
        self.showcase_style.setCurrentIndex(self.showcase_style.findData(saved_showcase.style))
        self.showcase_cell.setValue(saved_showcase.cell)
        showcase_form.addRow("模拟样式", self.showcase_style)
        showcase_form.addRow("每格像素（不改变针数）", self.showcase_cell)
        note = QLabel(
            "模拟来自当前母版，保持严格3:4，只扩白边。点图用于展示，不替代生产TIFF。保存后可在交付页预览。"
        )
        note.setWordWrap(True)
        showcase_form.addRow(note)
        tabs.addTab(showcase_page, "展示模拟")
        presets_page = QWidget()
        pl = QVBoxLayout(presets_page)
        self.presets = QComboBox()
        pl.addWidget(self.presets)
        for text, callback in (
            ("将所选预设填入本窗口", self.load_preset),
            ("当前参数另存为新预设…", self.save_preset),
            ("移除所选预设（历史保留）", self.hide_preset),
            ("恢复 0.181cm 基础参数", self.reset),
        ):
            item = QPushButton(text)
            item.clicked.connect(callback)
            pl.addWidget(item)
        self.preset_notice = QLabel(
            "预设仅包含尺寸与查图设置，不包含密钥、货号或客户资源。应用后仍需点击保存。"
        )
        self.preset_notice.setWordWrap(True)
        pl.addWidget(self.preset_notice)
        pl.addStretch()
        tabs.addTab(presets_page, "预设")
        self.fill(
            Fabric(**owner.draft.entities["fabric"]),
            ReferenceSettings(**owner.draft.entities.get("reference", {})),
        )
        for field in self.fabric_fields.values():
            field.textChanged.connect(self.update_fabric_preview)
        self.dpi.valueChanged.connect(self.update_fabric_preview)
        self.update_fabric_preview()
        self.reload_presets()
        self.margin_unit.currentIndexChanged.connect(self.change_margin_unit)
        self.paper.currentIndexChanged.connect(self.update_paper_controls)
        self.update_paper_controls()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose_ct(self, ct):
        self.fabric_fields["nominal_ct"].setText(str(ct))
        self.theoretical()

    def update_fabric_preview(self):
        try:
            self.fabric_preview.refresh(
                self.owner.design.width, self.owner.design.height, self.fabric_values()
            )
        except Exception as exc:
            self.fabric_preview.invalid(exc)

    def update_effective_ct(self):
        from decimal import Decimal, InvalidOperation

        try:
            values = [Decimal(self.fabric_fields[key].text()) for key in ("sx", "sy")]
            if any(not value.is_finite() or value <= 0 for value in values):
                raise ValueError()
            self.effective_ct.setText(
                f"横向 {Decimal('2.54') / values[0]:.3f} CT\n"
                f"纵向 {Decimal('2.54') / values[1]:.3f} CT"
            )
        except (InvalidOperation, ValueError):
            self.effective_ct.setText("请输入有效的正格距")

    def fill(self, fabric, reference):
        self.fabric_source, self.fabric_dpi = fabric.source, fabric.dpi
        for key, field in self.fabric_fields.items():
            field.setText(getattr(fabric, key))
            field.setCursorPosition(0)
            field.setToolTip(field.text())
        self.margin_unit.blockSignals(True)
        self.margin_unit.setCurrentIndex(self.margin_unit.findData(fabric.margin_unit))
        self.margin_unit.blockSignals(False)
        self._margin_unit = fabric.margin_unit
        self.dpi.setValue(fabric.dpi)
        self._margin_baseline = {}
        from stitchpress.core.configuration import margin_value

        for key in ("left", "right", "top", "bottom"):
            pitch = fabric.sx if key in {"left", "right"} else fabric.sy
            display = margin_value(getattr(fabric, key), fabric.margin_unit, pitch, to_cm=False)
            self.fabric_fields[key].setText(display)
            self.fabric_fields[key].setCursorPosition(0)
            self._margin_baseline[key] = (display, getattr(fabric, key), pitch)
        self.paper.setCurrentIndex(["A4", "A3", "A2", "custom", "whole"].index(reference.paper))
        self.custom_w.setText(reference.custom_width_cm)
        self.custom_h.setText(reference.custom_height_cm)
        self.landscape.setChecked(reference.landscape)
        self.scale.setText(reference.scale)
        self.overlap.setValue(reference.overlap)
        self.style.setCurrentIndex(self.style.findData(reference.style))
        self.minor_grid.setChecked(reference.minor_grid)
        self.reading_dpi.setValue(reference.dpi or 0)
        self.reading_cell.setText(reference.cell_mm or "")
        for field, value in zip(
            self.reading_margins, reference.margins_mm or ("",) * 4, strict=True
        ):
            field.setText(value)
        self.update_paper_controls()
        self.update_fabric_preview()

    def update_paper_controls(self):
        whole = self.paper.currentIndex() == 4
        self.custom_w.setEnabled(self.paper.currentIndex() == 3)
        self.custom_h.setEnabled(self.paper.currentIndex() == 3)
        self.overlap.setEnabled(not whole)
        self.landscape.setEnabled(not whole)
        if whole:
            self.overlap.setValue(0)
            self.landscape.setChecked(False)

    def fabric_values(self):
        from stitchpress.core.configuration import margin_value

        fields = {k: w.text().strip() for k, w in self.fabric_fields.items()}
        for key in ("left", "right", "top", "bottom"):
            text, original_cm, original_pitch = self._margin_baseline.get(key, (None, None, None))
            pitch = fields["sx"] if key in {"left", "right"} else fields["sy"]
            fields[key] = (
                original_cm
                if fields[key] == text and (self._margin_unit != "cells" or pitch == original_pitch)
                else margin_value(
                    fields[key],
                    self._margin_unit,
                    pitch,
                    to_cm=True,
                )
            )
        return Fabric(
            **fields,
            source=self.fabric_source,
            dpi=self.dpi.value(),
            margin_unit=self._margin_unit,
        )

    def values(self):
        fabric = self.fabric_values()
        reference = ReferenceSettings(
            paper=["A4", "A3", "A2", "custom", "whole"][self.paper.currentIndex()],
            custom_width_cm=self.custom_w.text().strip(),
            custom_height_cm=self.custom_h.text().strip(),
            landscape=self.landscape.isChecked(),
            scale=self.scale.text().strip(),
            overlap=self.overlap.value(),
            style=self.style.currentData(),
            minor_grid=self.minor_grid.isChecked(),
            dpi=self.reading_dpi.value() or None,
            cell_mm=self.reading_cell.text().strip() or None,
            margins_mm=tuple(w.text().strip() for w in self.reading_margins)
            if any(w.text().strip() for w in self.reading_margins)
            else None,
        )
        return fabric, reference

    def change_margin_unit(self):
        previous = self._margin_unit
        try:
            fabric, reference = self.values()
            from dataclasses import replace

            self.fill(replace(fabric, margin_unit=self.margin_unit.currentData()), reference)
        except Exception as exc:
            self.margin_unit.blockSignals(True)
            self.margin_unit.setCurrentIndex(self.margin_unit.findData(previous))
            self.margin_unit.blockSignals(False)
            QMessageBox.warning(self, "单位未切换", str(exc))

    def validate_accept(self):
        try:
            self.values()
            self.showcase_values()
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "参数未保存", str(exc))

    def showcase_values(self):
        return ShowcaseSettings(
            style=self.showcase_style.currentData(), cell=self.showcase_cell.value()
        )

    def theoretical(self):
        try:
            theoretical = Fabric.theoretical(self.fabric_fields["nominal_ct"].text())
            for key in ("sx", "sy"):
                self.fabric_fields[key].setText(getattr(theoretical, key))
                self.fabric_fields[key].setCursorPosition(0)
                self.fabric_fields[key].setToolTip(self.fabric_fields[key].text())
            self.fabric_source = theoretical.source
        except Exception as exc:
            QMessageBox.warning(self, "CT 无效", str(exc))

    def apply_width(self):
        try:
            fabric, _ = self.values()
            resized = resize_fabric(
                fabric, self.owner.design.width, target_width_cm=self.target_width.text()
            )
            for key in ("sx", "sy"):
                self.fabric_fields[key].setText(getattr(resized, key))
                self.fabric_fields[key].setCursorPosition(0)
            self.fabric_source = resized.source
        except Exception as exc:
            QMessageBox.warning(self, "尺寸未应用", str(exc))

    def choose_font(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择有权使用并随作品保存的字体", "", "Fonts (*.ttf *.otf)"
        )
        if path:
            try:
                data = Path(path).read_bytes()
                load_font(39, data)
                self.font_data, self.font_name = data, Path(path).name
                self.font_reset = False
                self.font_label.setText(f"将随作品保存：{self.font_name}")
            except Exception as exc:
                QMessageBox.warning(self, "字体未导入", str(exc))

    def reset_output_font(self):
        self.font_data = self.font_name = None
        self.font_reset = bool(self.owner.draft.entities.get("output_font"))
        self.font_label.setText("内置 Noto Sans CJK SC · 支持常用中文和英文，无需安装系统字体")

    def reload_presets(self):
        presets, errors = self.preset_store.list()
        self.presets.clear()
        for preset in presets:
            self.presets.addItem(f"{preset.name} · {preset.revision[:8]}", preset)
        if errors:
            self.preset_notice.setText("有损坏预设未载入：\n" + "\n".join(errors[:3]))

    def load_preset(self):
        preset = self.presets.currentData()
        if preset:
            self.fill(Fabric(**preset.fabric), ReferenceSettings(**preset.reference))

    def save_preset(self):
        try:
            fabric, reference = self.values()
            name, accepted = QInputDialog.getText(self, "保存新预设", "预设名称")
            if accepted and name.strip():
                self.preset_store.save(Preset(name.strip(), asdict(fabric), asdict(reference)))
                self.reload_presets()
        except Exception as exc:
            QMessageBox.warning(self, "预设未保存", str(exc))

    def reset(self):
        self.fill(Fabric(), ReferenceSettings())

    def hide_preset(self):
        try:
            if preset := self.presets.currentData():
                self.preset_store.hide(preset)
                self.reload_presets()
        except Exception as exc:
            QMessageBox.warning(self, "预设未移除", str(exc))


class PagePreview(QDialog):
    def __init__(self, owner, result):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("查图分页与原像素预览")
        self.resize(980, 820)
        layout = QVBoxLayout(self)
        self.description = QLabel(
            f"{result['page_count']} 页 · 每页 "
            f"{result['paper_px'][0]} × {result['paper_px'][1]} px · "
            f"整包 {result['files']} 件\n"
            f"保守工作空间估算 {result['estimated_workspace_bytes'] / 1024**3:.2f} GiB；"
            "预览为当前配置的实际查图。"
        )
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        row = QHBoxLayout()
        self.page = QComboBox()
        for page in result["plan"]["pages"]:
            x0, y0, x1, y1 = page["core"]
            self.page.addItem(
                f"第 {page['number']} 页 · 列 {x0 + 1}–{x1} / 行 {y0 + 1}–{y1}", page["number"]
            )
        row.addWidget(self.page, 1)
        self.render = QPushButton("生成此页预览")
        self.render.clicked.connect(self.request)
        row.addWidget(self.render)
        fit = QPushButton("适应")
        fit.clicked.connect(lambda: self.canvas.fit())
        row.addWidget(fit)
        actual = QPushButton("100% 像素")
        actual.clicked.connect(lambda: self.canvas.actual_pixels())
        row.addWidget(actual)
        layout.addLayout(row)
        self.canvas = type(owner.canvas)()
        layout.addWidget(self.canvas, 1)
        self.state = QLabel("先选择页码，再生成预览；不会导出或覆盖交付。")
        layout.addWidget(self.state)

    def request(self):
        try:
            self.owner.start_worker(
                "preview", {"page_number": self.page.currentData(), "include_image": False}
            )
            self.render.setEnabled(False)
            self.state.setText("正在后台绘制并核对针格…")
        except Exception as exc:
            self.owner.error(exc)

    def show_result(self, result):
        with Image.open(result["preview"]) as image:
            self.canvas.set_image(image.convert("RGB"), fit=True)
        self.render.setEnabled(True)
        self.state.setText(
            f"第 {result['page_number']} 页 · {result['checks']['stitches']:,} 针 · "
            "已核对针格；显示为 ICC 转换后的屏幕预览"
        )


class LocalFeatures:
    def conversion_options(self):
        from stitchpress.core.conversion import ConversionOptions

        return asdict(
            ConversionOptions(
                self.grid_w.value(),
                self.grid_h.value(),
                self.color_limit.value(),
                alpha_mode="threshold" if self.alpha.currentIndex() == 0 else "composite",
            )
        )

    def capture_conversion_options(self):
        if self._refreshing:
            return
        options = self.conversion_options()
        if self.draft.entities.get("conversion_options") != options:
            self.draft.entities["conversion_options"] = options
            self.dirty = True
            self.update_document_state()

    def restore_conversion_controls(self):
        options = (
            self.draft.entities.get("conversion_options")
            or self.draft.entities.get("active_conversion", {}).get("options")
            or {
                "width": self.design.width,
                "height": self.design.height,
                "max_colors": sum(n > 0 for n in self.design.counts),
                "alpha_mode": "threshold",
            }
        )
        previous = self._refreshing
        self._refreshing = True
        try:
            for widget, key in (
                (self.grid_w, "width"),
                (self.grid_h, "height"),
                (self.color_limit, "max_colors"),
            ):
                widget.setValue(max(1, options[key]))
            self.alpha.setCurrentIndex(0 if options["alpha_mode"] == "threshold" else 1)
        finally:
            self._refreshing = previous

    def batch_export(self):
        from PySide6.QtCore import QProcess

        try:
            if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
                raise StitchPressError("已有任务运行，请等待或安全取消")
            paths, _ = QFileDialog.getOpenFileNames(
                self, "选择已保存作品批量导出", "", "StitchPress (*.stitchpress)"
            )
            if not paths:
                return
            parent = QFileDialog.getExistingDirectory(
                self, "选择批次交付父目录；每款新建独立版本目录"
            )
            if not parent:
                return
            folder = settings_dir() / "work" / ("batch-" + uuid4().hex)
            items = []
            for i, path in enumerate(paths, 1):
                project = Project.decode(Path(path).read_bytes())
                code = project.entities["metadata"].get("code") or Path(path).stem
                destination = Path(parent) / f"{i:02d}-{safe_filename(code)}-{folder.name[-8:]}"
                items.append(
                    {
                        "project": path,
                        "destination": str(destination),
                        "demo": self.demo_mode.isChecked(),
                    }
                )
            recipe = folder / "batch.json"
            atomic_write(recipe, canonical_bytes({"schema_version": 1, "items": items}))
            self.launch_batch(recipe, folder)
        except Exception as exc:
            self.error(exc)

    def launch_batch(self, recipe, folder, *, resume=False):
        from PySide6.QtCore import QProcess

        from stitchpress.platform.launch import worker_arguments, worker_program

        cancel_file = folder / "cancel"
        if resume:
            cancel_file.unlink(missing_ok=True)
        args = [
            "batch-export",
            str(recipe),
            "--work",
            str(folder),
            "--cache",
            str(settings_dir() / "cache" / "exports"),
            "--cancel-file",
            str(cancel_file),
        ]
        if resume:
            args.append("--resume")
        self.job = {"operation": "batch", "work": folder, "cancel_file": cancel_file}
        self.process = QProcess(self)
        self.process.setProgram(worker_program())
        self.process.setArguments(worker_arguments("cli", args))
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.readyReadStandardError.connect(self.read_stderr)
        self.process.errorOccurred.connect(self.worker_error)
        self.process.finished.connect(self.worker_finished)
        self.process.start()
        self.cancel_button.setEnabled(True)
        self.show_page(2)
        self.statusBar().showMessage("批次在后台运行；每款使用冻结输入，不覆盖已存在交付")

    def resume_batch(self):
        from PySide6.QtCore import QProcess

        try:
            if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
                raise StitchPressError("已有任务运行")
            states = sorted(
                (settings_dir() / "work").glob("batch-*/queue.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not states:
                return self.statusBar().showMessage("没有可恢复的本机批次")
            labels = []
            for path in states:
                data = json.loads(path.read_bytes())
                done = sum(j["status"] == "completed" for j in data["jobs"])
                labels.append(f"{path.parent.name} · {data['status']} · {done}/{len(data['jobs'])}")
            chosen, accepted = QInputDialog.getItem(
                self,
                "恢复本机批次",
                "按原冻结输入恢复；已完成文件只核验，失败项复用缓存重做",
                labels,
                0,
                False,
            )
            if accepted:
                folder = states[labels.index(chosen)].parent
                self.launch_batch(folder / "batch.json", folder, resume=True)
        except Exception as exc:
            self.error(exc)

    def delivery_review(self):
        from datetime import datetime

        from .jobs import verify_delivery

        records = [
            (k, v)
            for k, v in self.draft.entities.items()
            if k.startswith("export:") and v.get("status") == "completed"
        ]
        if not records:
            return self.statusBar().showMessage("请先完成一次当前作品导出，再登记对应交付的验收")
        labels = [
            f"{v['job_id'][:8]} · {v['snapshot']['code']} · {v['files']}件" for _, v in records
        ]
        chosen, accepted = QInputDialog.getItem(
            self, "选择交付快照", "各项验收独立记录，不能用数字通过代替试印/洗水", labels, 0, False
        )
        if not accepted:
            return
        _, record = records[labels.index(chosen)]
        directory = Path(record["destination"])
        try:
            verify_delivery(directory, record)
            dialog = QDialog(self)
            dialog.setWindowTitle("登记交付验收")
            form = QFormLayout(dialog)
            reviewer, notes = QLineEdit(), QLineEdit()
            kind, result = QComboBox(), QComboBox()
            for name, value in (
                ("人工外观", "visual"),
                ("客户交付", "customer_delivery"),
                ("工厂试印", "factory_print"),
                ("洗水/熨烫", "wash"),
            ):
                kind.addItem(name, value)
            result.addItems(["待验证", "通过", "未通过"])
            form.addRow("审核人", reviewer)
            form.addRow("检查类型", kind)
            form.addRow("结果", result)
            form.addRow("设备/说明", notes)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if not reviewer.text().strip():
                raise StitchPressError("验收记录需要具名审核人")
            self.draft.entities[f"delivery-review:{uuid4().hex}"] = {
                "job_id": record["job_id"],
                "input_fingerprint": record["input_fingerprint"],
                "artifacts": {a["name"]: a["sha256"] for a in record["artifacts"]},
                "reviewer": reviewer.text().strip(),
                "kind": kind.currentData(),
                "result": ["pending", "passed", "failed"][result.currentIndex()],
                "notes": notes.text(),
                "created_at": datetime.now(UTC).isoformat(),
            }
            self.dirty = True
            self.update_document_state()
            self.statusBar().showMessage("验收已登记到此交付快照；其他验收项目和其他版本状态不变")
        except Exception as exc:
            self.error(exc)

    def zip_delivery(self):
        try:
            records = [
                v
                for k, v in self.draft.entities.items()
                if k.startswith("export:") and v.get("status") == "completed"
            ]
            if not records:
                raise StitchPressError("请先完成当前作品的交付导出")
            labels = [
                f"{v['job_id'][:8]} · {v['snapshot']['code']} · {v['files']}件" for v in records
            ]
            chosen, accepted = QInputDialog.getItem(
                self,
                "压缩已核验交付",
                "ZIP保存在交付目录之外，成员SHA与当前回执一致",
                labels,
                0,
                False,
            )
            if not accepted:
                return
            record = records[labels.index(chosen)]
            path, _ = QFileDialog.getSaveFileName(self, "另存ZIP版本", "交付.zip", "ZIP (*.zip)")
            if not path:
                return
            self.start_worker(
                "zip",
                {
                    "receipt": record,
                    "directory": record["destination"],
                    "target": str(Path(path).with_suffix(".zip")),
                    "include_image": False,
                },
            )
        except Exception as exc:
            self.error(exc)

    def pending_form(self):
        return {
            "title": self.title_edit.text(),
            "code": self.code_edit.text(),
            "sx": self.sx.text(),
            "sy": self.sy.text(),
            **{k: w.text() for k, w in self.margins.items()},
        }

    def has_pending_form(self):
        current = {
            **{k: self.draft.entities["metadata"][k] for k in ("title", "code")},
            **{k: self.draft.entities["fabric"][k] for k in ("sx", "sy", *self.margins)},
        }
        return self.pending_form() != current

    def init_local_features(self):
        self.recovery = RecoveryStore(settings_dir() / "recovery")
        self._last_recovery_sha = None
        self._recovery_process = None
        self.preview_dialog = None
        self.recovery_timer = QTimer(self)
        self.recovery_timer.timeout.connect(self.checkpoint_draft)
        self.recovery_timer.start(20_000)
        if records := self.recovery.candidates():
            self.statusBar().showMessage(
                f"发现 {len(records)} 份可恢复草稿；可在「文件 → 恢复未保存草稿」中恢复。"
            )

    def checkpoint_draft(self):
        form = self.pending_form()
        if not self.dirty and not self.has_pending_form():
            return
        try:
            fingerprint = digest({"entities": self.draft.entities, "form": form})
            if fingerprint == self._last_recovery_sha:
                return
            draft = copy.deepcopy(self.draft)
            draft.entities["recovery_form"] = form
            if sum(map(len, draft.assets.values())) < 8 * 1024**2:
                self.recovery.checkpoint(draft, self.project_path)
                self._last_recovery_sha = fingerprint
            else:
                self.start_recovery_worker(draft, fingerprint)
        except Exception as exc:
            self.statusBar().showMessage(f"草稿恢复副本未更新：{exc}；请手动保存")

    def start_recovery_worker(self, draft, fingerprint):
        if self._recovery_process is not None:
            return  # One in flight; a later timer captures subsequent edits.
        folder = settings_dir() / "recovery-work" / uuid4().hex
        folder.mkdir(parents=True)
        snapshot = folder / "input.stitchpress"
        atomic_write(snapshot, draft.encode(compression=ZIP_STORED))
        output = folder / "result.json"
        job = folder / "job.json"
        atomic_write(
            job,
            canonical_bytes(
                dict(
                    operation="recovery",
                    project=str(snapshot),
                    output=str(output),
                    original_path=str(self.project_path) if self.project_path else None,
                )
            ),
        )
        process = QProcess(self)
        self._recovery_process = process
        process.setProgram(worker_program())
        process.setArguments(worker_arguments("worker", [str(job)]))

        def finished(code, *_):
            try:
                if self._recovery_process is not process:
                    return
                self._recovery_process = None
                report = json.loads(output.read_bytes())
                if code or report["status"] != "completed":
                    raise StitchPressError(report.get("error", "后台草稿未完成"))
                result = report["result"]
                self.recovery.publish(Path(result["candidate"]).read_bytes(), result["receipt"])
                self._last_recovery_sha = fingerprint
            except Exception as exc:
                self.statusBar().showMessage(f"草稿恢复副本未更新：{exc}；请手动保存")
            finally:
                import shutil

                shutil.rmtree(folder, ignore_errors=True)
                process.deleteLater()

        def failed(error):
            if error == QProcess.ProcessError.FailedToStart:
                finished(2)

        process.finished.connect(finished)
        process.errorOccurred.connect(failed)
        process.start()

    def stop_recovery_worker(self):
        process = getattr(self, "_recovery_process", None)
        if process is not None:
            self._recovery_process = None
            process.kill()
            # Drain finished while the owning window and QProcess still exist.
            # Otherwise parent destruction can race the queued cleanup callback.
            process.waitForFinished(1500)

    def recover_draft(self):
        records = self.recovery.candidates()
        if not records:
            return self.statusBar().showMessage("没有其他会话留下的可恢复草稿")
        labels = [
            f"{r.get('title', '未命名')} · {r.get('created_at', '')} · {r['path'].stem[:8]}"
            for r in records
        ]
        chosen, accepted = QInputDialog.getItem(
            self, "恢复未保存草稿", "恢复后作为未保存副本打开，原作品不被覆盖", labels, 0, False
        )
        if accepted and self.confirm_discard():
            try:
                draft = self.recovery.restore(records[labels.index(chosen)])
                pending = draft.entities.pop("recovery_form", {})
                self.set_project(draft)
                for key, widget in {
                    "title": self.title_edit,
                    "code": self.code_edit,
                    "sx": self.sx,
                    "sy": self.sy,
                    **self.margins,
                }.items():
                    if key in pending:
                        widget.setText(pending[key])
                if forms := self.draft.entities.pop("trial_pending_forms", None):
                    from .license_ui import restore_pending_forms

                    restore_pending_forms(self, forms)
                self.dirty = True
                self.update_document_state()
                self.statusBar().showMessage("已恢复草稿副本，请核对后另存；原始恢复记录保留")
            except Exception as exc:
                self.error(exc)

    def specification_settings(self):
        if not self.apply_fields():
            return
        dialog = SpecificationDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            fabric, reference = dialog.values()
            before = {
                k: copy.deepcopy(self.draft.entities.get(k))
                for k in ("fabric", "reference", "output_font", "showcase")
            }
            self.draft.entities["fabric"] = asdict(fabric)
            self.draft.entities["reference"] = reference.to_dict()
            self.draft.entities["showcase"] = dialog.showcase_values().to_dict()
            if dialog.font_reset:
                self.draft.entities.pop("output_font", None)
            if dialog.font_data:
                asset = f"resources/fonts/{sha256(dialog.font_data)}.font"
                self.draft.assets[asset] = dialog.font_data
                self.draft.entities["output_font"] = {
                    "asset": asset,
                    "sha256": sha256(dialog.font_data),
                    "name": dialog.font_name,
                }
            self.draft.entities[f"profile:{uuid4().hex}"] = {
                "before": before,
                "after": {k: copy.deepcopy(self.draft.entities.get(k)) for k in before},
            }
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def restore_version(self):
        if not self.coordinator:
            return self.statusBar().showMessage("请先保存作品；保存后可从此处恢复旧提交")
        paths = self.coordinator.versions()
        if not paths:
            return self.statusBar().showMessage("还没有旧提交；后续保存会保留上一个完整版本")
        labels = [
            f"提交 {p.stem.split('-', 1)[0].lstrip('0') or '0'} · "
            + datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            for p in paths
        ]
        chosen, accepted = QInputDialog.getItem(
            self,
            "恢复保存历史",
            "选择旧提交；恢复为当前草稿，仍需保存，不覆盖旧版本",
            labels,
            0,
            False,
        )
        if not accepted or not self.confirm_discard():
            return
        try:
            restored = Project.decode(paths[labels.index(chosen)].read_bytes())
            if restored.project_id != self.draft.project_id:
                raise StitchPressError("旧提交不属于当前作品")
            active_keys = (
                "design",
                "mapping",
                "freeze",
                "metadata",
                "fabric",
                "reference",
                "showcase",
                "conversion_options",
                "active_conversion",
                "source",
                "catalog",
                "library",
                "source_profile",
                "output_font",
                "input_profile",
                "annotations",
                "brief",
                "creative_prompt",
                "ai_policy",
                "text_layers",
                "commerce",
                "commerce_targets",
                "material_rules",
                "post",
                "post_review",
                "pattern_input",
                "manual_mapping",
                "mapping_policy",
            )
            current = {k: copy.deepcopy(self.draft.entities.get(k)) for k in active_keys}
            updated = copy.deepcopy(self.draft)
            for key in active_keys:
                updated.entities.pop(key, None)
                if key in restored.entities:
                    updated.entities[key] = restored.entities[key]
            updated.assets, aliases = merge_assets(updated.assets, restored.assets)
            for key in active_keys:
                if key in restored.entities:
                    updated.entities[key] = remap_assets(restored.entities[key], aliases)
            updated.entities[f"restore:{uuid4().hex}"] = {
                "from_commit": restored.commit_seq,
                "before": current,
            }
            # Validation completes before adopting anything; do not release the active writer.
            # encode already performs a full read-back. This is validation only;
            # compressing a large customer library here blocks the desktop.
            updated.encode(compression=ZIP_STORED)
            self.draft = updated
            self.reference_project = None
            self._reference_path = None
            for panel in getattr(self, "reference_panels", ()):
                panel.project_id = None
            self.source_image = None
            self.source_canvas.scene().clear()
            self.source_canvas.hide()
            self.source_empty.show()
            if source := updated.entities.get("source"):
                self.source_image = updated.assets[source["asset"]]
                self.display_source(self.source_image)
            self.region, self.review_regions, self.history = None, [], []
            self.canvas.draw_selection(None)
            self.table.clearSelection()
            self.dirty = True
            self.refresh(fit=True)
            self.restore_conversion_controls()
            self.restore_ai_state()
            self.sync_task_records()
        except Exception as exc:
            self.error(exc)

    def export_oxs_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出当前全针母版（非XSD）", "当前母版.oxs", "Open Cross Stitch (*.oxs)"
        )
        if path:
            try:
                target = Path(path).with_suffix(".oxs")
                if target.exists():
                    raise StitchPressError("目标已存在，请使用新文件名保留旧版本")
                payload = export_oxs(self.design)
                actual = import_oxs(payload)
                if actual.labels != self.design.labels or [
                    (c.brand, c.code, c.rgb) for c in actual.palette
                ] != [(c.brand, c.code, c.rgb) for c in self.design.palette]:
                    raise StitchPressError("OXS逐针/色板反读不符")
                atomic_write(target, payload)
                self.statusBar().showMessage("全针OXS已导出并反读核对；目标软件互操作仍待验证")
            except Exception as exc:
                self.error(exc)

    def plan_preview(self):
        try:
            if self.apply_fields():
                self.start_worker("plan", {"include_image": False})
        except Exception as exc:
            self.error(exc)

    def inspect_master(self):
        try:
            self.start_worker("quality", {"include_image": False})
        except Exception as exc:
            self.error(exc)

    def preview_simulation(self):
        try:
            self.start_worker("showcase-preview", {"include_image": False})
        except Exception as exc:
            self.error(exc)

    def show_simulation(self, result):
        dialog = QDialog(self)
        dialog.setWindowTitle("当前母版展示模拟")
        dialog.resize(800, 780)
        layout = QVBoxLayout(dialog)
        style = "X 针格" if result["style"] == "x" else "点图"
        layout.addWidget(
            QLabel(f"{style} · {result['size'][0]} × {result['size'][1]} px · 非实绣照片")
        )
        canvas = type(self.canvas)()
        with Image.open(result["preview"]) as image:
            canvas.set_image(image.convert("RGB"), fit=True)
        layout.addWidget(canvas, 1)
        fit = QPushButton("适应窗口")
        fit.clicked.connect(canvas.fit)
        layout.addWidget(fit)
        dialog.exec()

    def export_selected(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("按需导出补件")
        layout = QVBoxLayout(dialog)
        note = QLabel("只生成选中产物。PDF需要的查图页在内部使用，不混入成品。")
        note.setWordWrap(True)
        layout.addWidget(note)
        targets = {}
        for key, text in (
            ("production", "生产 TIFF"),
            ("reference", "全部分页查图 TIFF"),
            ("pdf", "合并查图 PDF"),
            ("counts", "准确针数 XLSX"),
            ("showcase", "当前展示模拟 PNG"),
            ("notes", "中英文说明（2份）"),
        ):
            item = QCheckBox(text)
            targets[key] = item
            layout.addWidget(item)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            chosen = frozenset(k for k, widget in targets.items() if widget.isChecked())
            if not chosen:
                return self.error("请至少选择一种产物")
            self.export(chosen)

    def annotate_region(self, kind):
        from stitchpress.core.masks import annotation_masks

        try:
            if not self.region:
                raise StitchPressError("请先框选针格区域")
            record = copy.deepcopy(
                self.draft.entities.get("annotations")
                or {
                    "schema_version": 1,
                    "grid": [self.design.width, self.design.height],
                    "source_sha": sha256(self.source_image) if self.source_image else None,
                    "regions": [],
                }
            )
            record["regions"].append({"id": uuid4().hex, "kind": kind, "region": list(self.region)})
            annotation_masks(
                record,
                self.design.width,
                self.design.height,
                sha256(self.source_image) if self.source_image else None,
            )
            self.draft.entities["annotations"] = record
            self.invalidate_review("主体/负空间/保护区改变")
            self.dirty = True
            self.refresh_annotations()
            self.update_document_state()
            self.statusBar().showMessage(
                "标注已保存到草稿。主体/留空用于下次同源转换；保护区立即阻止局部改针。"
            )
        except Exception as exc:
            self.error(exc)

    def clear_annotations(self):
        if record := self.draft.entities.pop("annotations", None):
            self.invalidate_review("蒙版标注清除")
            self.draft.entities[f"annotations:{uuid4().hex}"] = record
            self.dirty = True
            self.refresh_annotations()
            self.update_document_state()

    def start_polygon_mask(self, kind):
        self.show_page(0)
        self.canvas.begin_polygon(kind)
        self.statusBar().showMessage(
            "左键逐点标注；双击或右键完成；Esc取消。按针格中心规则填充，不自动平滑。"
        )

    def annotate_polygon(self, payload):
        try:
            design = self.design
            dx = (self.canvas.sceneRect().width() - design.width * 18) // 2
            dy = (self.canvas.sceneRect().height() - design.height * 18) // 2
            points = []
            for x, y in payload["points"]:
                point = [
                    min(design.width, max(0, round((x - dx) / 18))),
                    min(design.height, max(0, round((y - dy) / 18))),
                ]
                if not points or point != points[-1]:
                    points.append(point)
            record = copy.deepcopy(
                self.draft.entities.get("annotations")
                or {
                    "schema_version": 1,
                    "grid": [design.width, design.height],
                    "source_sha": sha256(self.source_image) if self.source_image else None,
                    "regions": [],
                }
            )
            record["regions"].append(
                {
                    "id": uuid4().hex,
                    "kind": payload["kind"],
                    "shape": "polygon",
                    "points": points,
                    "fill_rule": "cell-center-even-odd-rational-v1",
                }
            )
            annotation_masks(
                record,
                design.width,
                design.height,
                sha256(self.source_image) if self.source_image else None,
            )
            self.draft.entities["annotations"] = record
            self.invalidate_review("多边形标注改变")
            self.dirty = True
            self.refresh_annotations()
            self.update_document_state()
            self.statusBar().showMessage("多边形已加入草稿，源图与针格坐标已绑定")
        except Exception as exc:
            self.error(exc)

    def manage_annotations(self):
        record = self.draft.entities.get("annotations")
        if not record:
            return self.statusBar().showMessage("当前没有蒙版标注")
        dialog = QDialog(self)
        dialog.setWindowTitle("蒙版标注管理 · 保存后生效")
        dialog.resize(720, 550)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["类型", "形状", "范围/顶点数"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        entries = copy.deepcopy(record["regions"])

        def refresh():
            table.setRowCount(len(entries))
            for i, entry in enumerate(entries):
                for j, value in enumerate(
                    (
                        entry["kind"],
                        entry.get("shape", "rectangle"),
                        str(entry.get("region", str(len(entry.get("points", []))) + "个顶点")),
                    )
                ):
                    table.setItem(i, j, QTableWidgetItem(value))

        def remove():
            if table.currentRow() >= 0:
                entries.pop(table.currentRow())
                refresh()

        button = QPushButton("移除所选标注")
        button.clicked.connect(remove)
        layout.addWidget(button)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        refresh()
        if dialog.exec() == QDialog.DialogCode.Accepted and entries != record["regions"]:
            self.draft.entities[f"annotations:{uuid4().hex}"] = copy.deepcopy(record)
            if entries:
                self.draft.entities["annotations"] = {**record, "regions": entries}
            else:
                self.draft.entities.pop("annotations", None)
            self.invalidate_review("蒙版标注移除")
            self.dirty = True
            self.refresh_annotations()
            self.update_document_state()

    def refresh_annotations(self):
        record = self.draft.entities.get("annotations")
        regions = []
        if (
            record
            and record.get("grid") == [self.design.width, self.design.height]
            and record.get("source_sha")
            == (sha256(self.source_image) if self.source_image else None)
        ):
            dx = (self.canvas.sceneRect().width() - self.design.width * 18) // 2
            dy = (self.canvas.sceneRect().height() - self.design.height * 18) // 2
            colors = {"subject": "#5cbea8", "blank": "#a58ae6", "protected": "#d99c54"}
            for item in record["regions"]:
                if item.get("shape") == "polygon":
                    path = QPainterPath()
                    path.moveTo(dx + item["points"][0][0] * 18, dy + item["points"][0][1] * 18)
                    for x, y in item["points"][1:]:
                        path.lineTo(dx + x * 18, dy + y * 18)
                    path.closeSubpath()
                    regions.append((path, colors[item["kind"]]))
                    continue
                x0, y0, x1, y1 = item["region"]
                regions.append(
                    (
                        QRectF(dx + x0 * 18, dy + y0 * 18, (x1 - x0) * 18, (y1 - y0) * 18),
                        colors[item["kind"]],
                    )
                )
        self.canvas.draw_regions(regions)

    def import_input_icc(self):
        import io

        from PIL import ImageCms

        from stitchpress.core.conversion import normalize_image

        path, _ = QFileDialog.getOpenFileName(
            self, "明确覆盖源图输入ICC（不改变生产ICC）", "", "ICC (*.icc *.icm)"
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            if len(data) > 8 * 1024 * 1024:
                raise StitchPressError("输入ICC超过8MiB上限")
            profile = ImageCms.ImageCmsProfile(io.BytesIO(data))
            if profile.profile.xcolor_space.strip() not in {"RGB", "CMYK", "GRAY"}:
                raise StitchPressError("输入ICC仅支持RGB/CMYK/GRAY")
            if self.source_image:
                normalize_image(self.source_image, data)
            asset = f"resources/input-icc/{sha256(data)}.icc"
            self.draft.assets[asset] = data
            self.draft.entities["input_profile"] = {
                "asset": asset,
                "sha256": sha256(data),
                "name": Path(path).name,
            }
            self.invalidate_review("源图输入ICC改变")
            self.dirty = True
            if self.source_image:
                self.display_source(self.source_image)
            self.update_document_state()
        except Exception as exc:
            self.error(exc)

    def clear_input_icc(self):
        if self.draft.entities.pop("input_profile", None):
            self.invalidate_review("源图输入ICC覆盖清除")
            self.dirty = True
            if self.source_image:
                try:
                    self.display_source(self.source_image)
                except StitchPressError:
                    self.source_canvas.scene().clear()
                    self.source_label.setText("源图缺少可用ICC；请指定输入ICC后再转换")
            self.update_document_state()

    def show_quality(self, report):
        dialog = QDialog(self)
        dialog.setWindowTitle("母版数字检查 · 仍需人工审核")
        dialog.resize(720, 600)
        layout = QVBoxLayout(dialog)
        tiny = sum(report["tiny_components"].values())
        summary = QLabel(
            f"{report['stitches']:,} 针 · {report['blank_cells']:,} 格留空 · "
            f"{report['colors']} 色\n"
            f"小色块（同色四邻域≤3针）{tiny:,} 处；可能是必要细节，不自动删除。\n"
            "数字检查不能判断构图、对象与来源；白色有针不等于留空。"
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["线色", "针数", "区域（列/行，0基）"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setStretchLastSection(True)
        for row, item in enumerate(report["examples"]):
            table.insertRow(row)
            color = self.design.palette[item["palette_index"]]
            for col, text in enumerate(
                (f"{color.brand} {color.code}", str(item["cells"]), str(item["region"]))
            ):
                table.setItem(row, col, QTableWidgetItem(text))
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)
        if report["examples_truncated"]:
            layout.addWidget(QLabel("列表只列前200处，汇总数量仍为完整结果。"))
        locate = QPushButton("返回画布定位所选疑点")

        def focus():
            row = table.currentRow()
            if row < 0:
                return
            x0, y0, x1, y1 = report["examples"][row]["region"]
            design = self.design
            dx = (self.canvas.sceneRect().width() - design.width * 18) // 2
            dy = (self.canvas.sceneRect().height() - design.height * 18) // 2
            rect = QRectF(dx + x0 * 18, dy + y0 * 18, (x1 - x0) * 18, (y1 - y0) * 18)
            self.show_page(0)
            self.select_region(rect)
            self.canvas.draw_selection(rect)
            self.canvas.actual_pixels()
            self.canvas.centerOn(rect.center())
            dialog.accept()

        locate.clicked.connect(focus)
        layout.addWidget(locate)
        dialog.exec()
