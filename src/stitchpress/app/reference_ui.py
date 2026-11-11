"""Six discoverable R1 pages. Existing AI and manufacturer delivery remain extensions."""

import copy
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest
from stitchpress.core.post import POST_TARGETS, PostSettings, ThreadPlan
from stitchpress.core.render import screen_rgb
from stitchpress.core.resources import assign_symbols, library_from_assets, natural_color_key
from stitchpress.platform.settings import settings_dir

from .commerce_ui import CommerceDialog
from .local_features import SpecificationDialog
from .post_settings import MissingSettingsAssets, companion_candidates, load_settings, save_settings
from .reference_actions import bind_symbol, import_pattern
from .reference_package import post_fingerprint
from .text_settings import TextSettingsDialog

TARGET_NAMES = dict(
    zip(
        POST_TARGETS,
        (
            "原始 XSD（字节保留）",
            "对应图 JPG",
            "生产画布 TIFF",
            "整张查图 TIFF",
            "符号表 TIFF",
            "速查卡 TIFF",
            "各 CT 配线 Excel",
            "点图效果 BMP",
            "分页图纸 JPG",
            "电商 JPG",
            "作品设置 JSON 与资源",
        ),
        strict=True,
    )
)
PAGE_NAMES = ("文件与设置", "画布与图纸", "文字与配线", "符号与分配", "预览与出图", "电商图片")


def text(value):
    label = QLabel(value)
    label.setWordWrap(True)
    return label


def action(label, callback):
    button = QPushButton(label)
    button.clicked.connect(callback)
    return button


def row(layout, *items):
    line = QHBoxLayout()
    for item in items:
        line.addWidget(item)
    layout.addLayout(line)


class EditorPanel(QWidget):
    """Stage a native form; apply explicitly, preserve edits across page navigation."""

    def __init__(self, owner, kind):
        super().__init__()
        self.owner, self.kind, self.project_id = owner, kind, None
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.editor = None
        self.model_sha = None
        self.reload()

    def keys(self):
        return {
            "spec": ("fabric", "reference", "output_font", "showcase"),
            "text": ("text_layers",),
            "commerce": ("commerce",),
        }[self.kind]

    def model(self):
        return digest({k: self.owner.draft.entities.get(k) for k in self.keys()})

    def value(self):
        d = self.editor
        if self.kind == "spec":
            fabric, reference = d.values()
            return [asdict(fabric), reference.to_dict(), d.showcase_values().to_dict(), d.font_data]
        return [d.values(), getattr(d, "assets", {})]

    def pending(self):
        if self.project_id != self.owner.draft.project_id:
            return False
        try:
            return self.value() != self.start_values
        except Exception:
            return True

    def reload(self):
        if self.editor:
            self.layout_.removeWidget(self.editor)
            self.editor.deleteLater()
        ctor = {
            "spec": SpecificationDialog,
            "text": TextSettingsDialog,
            "commerce": CommerceDialog,
        }[self.kind]
        self.editor = ctor(self.owner)
        self.editor.setWindowFlags(Qt.WindowType.Widget)
        # Route the standard Save to the page transaction; Cancel reloads the page form.
        buttons = self.editor.findChildren(QDialogButtonBox)[-1]
        buttons.accepted.disconnect()
        buttons.rejected.disconnect()
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("应用到草稿")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("恢复本页")
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reload)
        self.layout_.addWidget(self.editor)
        self.project_id = self.owner.draft.project_id
        self.start_values = copy.deepcopy(self.value())
        self.model_sha = self.model()
        for field in self.editor.findChildren(QWidget):
            signal = (
                field.textChanged
                if isinstance(field, (QLineEdit, QTextEdit))
                else field.currentIndexChanged
                if isinstance(field, QComboBox)
                else field.valueChanged
                if isinstance(field, QSpinBox)
                else field.toggled
                if isinstance(field, QCheckBox)
                else None
            )
            if signal is not None:
                signal.connect(self.owner.mark_reference_pending)
        self.editor.show()

    def sync(self):
        if self.project_id != self.owner.draft.project_id or (
            self.model_sha != self.model() and not self.pending()
        ):
            self.reload()

    def apply(self):
        try:
            value = self.value()  # Validate every field before mutating the draft.
            if self.model_sha != self.model():
                raise StitchPressError(
                    "此页参数已在别处改变，请取消恢复本页后重新编辑，避免覆盖新设置"
                )
            before = {k: copy.deepcopy(self.owner.draft.entities.get(k)) for k in self.keys()}
            d = self.editor
            if self.kind == "spec":
                (
                    self.owner.draft.entities["fabric"],
                    self.owner.draft.entities["reference"],
                    self.owner.draft.entities["showcase"],
                ) = value[:3]
                if d.font_data:
                    from stitchpress.core.identity import sha256

                    name = f"resources/fonts/{sha256(d.font_data)}.font"
                    self.owner.draft.assets[name] = d.font_data
                    self.owner.draft.entities["output_font"] = dict(
                        asset=name, name=d.font_name, sha256=sha256(d.font_data)
                    )
            else:
                self.owner.draft.entities["text_layers" if self.kind == "text" else "commerce"] = (
                    value[0] if self.kind == "text" else value[0].to_dict()
                )
                self.owner.draft.assets.update(d.assets)
            self.owner.draft.entities[f"reference-settings:{uuid4().hex}"] = before
            self.start_values = copy.deepcopy(self.value())
            self.model_sha = self.model()
            self.owner.dirty = True
            self.owner.refresh()
            self.owner.statusBar().showMessage("本页参数已应用；保存作品后可在下次继续")
        except Exception as exc:
            self.owner.error(exc)


class ReferenceFeatures:
    def build_reference_pages(self):
        self.reference_panels = []
        self.reference_nav = []
        self.reference_project = None
        nav = QWidget()
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(20, 4, 20, 8)
        for number, name in enumerate(PAGE_NAMES, 3):
            button = QPushButton(name)
            button.setCheckable(True)
            button.toggled.connect(
                lambda checked, index=number: self.show_page(index) if checked else None
            )
            self.reference_nav.append(button)
            self.navigation_group.addButton(button)
            nav_layout.addWidget(button)
        self.centralWidget().layout().insertWidget(2, nav)
        for build in (
            self.file_page,
            self.spec_page,
            self.text_page,
            self.symbol_page,
            self.post_page,
            self.commerce_page,
        ):
            content = build()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(content)
            self.pages.addWidget(scroll)
        self.show_page(3)

    def mark_reference_pending(self, *args):
        if hasattr(self, "preview_state"):
            self.preview_state.setText("页面参数正在编辑；请先应用，再刷新实际预览")

    def file_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            text("从已有全针 XSD 开始：确认对应原图和货号，设置尺寸与符号，再预览并输出参考全套。")
        )
        row(
            layout,
            action("导入全针 XSD…", self.import_reference_pattern),
            action("选择对应原图…", self.choose_image),
            action("打开作品…", self.open_project),
            action("保存作品", self.save),
            action("另存作品…", self.save_as),
        )
        form = QFormLayout()
        self.ref_title, self.ref_code = QLineEdit(), QLineEdit()
        for field, old in ((self.ref_title, self.title_edit), (self.ref_code, self.code_edit)):
            field.textChanged.connect(old.setText)
            field.editingFinished.connect(self.apply_fields)
        form.addRow("作品名称", self.ref_title)
        form.addRow("客户货号（保留前导零）", self.ref_code)
        layout.addLayout(form)
        row(
            layout,
            action("采用 XSD 文件名作为货号建议", self.accept_code_suggestion),
            action("发现同名对应图…", self.discover_companions),
        )
        self.source_confirm = QCheckBox("我已核对：对应原图与当前 XSD 是同一作品")
        self.source_confirm.toggled.connect(self.confirm_correspondence)
        layout.addWidget(self.source_confirm)
        self.ref_input_report = text("尚未导入参考原稿")
        layout.addWidget(self.ref_input_report)
        row(
            layout,
            action("导出作品设置 JSON…", self.save_reference_settings),
            action("打开作品设置 JSON…", self.open_reference_settings),
            action("选择输出父目录…", self.choose_post_folder),
        )
        from stitchpress.platform.settings import load_settings as load_local

        self.post_folder = QLineEdit(
            load_local().get("reference_output_directory")
            or str(Path.home() / "Documents" / "StitchPress")
        )
        self.post_folder.setReadOnly(True)
        form = QFormLayout()
        form.addRow("输出父目录（每次建立新版本）", self.post_folder)
        layout.addLayout(form)
        layout.addWidget(
            text(
                "设置 JSON 与同名资源目录须一起移动；资源缺失可重新定位，SHA 不匹配会停止打开。"
                "常用尺寸预设在“画布与图纸 → 预设”。"
            )
        )
        layout.addStretch()
        return page

    def spec_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.spec_panel = EditorPanel(self, "spec")
        self.reference_panels.append(self.spec_panel)
        layout.addWidget(self.spec_panel)
        return page

    def text_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        tabs = QTabWidget()
        self.text_panel = EditorPanel(self, "text")
        self.reference_panels.append(self.text_panel)
        tabs.addTab(self.text_panel, "画布 / 查图 / 符号表文字")
        threads = QWidget()
        form = QVBoxLayout(threads)
        form.addWidget(
            text(
                "每行一套 CT 与每米针数；各 CT 独立输出 Excel。"
                "米数 = 逐色针数 ÷ 每米针数后向上取整。"
                "系数由你按材料规则确认，软件不猜默认值。"
            )
        )
        self.thread_table = QTableWidget(0, 4)
        self.thread_table.setHorizontalHeaderLabels(["CT", "每米针数", "系数来源", "已确认"])
        self.thread_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        form.addWidget(self.thread_table)
        row(
            form,
            action("添加 CT 规则", self.add_thread_row),
            action("移除选中规则", self.remove_thread_row),
            action("应用配线规则", self.apply_thread_rows),
        )
        tabs.addTab(threads, "多 CT 配线")
        layout.addWidget(tabs)
        return page

    def symbol_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row(
            layout,
            action("选择已存符号库…", self.select_reference_library),
            action("导入客户资源包…", self.import_resources),
            action("从文件夹导入符号…", self.import_reference_symbols),
            action("选择符号输出 ICC…", self.import_symbol_icc),
            action("编辑当前库…", self.manage_symbols),
        )
        self.ref_library_label = text("")
        layout.addWidget(self.ref_library_label)
        split = QSplitter()
        self.ref_symbols = QListWidget()
        self.ref_symbols.setViewMode(QListWidget.ViewMode.IconMode)
        self.ref_symbols.setIconSize(QSize(52, 52))
        self.ref_symbols.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.ref_symbols.setMinimumWidth(260)
        self.ref_symbols.currentItemChanged.connect(self.show_reference_symbol)
        split.addWidget(self.ref_symbols)
        right = QWidget()
        rl = QVBoxLayout(right)
        self.ref_symbol_zoom = QLabel("选择左侧符号，右侧选择线色")
        self.ref_symbol_zoom.setMinimumHeight(130)
        rl.addWidget(self.ref_symbol_zoom)
        self.ref_symbol_actual = QLabel("实际格距小符号（100%像素）")
        rl.addWidget(self.ref_symbol_actual)
        self.ref_mapping = QTableWidget(0, 4)
        self.ref_mapping.setHorizontalHeaderLabels(
            ["DMC / 线色", "针数", "精确符号 ID", "手动保护"]
        )
        self.ref_mapping.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ref_mapping.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ref_mapping.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        rl.addWidget(self.ref_mapping)
        split.addWidget(right)
        layout.addWidget(split, 1)
        self.keep_manual = QCheckBox("自动匹配时保留手动绑定（包含交换的两色）")
        self.keep_manual.setChecked(True)
        layout.addWidget(self.keep_manual)
        row(
            layout,
            action("将所选符号分配 / 整体交换", self.bind_reference_symbol),
            action("按着墨色接近自动匹配…", self.auto_reference_symbols),
        )
        layout.addWidget(
            text(
                "匹配 color-nearest-v1：手动项优先，其余按针数从多到少，以符号平均"
                "着墨色与线色距离选择未用符号。"
                "并列按自然线号和库顺序稳定处理。"
                "实际 CT 小符号与大图仅屏幕预览，打印可读性另验。"
            )
        )
        return page

    def post_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.post_summary = text("点击“检查尺寸和出图估算”取得与导出相同的计划。")
        layout.addWidget(self.post_summary)
        row(
            layout,
            action("检查尺寸和出图估算", self.plan_reference_output),
            action("登记本次核对…", self.review_reference_output),
            action("打开上次输出目录", self.open_post_folder),
            action("本地任务与失败恢复…", self.post_task_history),
        )
        preview_box = QGroupBox("实际目标预览")
        form = QFormLayout(preview_box)
        self.post_preview_target = QComboBox()
        for key in ("production", "whole", "pages", "symbols", "card", "effect"):
            self.post_preview_target.addItem(TARGET_NAMES[key], key)
        from stitchpress.core.commerce import TARGETS

        for key in TARGETS:
            self.post_preview_target.addItem("电商 · " + key, key)
        self.post_preview_number = QSpinBox()
        self.post_preview_number.setRange(1, 100)
        form.addRow("目标", self.post_preview_target)
        form.addRow("查图 / 符号表 / 卡页码", self.post_preview_number)
        form.addRow(action("刷新实际预览…", self.preview_reference_output))
        self.preview_state = text("参数变化后须刷新；后台旧快照不会覆盖当前预览。")
        form.addRow(self.preview_state)
        layout.addWidget(preview_box)
        targets = QGroupBox("参考全套 / 单项输出")
        target_layout = QVBoxLayout(targets)
        self.post_targets = {}
        for key, name in TARGET_NAMES.items():
            item = QCheckBox(name)
            self.post_targets[key] = item
            target_layout.addWidget(item)
        layout.addWidget(targets)
        form = QFormLayout()
        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(80, 100)
        self.tiff_container = QComboBox()
        self.tiff_container.addItem("Classic TIFF（默认）", "classic")
        self.tiff_container.addItem("BigTIFF（生产图）", "bigtiff")
        self.bigtiff_confirm = QCheckBox("已确认目标读取软件支持 BigTIFF")
        self.effect_cell = QSpinBox()
        self.effect_cell.setRange(6, 36)
        self.post_demo = QCheckBox("草稿输出（示例符号可用，文件以 DRAFT 标记）")
        self.post_demo.setChecked(True)
        for name, widget in (
            ("JPEG 质量", self.jpeg_quality),
            ("TIFF 容器", self.tiff_container),
            ("兼容确认", self.bigtiff_confirm),
            ("点图每格像素", self.effect_cell),
            ("模式", self.post_demo),
        ):
            form.addRow(name, widget)
        layout.addLayout(form)
        row(
            layout,
            action("应用输出选项", self.apply_post_options),
            action("生成所选参考文件…", self.export_reference_output),
            action("取消当前任务", self.cancel_job),
        )
        layout.addWidget(
            text(
                "正式输出须客户符号库 / CMYK ICC，以及当前原稿、映射和尺寸核对。"
                "这里不要求二期 AI 或专用交付审核。"
                "每次发布新目录；失败或取消不发布半成品。"
            )
        )
        return page

    def commerce_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.commerce_panel = EditorPanel(self, "commerce")
        self.reference_panels.append(self.commerce_panel)
        layout.addWidget(self.commerce_panel)
        row(
            layout,
            action("图形化挑选场景 / 相框…", self.commerce_gallery),
            action("预览电商目标…", self.preview_commerce),
            action("导出 JPG 组 / 单张…", self.export_reference_commerce),
        )
        return page

    def reference_pending(self):
        if any(panel.pending() for panel in getattr(self, "reference_panels", ())):
            return True
        if getattr(self, "reference_project", None) != self.draft.project_id:
            return False
        try:
            return (
                self.thread_values() != self._thread_baseline
                or self.post_values().to_dict() != self._post_baseline
            )
        except Exception:
            return True

    def require_reference_applied(self):
        if not self.apply_fields():
            return False
        if self.reference_pending():
            raise StitchPressError(
                "有尚未应用的页面参数，请先点击对应页面的保存 / 应用，"
                "或取消恢复后再预览、保存或导出"
            )
        return True

    def refresh_reference_pages(self):
        if not hasattr(self, "reference_nav"):
            return
        metadata = self.draft.entities["metadata"]
        for field, key in ((self.ref_title, "title"), (self.ref_code, "code")):
            if not field.hasFocus():
                field.setText(metadata[key])
        source = self.draft.entities.get("source", {})
        self.source_confirm.blockSignals(True)
        self.source_confirm.setChecked(source.get("correspondence_confirmed", False))
        self.source_confirm.blockSignals(False)
        self.source_confirm.setEnabled(bool(source))
        record = self.draft.entities.get("pattern_input", {})
        self.ref_input_report.setText(
            f"原稿：{record.get('filename', '尚未导入 XSD')}\n"
            f"{self.design.width} × {self.design.height} 格 · {sum(self.design.counts):,} 针 · "
            f"{sum(n > 0 for n in self.design.counts)} 色 · "
            f"空白 {self.design.width * self.design.height - sum(self.design.counts):,} 格\n"
            f"XSD SHA：{record.get('sha256', '无原始XSD记录')}\n"
            f"对应原图：{source.get('filename', source.get('asset', '尚未绑定'))}"
        )
        for panel in self.reference_panels:
            panel.sync()
        project_changed = self.reference_project != self.draft.project_id
        if project_changed or not self._post_or_threads_pending():
            options = PostSettings(**self.draft.entities.get("post", {}))
            self.thread_table.setRowCount(0)
            for plan in options.thread_plans:
                self.add_thread_row(plan=plan)
            for key, control in self.post_targets.items():
                control.setChecked(key in options.targets)
            self.jpeg_quality.setValue(options.jpeg_quality)
            self.tiff_container.setCurrentIndex(
                self.tiff_container.findData(options.tiff_container)
            )
            self.bigtiff_confirm.setChecked(options.bigtiff_confirmed)
            self.effect_cell.setValue(options.effect_cell)
            self._thread_baseline = self.thread_values()
            self._post_baseline = self.post_values().to_dict()
        self.reference_project = self.draft.project_id
        self.refresh_reference_symbols()
        current = post_fingerprint(self.draft)
        if getattr(self, "_reference_preview_sha", None) != current:
            self.preview_state.setText("预览待刷新 / 参数已改变；请查看当前真实目标")

    def _post_or_threads_pending(self):
        if not hasattr(self, "_post_baseline"):
            return False
        try:
            return (
                self.thread_values() != self._thread_baseline
                or self.post_values().to_dict() != self._post_baseline
            )
        except Exception:
            return True

    def add_thread_row(self, checked=False, *, plan=None):
        plan = plan or {}
        index = self.thread_table.rowCount()
        self.thread_table.insertRow(index)
        for column, key in enumerate(("ct", "stitches_per_metre", "source_note")):
            self.thread_table.setItem(index, column, QTableWidgetItem(plan.get(key, "")))
        confirmed = QCheckBox("已核对")
        confirmed.setChecked(plan.get("confirmed", False))
        self.thread_table.setCellWidget(index, 3, confirmed)

    def remove_thread_row(self):
        index = self.thread_table.currentRow()
        if index >= 0:
            self.thread_table.removeRow(index)

    def thread_values(self):
        return [
            asdict(
                ThreadPlan(
                    *(self.thread_table.item(i, j).text().strip() for j in range(3)),
                    self.thread_table.cellWidget(i, 3).isChecked(),
                )
            )
            for i in range(self.thread_table.rowCount())
        ]

    def apply_thread_rows(self):
        try:
            options = PostSettings(
                **{**self.draft.entities.get("post", {}), "thread_plans": self.thread_values()}
            )
            self.draft.entities["post"] = options.to_dict()
            self._thread_baseline = self.thread_values()
            self._post_baseline["thread_plans"] = options.to_dict()["thread_plans"]
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def post_values(self):
        return PostSettings(
            targets=tuple(key for key, box in self.post_targets.items() if box.isChecked()),
            jpeg_quality=self.jpeg_quality.value(),
            tiff_container=self.tiff_container.currentData(),
            bigtiff_confirmed=self.bigtiff_confirm.isChecked(),
            effect_cell=self.effect_cell.value(),
            thread_plans=tuple(self.thread_values()),
        )

    def apply_post_options(self):
        try:
            self.draft.entities["post"] = self.post_values().to_dict()
            self._post_baseline = self.post_values().to_dict()
            self._thread_baseline = self.thread_values()
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def import_reference_pattern(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入已有全针XSD（其他针法会停止）", "", "Pattern Maker (*.xsd)"
        )
        if not path:
            return
        try:
            updated = import_pattern(self.draft, path)
            if not self.confirm_discard():
                return
            # Keep current project identity and prior assets; import is a new draft revision.
            updated.project_id = self.draft.project_id
            self.set_project(updated)
            self.dirty = True
            self._reference_path = Path(path)
            self.refresh()
            self.statusBar().showMessage("全针 XSD 已导入，原件与旧修订已留底；请确认货号及对应图")
        except Exception as exc:
            self.error(exc)

    def accept_code_suggestion(self):
        record = self.draft.entities.get("pattern_input")
        if record:
            self.code_edit.setText(Path(record["filename"]).stem)
            self.apply_fields()

    def discover_companions(self):
        try:
            path = getattr(self, "_reference_path", None)
            if not path:
                chosen, _ = QFileDialog.getOpenFileName(
                    self, "重新定位XSD以发现同名对应图", "", "XSD (*.xsd)"
                )
                if not chosen:
                    return
                path = Path(chosen)
                record = self.draft.entities.get("pattern_input", {})
                from stitchpress.core.identity import sha256

                if sha256(path.read_bytes()) != record.get("sha256"):
                    raise StitchPressError("所选XSD与当前原稿SHA不同")
            candidates = companion_candidates(path)
            if not candidates:
                return self.statusBar().showMessage(
                    "没有发现同名图片，可使用“选择对应原图”手动绑定"
                )
            chosen, ok = QInputDialog.getItem(
                self,
                "发现对应图候选",
                "选择后仍需人工核对对应关系",
                [p.name for p in candidates],
                0,
                False,
            )
            if ok:
                file = next(p for p in candidates if p.name == chosen)
                self.import_source(file.read_bytes(), file.name)
                self.refresh()
        except Exception as exc:
            self.error(exc)

    def confirm_correspondence(self, checked):
        if source := self.draft.entities.get("source"):
            source["correspondence_confirmed"] = checked
            self.dirty = True
            self.refresh()

    def choose_post_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出父目录", self.post_folder.text())
        if path:
            self.post_folder.setText(path)
            from stitchpress.platform.settings import (
                load_settings as load_local,
            )
            from stitchpress.platform.settings import (
                save_settings as save_local,
            )

            config = load_local()
            config["reference_output_directory"] = path
            save_local(config)

    def save_reference_settings(self):
        try:
            if not self.require_reference_applied():
                return
            path, _ = QFileDialog.getSaveFileName(
                self, "另存作品设置（连同资源目录保留）", "作品设置.json", "JSON (*.json)"
            )
            if path:
                save_settings(self.draft, Path(path).with_suffix(".json"))
                self.statusBar().showMessage("设置 JSON 和相对资源目录已保存，可一起移动重开")
        except Exception as exc:
            self.error(exc)

    def open_reference_settings(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开本软件版本化设置", "", "JSON (*.json)")
        if not path:
            return
        try:
            replacements = {}
            while True:
                try:
                    project = load_settings(path, replacements=replacements)
                    break
                except MissingSettingsAssets as exc:
                    for name in exc.missing:
                        selected, _ = QFileDialog.getOpenFileName(self, "重新定位资源：" + name)
                        if not selected:
                            return
                        replacements[name] = Path(selected)
            if self.confirm_discard():
                self.set_project(project)
                self.dirty = True
        except Exception as exc:
            self.error(exc)

    def refresh_reference_symbols(self):
        library = library_from_assets(self.draft.entities["library"], self.draft.assets)
        mapping = self.draft.entities["mapping"]
        manual = self.draft.entities.get("manual_mapping", {}).get("bindings", {})
        token = digest([library.revision, mapping, manual, self.design.content_sha])
        if token == getattr(self, "_symbol_page_sha", None):
            return
        self._symbol_page_sha = token
        self.ref_library_label.setText(
            f"当前库 {library.revision[:16]} · {len(library.symbols)} 个符号 · {library.usage} · "
            + ("已带 CMYK ICC" if library.icc else "无 ICC，仅草稿")
        )
        self.ref_symbols.clear()
        for symbol in library.symbols:
            rgb = screen_rgb(symbol.image(), library.icc)
            item = QListWidgetItem(QIcon(QPixmap.fromImage(ImageQt(rgb))), symbol.key)
            rgb.close()
            item.setData(Qt.ItemDataRole.UserRole, symbol.key)
            self.ref_symbols.addItem(item)
        self.ref_mapping.setRowCount(0)
        for index in sorted(
            range(len(self.design.palette)), key=lambda i: natural_color_key(self.design.palette[i])
        ):
            color, count = self.design.palette[index], self.design.counts[index]
            if not count:
                continue
            row_number = self.ref_mapping.rowCount()
            self.ref_mapping.insertRow(row_number)
            for col, value in enumerate(
                (
                    f"{color.brand} {color.code}",
                    str(count),
                    mapping[color.key],
                    "保护" if color.key in manual else "自动",
                )
            ):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, color.key)
                self.ref_mapping.setItem(row_number, col, item)

    def show_reference_symbol(self, current, previous=None):
        if not current:
            return
        library = library_from_assets(self.draft.entities["library"], self.draft.assets)
        image = screen_rgb(
            library.by_key[current.data(Qt.ItemDataRole.UserRole)].image(), library.icc
        )
        self.ref_symbol_zoom.setPixmap(
            QPixmap.fromImage(ImageQt(image)).scaled(
                120,
                120,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )
        self.ref_symbol_zoom.setToolTip(current.text())
        from stitchpress.core.layout import Fabric, decimal, px

        fabric = Fabric(**self.draft.entities["fabric"])
        side = max(3, px(decimal(fabric.sx), fabric.dpi))
        tiny = image.resize((side, max(3, px(decimal(fabric.sy), fabric.dpi))))
        self.ref_symbol_actual.setPixmap(QPixmap.fromImage(ImageQt(tiny)))
        self.ref_symbol_actual.setToolTip(
            f"实际格距：{fabric.sx} × {fabric.sy}cm；"
            f"{fabric.dpi}DPI：{tiny.width} × {tiny.height}px。"
            "屏幕100%像素不等于物理打印尺寸"
        )
        tiny.close()
        image.close()

    def bind_reference_symbol(self):
        try:
            symbol, selected = self.ref_symbols.currentItem(), self.ref_mapping.selectedItems()
            if not symbol or not selected:
                raise StitchPressError("请同时选择左侧符号和右侧线色")
            key = selected[0].data(Qt.ItemDataRole.UserRole)
            chosen = symbol.data(Qt.ItemDataRole.UserRole)
            mapping = self.draft.entities["mapping"]
            other = next((k for k, value in mapping.items() if value == chosen and k != key), None)
            if (
                other
                and QMessageBox.question(
                    self,
                    "整体交换",
                    f"符号已被另一线色占用。"
                    f"将同时交换两色符号，记录两项手动保护；"
                    f"其他产物需重新生成。\n{mapping[key]} ↔ {chosen}",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            bind_symbol(self.draft, key, chosen)
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def auto_reference_symbols(self):
        try:
            library = library_from_assets(self.draft.entities["library"], self.draft.assets)
            manual = (
                self.draft.entities.get("manual_mapping", {}).get("bindings", {})
                if self.keep_manual.isChecked()
                else {}
            )
            active = {
                c.key for c, n in zip(self.design.palette, self.design.counts, strict=True) if n
            }
            manual = {k: v for k, v in manual.items() if k in active and v in library.by_key}
            mapping = assign_symbols(self.design, library, manual, strategy="color-nearest-v1")
            old = self.draft.entities["mapping"]
            changes = sum(old.get(k) != v for k, v in mapping.items())
            if (
                QMessageBox.question(
                    self,
                    "应用颜色接近匹配",
                    f"color-nearest-v1：保留 {len(manual)} 项手动绑定，"
                    f"将改变 {changes} 项。\n重新生成后生效，旧映射保留。",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            self.draft.entities[f"mapping:{uuid4().hex}"] = dict(
                before=copy.deepcopy(old), after=mapping, algorithm="color-nearest-v1"
            )
            self.draft.entities["mapping"] = mapping
            self.draft.entities["manual_mapping"] = dict(bindings=manual)
            self.draft.entities["mapping_policy"] = dict(
                algorithm="color-nearest-v1", manual_first=self.keep_manual.isChecked()
            )
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def import_reference_symbols(self):
        folder = QFileDialog.getExistingDirectory(self, "选择TIF/PNG/BMP符号目录；文件名为精确ID")
        if folder:
            try:
                self.start_worker("symbol-folder", dict(folder=folder, include_image=False))
            except Exception as exc:
                self.error(exc)

    def show_imported_symbols(self, result):
        from .store import Project

        candidate = Project.decode(Path(result["project"]).read_bytes())
        record = candidate.entities["library"]
        count = len(record["entries"])
        if (
            QMessageBox.question(
                self,
                "采用新符号库",
                f"全部 {count} 个符号已通过校验，将建立新库并重新匹配。旧库及原件保留。",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.install_reference_library(record, candidate.assets, candidate.entities["mapping"])
            self.draft.entities.setdefault("symbol_imports", {"records": []})["records"].extend(
                result["receipts"]
            )

    def import_symbol_icc(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择客户指定的CMYK符号输出配置", "", "ICC (*.icc *.icm)"
        )
        if not path:
            return
        try:
            from dataclasses import replace

            from stitchpress.core.identity import sha256
            from stitchpress.core.resources import library_assets

            library = library_from_assets(self.draft.entities["library"], self.draft.assets)
            raw = Path(path).read_bytes()
            # Binding a profile must not silently relabel existing ink values.
            # A new folder import performs the actual RGB/CMYK normalization.
            new = replace(
                library,
                icc=raw,
                revision=digest([library.revision, sha256(raw)]),
                usage="profile-selected-pending-symbol-import",
            )
            record, assets = library_assets(new)
            self.install_reference_library(record, assets, self.draft.entities["mapping"])
            self.statusBar().showMessage(
                "输出ICC已选择；请重新导入符号文件夹完成色彩转换后再正式输出"
            )
        except Exception as exc:
            self.error(exc)

    def install_reference_library(self, record, assets, mapping=None):
        previous = self.draft.entities["library"]
        library = library_from_assets(record, {**self.draft.assets, **assets})
        mapping = mapping or assign_symbols(self.design, library, strategy="color-nearest-v1")
        self.draft.entities[f"library:{previous['revision']}"] = copy.deepcopy(previous)
        self.draft.entities["library"] = copy.deepcopy(record)
        self.draft.assets.update(assets)
        self.draft.entities["mapping"] = mapping
        self.draft.entities["manual_mapping"] = dict(bindings={})
        self.dirty = True
        self.refresh()

    def select_reference_library(self):
        from stitchpress.core.resources import demo_library, library_assets

        base, base_assets = library_assets(demo_library())
        records = {
            v["revision"]: v
            for k, v in self.draft.entities.items()
            if (k == "library" or k.startswith("library:"))
            and isinstance(v, dict)
            and "revision" in v
        }
        records.setdefault(base["revision"], base)
        keys = list(records)
        chosen, ok = QInputDialog.getItem(
            self, "当前作品保存的符号库", "选择库版本（重新匹配；旧映射保留于历史）", keys, 0, False
        )
        if ok:
            try:
                self.install_reference_library(
                    records[chosen], base_assets if chosen == base["revision"] else {}
                )
            except Exception as exc:
                self.error(exc)

    def plan_reference_output(self):
        try:
            if self.require_reference_applied():
                self.start_worker("post-plan", dict(include_image=False))
        except Exception as exc:
            self.error(exc)

    def show_post_plan(self, result):
        paper = result["paper_px"] or ("未选查图", "—")
        self.post_summary.setText(
            f"{result['stitches']:,} 针 · {result['colors']} 色 · "
            f"画心 {' × '.join(result['artwork_cm'])} cm · "
            f"含留边 {' × '.join(result['cloth_cm'])} cm\n"
            f"生产 {result['production_px'][0]} × {result['production_px'][1]} px / "
            f"{result['production_dpi']} DPI\n"
            f"查图 {paper[0]} × {paper[1]} px / "
            f"{result['reference_dpi']} DPI · "
            f"{result['page_count']} 页 · 每页容量 {result['capacity']}\n"
            f"配线 CT：{', '.join(result['ct_plans']) or '未配置'} "
            f"· 电商 {result['commerce_files']} 张 "
            f"· 工作磁盘预留约 {result['estimate_bytes'] / 1024**2:.1f} MiB"
        )

    def preview_reference_output(self):
        try:
            if self.require_reference_applied():
                self.start_worker(
                    "post-preview",
                    dict(
                        target=self.post_preview_target.currentData(),
                        page_number=self.post_preview_number.value(),
                        include_image=False,
                    ),
                )
        except Exception as exc:
            self.error(exc)

    def show_post_preview(self, result):
        from PIL import Image

        from .desktop import Canvas

        dialog = QDialog(self)
        dialog.resize(980, 760)
        dialog.setWindowTitle("参考目标 · 实际渲染预览")
        layout = QVBoxLayout(dialog)
        checks = result["checks"]
        preview_size = " × ".join(str(v) for v in checks.get("preview_size", ()))
        preview_title = TARGET_NAMES.get(checks.get("target"), "电商图片")
        layout.addWidget(
            text(
                ("仅显示首条带，不是整张缩略图。 " if checks.get("partial") else "")
                + f"{preview_title} · {preview_size} px · "
                f"第 {checks.get('page', 1)} 页"
            )
        )
        canvas = Canvas()
        with Image.open(result["preview"]) as source:
            canvas.set_image(source.copy(), fit=True)
        layout.addWidget(canvas, 1)
        row(
            layout,
            action("放大", lambda: canvas.zoom(1.2)),
            action("缩小", lambda: canvas.zoom(1 / 1.2)),
            action("100% 原像素", canvas.actual_pixels),
            action("适应窗口", canvas.fit),
        )
        self._reference_preview_sha = post_fingerprint(self.draft)
        self.preview_state.setText("已显示当前快照实际预览；更改参数后需刷新")
        dialog.exec()

    def review_reference_output(self):
        try:
            if not self.require_reference_applied():
                return
            name, ok = QInputDialog.getText(
                self,
                "确认当前参考出图参数",
                "核对原稿 / 对应图 / 符号 / 尺寸与配线口径后，填写确认人：",
            )
            if ok and name.strip():
                options = PostSettings(**self.draft.entities.get("post", {}))
                self.draft.entities["post_review"] = dict(
                    input_sha=post_fingerprint(self.draft),
                    options_sha=digest(options.to_dict()),
                    reviewer=name.strip(),
                )
                self.dirty = True
                self.update_document_state()
                self.statusBar().showMessage("当前参考出图参数已登记；参数变化后须重新核对")
        except Exception as exc:
            self.error(exc)

    def export_reference_output(self, checked=False, *, options=None):
        try:
            if not self.require_reference_applied():
                return
            from .reference_package import preflight

            options = options or PostSettings(**self.draft.entities.get("post", {}))
            preflight(self.draft, options, demo=self.post_demo.isChecked())
            from stitchpress.core.identity import safe_filename

            name, ok = QInputDialog.getText(
                self,
                "新版本输出目录",
                "在父目录下建立新目录（已有目录不会覆盖）",
                text=safe_filename(self.draft.entities["metadata"]["code"]) + "-" + uuid4().hex[:6],
            )
            if not ok or not name.strip():
                return
            if name != safe_filename(name) or name in {".", ".."}:
                raise StitchPressError("请使用不含路径分隔符的目录名")
            destination = Path(self.post_folder.text()) / name
            cancel = settings_dir() / "work" / ("post-cancel-" + uuid4().hex)
            self.start_worker(
                "reference-export",
                dict(
                    target=str(destination),
                    options=options.to_dict(),
                    demo=self.post_demo.isChecked(),
                    cancel_file=str(cancel),
                    include_image=False,
                ),
            )
        except Exception as exc:
            self.error(exc)

    def open_post_folder(self):
        from PySide6.QtCore import QUrl

        path = getattr(self, "_last_post_destination", None) or self.post_folder.text()
        if Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.statusBar().showMessage("所选输出目录尚未生成")

    def export_reference_commerce(self):
        from stitchpress.core.commerce import TARGETS

        choices = ["全部12张", "7张详情", "5张方图", *TARGETS]
        chosen, ok = QInputDialog.getItem(
            self, "电商JPG输出", "选择分组或单张目标", choices, 0, False
        )
        if not ok:
            return
        targets = (
            TARGETS
            if chosen == choices[0]
            else tuple(t for t in TARGETS if t.startswith("detail"))
            if chosen == choices[1]
            else tuple(t for t in TARGETS if t.startswith("square"))
            if chosen == choices[2]
            else (chosen,)
        )
        options = PostSettings(
            **{
                **self.draft.entities.get("post", {}),
                "targets": ("commerce",),
                "commerce_targets": targets,
            }
        )
        self.export_reference_output(options=options)

    def commerce_gallery(self):
        from .commerce_gallery import choose_gallery

        try:
            choose_gallery(self, self.commerce_panel.editor)
        except Exception as exc:
            self.error(exc)

    def post_task_history(self):
        from stitchpress.core.identity import sha256

        from .store import Project
        from .task_history import TaskHistory

        operations = {
            "reference-export": "参考出图",
            "post-preview": "目标预览",
            "post-plan": "尺寸计划",
            "symbol-folder": "符号导入",
            "commerce-gallery": "场景缩略图",
        }
        records = [
            (path, item)
            for path, item in TaskHistory(settings_dir() / "work").records(self.draft.project_id)
            if item["operation"] in operations
        ]
        dialog = QDialog(self)
        dialog.resize(880, 600)
        dialog.setWindowTitle("一期本地任务与失败恢复")
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            text(
                "选中任务查看状态。恢复只打开当时的参数快照；确认后重新生成新目录，不自动重试，不覆盖已完成产物。"
            )
        )
        table = QTableWidget(len(records), 3)
        table.setHorizontalHeaderLabels(["任务", "状态", "时间"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for i, (_, item) in enumerate(records):
            state = {
                "completed": "完成",
                "failed": "失败",
                "cancelled": "已取消",
                "interrupted": "中断",
                "running": "运行中",
            }.get(item["status"], item["status"])
            for j, value in enumerate(
                (operations[item["operation"]], state, item["created_at"][:19])
            ):
                table.setItem(i, j, QTableWidgetItem(value))
        layout.addWidget(table)
        details = text("选择任务查看详情")
        layout.addWidget(details)

        def selected():
            if table.currentRow() < 0:
                raise StitchPressError("请先选择任务")
            return records[table.currentRow()]

        def show():
            path, item = selected()
            output = path.parent / "result.json"
            response = json.loads(output.read_bytes()) if output.exists() else {}
            result = response.get("result", {})
            details.setText(
                response.get("error")
                or (
                    "完成，产物目录：" + result["destination"]
                    if result.get("destination")
                    else "本地任务结果已保留；无输出不视作成功"
                )
            )

        def restore():
            try:
                path, item = selected()
                payload = (path.parent / "input.stitchpress").read_bytes()
                if sha256(payload) != item["input_sha"]:
                    raise StitchPressError("任务输入快照SHA已改变，不能恢复")
                project = Project.decode(payload)
                if self.confirm_discard():
                    self.set_project(project)
                    self.dirty = True
                    self.update_document_state()
                    dialog.accept()
            except Exception as exc:
                self.error(exc)

        table.itemSelectionChanged.connect(show)
        row(layout, action("恢复任务参数快照", restore), action("关闭", dialog.reject))
        if records:
            table.selectRow(0)
        dialog.exec()
