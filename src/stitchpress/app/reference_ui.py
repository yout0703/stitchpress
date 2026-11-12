"""Five task-oriented R1 workspaces; AI and special delivery remain extensions."""

import copy
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
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
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
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
from .navigation import ROUTES, disclosure, install_navigation, menu_button
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

PAGE_NAMES = tuple(route[1] for route in ROUTES)


def page_heading(layout, title, description):
    layout.setContentsMargins(28, 24, 28, 24)
    layout.setSpacing(12)
    heading = QVBoxLayout()
    heading.setSpacing(5)
    label = text(title)
    label.setProperty("role", "pageTitle")
    heading.addWidget(label)
    label = text(description)
    label.setProperty("role", "muted")
    heading.addWidget(label)
    layout.addLayout(heading)
    layout.addSpacing(10)


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
    fields = any(not isinstance(item, QAbstractButton) for item in items)
    for item in items:
        if not fields and item.property("role") == "primary":
            line.addStretch()
        line.addWidget(item, 1 if fields and not isinstance(item, QAbstractButton) else 0)
    if not fields and not any(item.property("role") == "primary" for item in items):
        line.addStretch()
    layout.addLayout(line)


class EditorPanel(QWidget):
    """Stage a native form; apply explicitly, preserve edits across page navigation."""

    def __init__(self, owner, kind):
        super().__init__()
        self.owner, self.kind, self.project_id = owner, kind, None
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.layout_.addWidget(self.form_scroll, 1)
        self.form_notice = text("所有更改已应用")
        self.form_notice.setProperty("role", "muted")
        self.footer = QHBoxLayout()
        self.footer.addWidget(self.form_notice, 1)
        self.layout_.addLayout(self.footer)
        self.buttons = None
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
        tab_indices = (
            [tab.currentIndex() for tab in self.editor.findChildren(QTabWidget)]
            if self.editor
            else []
        )
        if self.editor:
            if hasattr(self.editor, "board"):
                self.editor.board.stop()
            old_editor = self.form_scroll.takeWidget()
            old_editor.hide()
            old_editor.deleteLater()
            self.footer.removeWidget(self.buttons)
            self.buttons.hide()
            self.buttons.deleteLater()
        ctor = {
            "spec": SpecificationDialog,
            "text": TextSettingsDialog,
            "commerce": CommerceDialog,
        }[self.kind]
        self.editor = ctor(self.owner)
        self.editor.setWindowFlags(Qt.WindowType.Widget)
        self.editor.setObjectName("editorSurface")
        for tabs in self.editor.findChildren(QTabWidget):
            tabs.tabBar().setExpanding(False)
        for form in self.editor.findChildren(QFormLayout):
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            form.setVerticalSpacing(14)
            form.setHorizontalSpacing(22)
        for field in self.editor.findChildren(QLineEdit):
            if not isinstance(field.parentWidget(), (QSpinBox, QComboBox)):
                field.setMaximumWidth(min(field.maximumWidth(), 420))
        for field in self.editor.findChildren(QComboBox):
            field.setMaximumWidth(min(field.maximumWidth(), 300))
        for field in self.editor.findChildren(QSpinBox):
            field.setMinimumWidth(min(field.maximumWidth(), 96))
            field.setMaximumWidth(min(field.maximumWidth(), 140))
        for button in self.editor.findChildren(QPushButton):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.clicked.connect(self.owner.mark_reference_pending)
        if self.kind == "spec":
            self.editor.tabs.tabBar().hide()  # One shared page-level navigation.

        # Route the standard Save to the page transaction; Cancel reloads the page form.
        buttons = self.editor.findChildren(QDialogButtonBox)[-1]
        buttons.accepted.disconnect()
        buttons.rejected.disconnect()
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("应用更改")
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("role", "primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("恢复本页")
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reload)
        self.editor.layout().removeWidget(buttons)
        self.buttons = buttons
        self.form_scroll.setWidget(self.editor)
        self.footer.addWidget(buttons)
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
        for tabs, index in zip(self.editor.findChildren(QTabWidget), tab_indices, strict=False):
            tabs.setCurrentIndex(index)
        self.editor.show()
        if self.kind == "spec" and hasattr(self.owner, "print_stack"):
            self.owner.select_print_section(self.owner.print_tabs.currentIndex())
        self.owner.update_pending_navigation()

    def update_state(self):
        pending = self.pending()
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(pending)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(pending)
        self.form_notice.setText(
            "有未应用的更改" if pending else "所有更改已应用 · 保存作品可在下次继续"
        )

    def sync(self):
        if self.project_id != self.owner.draft.project_id or (
            self.model_sha != self.model() and not self.pending()
        ):
            self.reload()

    def apply(self):
        if not self.pending():
            return
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
        for build in (
            self.file_page,
            self.spec_page,
            self.text_page,
            self.symbol_page,
            self.post_page,
            self.commerce_page,
        ):
            content = build()
            if build in (self.post_page, self.spec_page, self.commerce_page):
                self.pages.addWidget(content)
                continue
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(content)
            self.pages.addWidget(scroll)
        install_navigation(self)
        self.show_page(3)

    def mark_reference_pending(self, *args):
        if not hasattr(self, "_post_baseline"):
            return
        if hasattr(self, "preview_state"):
            self.invalidate_post_preview("有尚未应用的参数；请先应用，再刷新预览。")
        self.update_pending_navigation()

    def update_pending_navigation(self):
        if not hasattr(self, "_post_baseline"):
            return
        pending = set()
        for panel in self.reference_panels:
            panel.update_state()
            if panel.pending():
                pending.add({"spec": 4, "text": 4, "commerce": 8}[panel.kind])
        try:
            if self.thread_values() != self._thread_baseline:
                pending.add(6)
        except Exception:
            pending.add(6)
        try:
            current = self.post_values().to_dict()
            current["thread_plans"] = self._post_baseline["thread_plans"]
            if current != self._post_baseline:
                pending.add(7)
        except Exception:
            pending.add(7)
        for nav, (index, name, _, tip) in zip(self.reference_nav, ROUTES, strict=True):
            nav.setText(name + (" · 未应用" if index in pending else ""))
            nav.setToolTip(tip + ("；有未应用更改" if index in pending else ""))
        if not pending and self.preview_state.text().startswith("有尚未应用的参数"):
            self.preview_state.setText("页面参数已恢复或应用；刷新预览以核对当前结果。")

    def file_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        page_heading(layout, "作品", "从已有图纸开始，确认作品信息与对应原图。")
        body = QHBoxLayout()
        body.setSpacing(24)
        details = QWidget()
        dl = QVBoxLayout(details)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(18)
        form = QFormLayout()
        form.setVerticalSpacing(14)
        self.ref_title, self.ref_code = QLineEdit(), QLineEdit()
        for field, old in ((self.ref_title, self.title_edit), (self.ref_code, self.code_edit)):
            field.setMaximumWidth(660)
            field.textChanged.connect(old.setText)
            field.editingFinished.connect(self.apply_fields)
        form.addRow("作品名称", self.ref_title)
        form.addRow("客户货号", self.ref_code)
        dl.addLayout(form)
        source = QFrame()
        source.setObjectName("sourceCard")
        source.setMaximumWidth(780)
        sl = QVBoxLayout(source)
        sl.setContentsMargins(20, 20, 20, 20)
        sl.setSpacing(16)
        sl.addWidget(text("图纸与对应原图"))
        import_action = action("导入 XSD…", self.import_reference_pattern)
        row(
            sl,
            import_action,
            action("选择对应原图…", self.choose_image),
            menu_button(
                "更多",
                (
                    ("使用 XSD 文件名作为货号", self.accept_code_suggestion),
                    ("查找同名对应图…", self.discover_companions),
                ),
            ),
        )
        import_action.setProperty("role", "primary")
        self.ref_input_report = text("尚未导入图纸")
        self.ref_input_report.setProperty("role", "muted")
        sl.addWidget(self.ref_input_report)
        self.ref_dimensions = text("")
        self.ref_dimensions.setProperty("role", "heading")
        sl.addWidget(self.ref_dimensions)
        self.source_confirm = QCheckBox("已核对：对应原图与 XSD 为同一作品")
        self.source_confirm.toggled.connect(self.confirm_correspondence)
        sl.addWidget(self.source_confirm)
        self.ref_source_details = text("")
        sl.addWidget(disclosure("查看来源详情", self.ref_source_details))
        dl.addWidget(source)
        note = text("返单可从「文件」打开作品或作品设置。输出目录在「检查与导出」设置。")
        note.setProperty("role", "muted")
        dl.addWidget(note)
        next_button = action("设置印花与图纸", lambda: self.show_page(4))
        next_button.setProperty("role", "primary")
        next_button.setText("继续设置图纸")
        next_button.setProperty("role", "quiet")
        row(dl, next_button)
        dl.addStretch()
        from .desktop import Canvas

        preview = QFrame()
        preview.setObjectName("overviewCard")
        preview.setMaximumHeight(510)
        pv = QVBoxLayout(preview)
        pv.setContentsMargins(20, 20, 20, 20)
        title = text("作品概览")
        title.setProperty("role", "heading")
        pv.addWidget(title)
        self.work_overview = Canvas()
        self.work_overview.setMinimumSize(260, 280)
        self.work_overview.setMaximumHeight(460)
        self.work_overview.setDragMode(Canvas.DragMode.ScrollHandDrag)
        pv.addWidget(self.work_overview, 1)
        caption = text("当前针格母版 · 导出尺寸与符号请在检查页核对")
        caption.setProperty("role", "muted")
        pv.addWidget(caption)
        body.addWidget(details, 3)
        body.addWidget(preview, 2)
        body.setAlignment(preview, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(body, 1)

        return page

    def spec_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        page_heading(layout, "印花与图纸", "调整实际尺寸、阅读图纸和标题页脚。")
        self.print_tabs = QTabBar()
        self.print_tabs.setExpanding(False)
        for title in ("尺寸与布料", "阅读图纸", "标题与页脚", "预设"):
            self.print_tabs.addTab(title)
        self.spec_panel = EditorPanel(self, "spec")
        self.reference_panels.append(self.spec_panel)
        self.print_stack = QStackedWidget()
        self.print_stack.addWidget(self.spec_panel)
        layout.addWidget(self.print_tabs)
        layout.addWidget(self.print_stack)
        self.print_tabs.currentChanged.connect(self.select_print_section)
        return page

    def select_print_section(self, index):
        if index == 2:
            self.print_stack.setCurrentIndex(1)
        else:
            self.print_stack.setCurrentIndex(0)
            self.spec_panel.editor.tabs.setCurrentIndex({0: 0, 1: 1, 3: 3}.get(index, 0))

    def text_page(self):
        self.text_panel = EditorPanel(self, "text")
        self.reference_panels.append(self.text_panel)
        self.print_stack.addWidget(self.text_panel)
        self.threads_page = QWidget()
        form = QVBoxLayout(self.threads_page)
        form.addWidget(
            text("各 CT 独立输出配线 Excel。每色米数向上取整；每米针数需按材料规则确认。")
        )
        self.thread_table = QTableWidget(0, 4)
        self.thread_table.verticalHeader().hide()
        self.thread_table.verticalHeader().setDefaultSectionSize(40)
        self.thread_table.setShowGrid(False)
        self.thread_table.setAlternatingRowColors(True)
        self.thread_table.setHorizontalHeaderLabels(["CT", "每米针数", "系数来源", "已确认"])
        self.thread_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.thread_empty = text("还没有配线规则。添加布号和厂家确认的系数后，可输出配线表。")
        self.thread_empty.setProperty("role", "muted")
        form.addWidget(self.thread_empty)
        form.addWidget(self.thread_table)
        row(
            form,
            action("添加规则", self.add_thread_row),
            action("移除选中", self.remove_thread_row),
            action("应用配线规则", self.apply_thread_rows),
        )
        self.thread_table.itemChanged.connect(self.mark_reference_pending)
        return QWidget()  # Reserved old route; show_page redirects to print text tab.

    def symbol_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        page_heading(layout, "符号与配线", "选择符号库，完成线色分配与材料规则。")
        tabs = QTabWidget()
        layout.addWidget(tabs)
        mapping = QWidget()
        layout = QVBoxLayout(mapping)
        tabs.addTab(mapping, "符号分配")
        tabs.addTab(self.threads_page, "配线规则")
        self.material_tabs = tabs
        row(
            layout,
            action("选择符号库…", self.select_reference_library),
            menu_button(
                "管理符号库",
                (
                    ("编辑 / 重命名当前库…", self.manage_symbols),
                    ("从文件夹导入…", self.import_reference_symbols),
                    ("导入客户资源包…", self.import_resources),
                    (None, None),
                    ("设置符号输出 ICC…", self.import_symbol_icc),
                ),
            ),
        )
        self.ref_library_label = text("")
        layout.addWidget(self.ref_library_label)
        split = QSplitter()
        self.ref_symbols = QListWidget()
        self.ref_symbols.setViewMode(QListWidget.ViewMode.IconMode)
        self.ref_symbols.setObjectName("symbolGallery")
        self.ref_symbols.setIconSize(QSize(40, 40))
        self.ref_symbols.setGridSize(QSize(76, 82))
        self.ref_symbols.setSpacing(6)
        self.ref_symbols.setMovement(QListWidget.Movement.Static)
        self.ref_symbols.setUniformItemSizes(True)
        self.ref_symbols.setWordWrap(False)
        self.ref_symbols.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.ref_symbols.setMinimumWidth(260)
        self.ref_symbols.currentItemChanged.connect(self.show_reference_symbol)
        gallery = QWidget()
        gl = QVBoxLayout(gallery)
        gl.setContentsMargins(0, 0, 12, 0)
        self.ref_symbol_search = QLineEdit()
        self.ref_symbol_search.setPlaceholderText("查找符号名称或编号…")
        self.ref_symbol_search.setClearButtonEnabled(True)
        self.ref_symbol_search.textChanged.connect(self.filter_reference_symbols)
        gl.addWidget(self.ref_symbol_search)
        gl.addWidget(self.ref_symbols, 1)
        split.addWidget(gallery)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 0, 0, 0)
        selection = QHBoxLayout()
        selection.setSpacing(16)
        self.ref_symbol_zoom = QLabel("符号")
        self.ref_symbol_zoom.setFixedSize(72, 72)
        self.ref_symbol_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        selection.addWidget(self.ref_symbol_zoom)
        info = QVBoxLayout()
        self.ref_symbol_name = text("选择符号与线色")
        info.addWidget(self.ref_symbol_name)
        hint = text("先选左侧符号，再选下方线色进行分配。")
        hint.setProperty("role", "muted")
        info.addWidget(hint)
        selection.addLayout(info, 1)
        self.ref_symbol_actual = QLabel()
        self.ref_symbol_actual.setFixedSize(48, 48)
        self.ref_symbol_actual.setAlignment(Qt.AlignmentFlag.AlignCenter)
        selection.addWidget(self.ref_symbol_actual)
        rl.addLayout(selection)
        self.ref_mapping = QTableWidget(0, 4)
        self.ref_mapping.setHorizontalHeaderLabels(["线色", "针数", "符号", "绑定方式"])
        self.ref_mapping.verticalHeader().hide()
        self.ref_mapping.verticalHeader().setDefaultSectionSize(36)
        self.ref_mapping.setShowGrid(False)
        self.ref_mapping.setAlternatingRowColors(True)
        self.ref_mapping.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ref_mapping.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ref_mapping.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        rl.addWidget(self.ref_mapping)
        split.addWidget(right)
        split.setSizes([420, 580])
        layout.addWidget(split, 1)
        self.keep_manual = QCheckBox("自动匹配时保留手动绑定（包含交换的两色）")
        self.keep_manual.setChecked(True)
        layout.addWidget(self.keep_manual)
        row(
            layout,
            action("分配给所选线色", self.bind_reference_symbol),
            action("自动匹配…", self.auto_reference_symbols),
        )
        layout.addWidget(
            disclosure(
                "匹配规则与打印说明",
                text(
                    "保留手动项后，按针数从多到少，选择着墨色最接近的未使用符号。"
                    "同距离时按线号与库顺序稳定处理。屏幕预览不能替代 14/16CT 打印可读性验收。"
                ),
            )
        )
        return page

    def post_page(self):
        from stitchpress.core.commerce import TARGETS
        from stitchpress.platform.settings import load_settings as load_local

        from .desktop import Canvas

        page = QWidget()
        layout = QVBoxLayout(page)
        page_heading(layout, "检查与导出", "先查看实际预览，再核对并输出所需文件。")
        self.post_tabs = QTabWidget()
        layout.addWidget(self.post_tabs, 1)
        preview = QWidget()
        pl = QVBoxLayout(preview)
        self.post_preview_target = QComboBox()
        for key in ("production", "whole", "pages", "symbols", "card", "effect"):
            self.post_preview_target.addItem(TARGET_NAMES[key], key)
        for key in TARGETS:
            from stitchpress.core.commerce import CONTENTS

            name = (
                "详情 · " + CONTENTS[key.removeprefix("detail-")]
                if key.startswith("detail-")
                else "方图 " + key.rsplit("-", 1)[-1]
            )
            self.post_preview_target.addItem(name, key)
        self.post_preview_number = QSpinBox()
        self.post_preview_number.setRange(1, 200)
        self.post_preview_number.setPrefix("第 ")
        self.post_preview_number.setSuffix(" 页")
        self.post_preview_number.setAccessibleName("预览页码")
        self.post_preview_number.setMaximumWidth(110)
        self.post_preview_number.setEnabled(False)
        row(
            pl,
            self.post_preview_target,
            self.post_preview_number,
            action("刷新预览", self.preview_reference_output),
        )
        self.post_preview_canvas = Canvas()
        self.post_preview_canvas.setMinimumHeight(210)
        self.post_preview_stack = QStackedWidget()
        self.post_preview_empty = QLabel(
            "选择一个输出目标，点击“刷新预览”\n这里会显示实际文件的渲染结果"
        )
        self.post_preview_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.post_preview_empty.setProperty("role", "emptyState")
        self.post_preview_stack.addWidget(self.post_preview_empty)
        self.post_preview_stack.addWidget(self.post_preview_canvas)
        pl.addWidget(self.post_preview_stack, 1)
        self.preview_state = text("选择目标并刷新预览。更改参数后需要重新生成。")
        pl.addWidget(self.preview_state)
        self.post_actual = action("100% 像素", self.post_preview_canvas.actual_pixels)
        row(
            pl,
            self.post_actual,
            action("适应窗口", self.post_preview_canvas.fit),
            action("放大", lambda: self.post_preview_canvas.zoom(1.2)),
            action("缩小", lambda: self.post_preview_canvas.zoom(1 / 1.2)),
        )
        self.post_tabs.addTab(preview, "实际预览")
        options = QWidget()
        ol = QVBoxLayout(options)
        self.post_targets = {}
        grid = QGridLayout()
        for index, (key, name) in enumerate(TARGET_NAMES.items()):
            item = QCheckBox(name)
            self.post_targets[key] = item
            grid.addWidget(item, index // 2, index % 2)
            item.toggled.connect(self.mark_reference_pending)
        ol.addLayout(grid)
        self.post_folder = QLineEdit(
            load_local().get("reference_output_directory")
            or str(Path.home() / "Documents" / "StitchPress")
        )
        self.post_folder.setReadOnly(True)
        row(ol, self.post_folder, action("选择输出目录…", self.choose_post_folder))
        self.post_demo = QCheckBox("草稿输出（示例符号可用，文件标记 DRAFT）")
        self.post_demo.setChecked(True)
        ol.addWidget(self.post_demo)
        advanced = QWidget()
        form = QFormLayout(advanced)
        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(80, 100)
        self.tiff_container = QComboBox()
        self.tiff_container.addItem("Classic TIFF", "classic")
        self.tiff_container.addItem("BigTIFF", "bigtiff")
        self.bigtiff_confirm = QCheckBox("已确认目标软件支持 BigTIFF")
        self.effect_cell = QSpinBox()
        self.effect_cell.setRange(6, 36)
        for name, widget in (
            ("JPEG 质量", self.jpeg_quality),
            ("TIFF 容器", self.tiff_container),
            ("兼容确认", self.bigtiff_confirm),
            ("点图每格像素", self.effect_cell),
        ):
            form.addRow(name, widget)
        ol.addWidget(disclosure("高级输出设置", advanced))
        ol.addWidget(text("每次生成独立目录。正式输出需客户符号库、CMYK ICC 与本次核对。"))
        ol.addStretch()
        options_scroll = QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setWidget(options)
        options_page = QWidget()
        options_layout = QVBoxLayout(options_page)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.addWidget(options_scroll, 1)
        row(options_layout, action("应用输出设置", self.apply_post_options))
        self.post_tabs.addTab(options_page, "输出设置")
        self.post_summary = text("尚未检查当前输出计划。")
        self.post_summary.setProperty("role", "muted")
        layout.addWidget(self.post_summary)
        generate = self.post_generate = action("生成所选文件…", self.export_reference_output)
        generate.setProperty("role", "primary")
        row(
            layout,
            action("检查尺寸与文件", self.plan_reference_output),
            action("确认本次核对…", self.review_reference_output),
            generate,
        )
        row(
            layout,
            menu_button(
                "结果与检查详情",
                (
                    ("打开上次输出目录", self.open_post_folder),
                    ("查看检查详情…", self.show_post_plan_details),
                ),
            ),
        )
        for spin in (self.jpeg_quality, self.effect_cell):
            spin.valueChanged.connect(self.mark_reference_pending)
        self.tiff_container.currentIndexChanged.connect(self.mark_reference_pending)
        self.bigtiff_confirm.toggled.connect(self.mark_reference_pending)
        self.post_preview_target.currentIndexChanged.connect(self.preview_target_changed)
        self.post_preview_number.valueChanged.connect(self.preview_target_changed)
        return page

    def invalidate_post_preview(self, message):
        self.preview_state.setText(message)
        self.post_preview_empty.setText(message + "\n预览完成后会显示在这里")
        self.post_preview_stack.setCurrentIndex(0)

    def preview_target_changed(self, *args):
        paged = self.post_preview_target.currentData() in {"pages", "symbols", "card"}
        self.post_preview_number.setEnabled(paged)
        if not paged:
            self.post_preview_number.setValue(1)
        self.invalidate_post_preview("目标已切换 · 刷新预览查看当前文件")

    def commerce_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        page_heading(layout, "商品图片", "选择一套风格，将作品整理为完整的详情图与方图。")
        self.commerce_panel = EditorPanel(self, "commerce")
        self.reference_panels.append(self.commerce_panel)
        layout.addWidget(self.commerce_panel)
        row(
            layout,
            action("图形化挑选场景 / 相框…", self.commerce_gallery),
            action("导出 JPG 组 / 单张…", self.export_reference_commerce),
        )
        return page

    def open_commerce_preview(self):
        self.post_preview_target.setCurrentIndex(
            self.post_preview_target.findData(
                f"detail-scene-{min(self.commerce_panel.editor.tabs.currentIndex(), 2) + 1}"
            )
        )
        if self.post_preview_target.currentIndex() < 0:
            self.post_preview_target.setCurrentIndex(6)
        self.post_tabs.setCurrentIndex(0)
        self.show_page(7)
        self.preview_reference_output()

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
            for panel in self.reference_panels:
                if panel.pending():
                    index = {"spec": 4, "text": 5, "commerce": 8}[panel.kind]
                    self.show_page(index)
                    if panel.kind == "spec":
                        self.print_tabs.setCurrentIndex(0)
                    raise StitchPressError("已定位到未应用的设置；请应用更改或恢复本页，再继续。")
            self.show_page(6 if self.thread_values() != self._thread_baseline else 7)
            if self.pages.currentIndex() == 6:
                self.material_tabs.setCurrentIndex(1)
            else:
                self.post_tabs.setCurrentIndex(1)
            raise StitchPressError("已定位到未应用的设置；请应用更改后再继续。")
        return True

    def refresh_reference_pages(self):
        if not hasattr(self, "reference_nav"):
            return
        if getattr(self, "_overview_sha", None) != self.design.content_sha:
            self.work_overview.set_design(self.design, fit=True)
            self._overview_sha = self.design.content_sha
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
            f"图纸  {record.get('filename', '当前为内置示例，请导入自己的 XSD')}\n"
            f"原图  {source.get('filename', source.get('asset', '尚未添加'))}"
        )
        self.ref_dimensions.setText(
            f"{self.design.width} × {self.design.height} 格    "
            f"{sum(self.design.counts):,} 针    {sum(n > 0 for n in self.design.counts)} 色"
        )
        self.ref_source_details.setText(f"原稿 SHA256：{record.get('sha256', '尚无记录')}")
        for panel in self.reference_panels:
            panel.sync()
        project_changed = self.reference_project != self.draft.project_id
        if project_changed or not self._post_or_threads_pending():
            options = PostSettings(**self.draft.entities.get("post", {}))
            self.thread_table.setRowCount(0)
            self.thread_empty.setVisible(not options.thread_plans)
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
        self.update_pending_navigation()
        current = post_fingerprint(self.draft)
        if getattr(self, "_post_plan_sha", None) != current:
            self.post_summary.setText("当前参数尚未检查；请检查尺寸与文件。")
        if getattr(self, "_reference_preview_sha", None) != current:
            self.invalidate_post_preview("当前设置尚未预览 · 点击刷新预览")

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
        self.thread_empty.hide()
        for column, key in enumerate(("ct", "stitches_per_metre", "source_note")):
            self.thread_table.setItem(index, column, QTableWidgetItem(plan.get(key, "")))
        confirmed = QCheckBox("已核对")
        confirmed.setChecked(plan.get("confirmed", False))
        self.thread_table.setCellWidget(index, 3, confirmed)
        confirmed.toggled.connect(self.mark_reference_pending)
        self.mark_reference_pending()

    def remove_thread_row(self):
        index = self.thread_table.currentRow()
        if index >= 0:
            self.thread_table.removeRow(index)
            self.thread_empty.setVisible(self.thread_table.rowCount() == 0)
            self.mark_reference_pending()

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
            dialog = QMessageBox(self)
            dialog.setWindowTitle("图纸未导入")
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setText("暂时无法导入这份图纸")
            dialog.setInformativeText(str(exc) + "\n当前作品保持不变。")
            dialog.setDetailedText("文件：" + Path(path).name)
            dialog.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
            dialog.exec()

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
            f"{library.name} · {len(library.symbols)} 个符号 · "
            + ("已带 CMYK ICC" if library.icc else "无 ICC，仅草稿")
        )
        self.ref_symbols.clear()
        for symbol in library.symbols:
            rgb = screen_rgb(symbol.image(), library.icc)
            label = (
                str(symbol.external_id)
                if symbol.key.startswith("int:")
                else symbol.key.removeprefix("str:")
            )
            thumbnail = QPixmap.fromImage(ImageQt(rgb)).scaled(
                40,
                40,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            item = QListWidgetItem(QIcon(thumbnail), label)
            item.setToolTip(f"符号 {label} · 原始标识 {symbol.key}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
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
                    (
                        mapping[color.key][4:]
                        if mapping[color.key].startswith("int:")
                        else mapping[color.key].removeprefix("str:")
                    ),
                    "手动" if color.key in manual else "自动",
                )
            ):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, color.key)
                if col == 2:
                    symbol = library.by_key[mapping[color.key]]
                    rgb = screen_rgb(symbol.image(), library.icc)
                    item.setIcon(QIcon(QPixmap.fromImage(ImageQt(rgb))))
                    item.setToolTip(mapping[color.key])
                    rgb.close()
                self.ref_mapping.setItem(row_number, col, item)
        self.filter_reference_symbols(self.ref_symbol_search.text())

    def filter_reference_symbols(self, query):
        query = query.strip().casefold()
        for i in range(self.ref_symbols.count()):
            item = self.ref_symbols.item(i)
            item.setHidden(query not in (item.text() + " " + item.toolTip()).casefold())

    def show_reference_symbol(self, current, previous=None):
        if not current:
            return
        library = library_from_assets(self.draft.entities["library"], self.draft.assets)
        image = screen_rgb(
            library.by_key[current.data(Qt.ItemDataRole.UserRole)].image(), library.icc
        )
        self.ref_symbol_zoom.setPixmap(
            QPixmap.fromImage(ImageQt(image)).scaled(
                56,
                56,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )
        self.ref_symbol_zoom.setToolTip(current.toolTip())
        self.ref_symbol_name.setText("当前符号 · " + current.text())
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
                    "其他产物需重新生成。\n"
                    f"符号 {mapping[key].partition(':')[2]} ↔ {chosen.partition(':')[2]}",
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
                    f"按颜色接近程度匹配：保留 {len(manual)} 项手动绑定，"
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
        labels = [f"{records[key].get('name', '未命名符号库')} · {key[:12]}" for key in keys]
        chosen, ok = QInputDialog.getItem(
            self,
            "当前作品保存的符号库",
            "选择库版本（重新匹配；旧映射保留于历史）",
            labels,
            0,
            False,
        )
        if ok:
            try:
                chosen = keys[labels.index(chosen)]
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

    def show_post_plan_details(self):
        QMessageBox.information(self, "输出检查详情", getattr(self, "_plan_detail", "尚未检查。"))

    def show_post_plan(self, result):
        from decimal import Decimal

        art_size = " × ".join(f"{Decimal(v):.3f}" for v in result["artwork_cm"])
        cloth_size = " × ".join(f"{Decimal(v):.3f}" for v in result["cloth_cm"])
        self._post_plan_sha = post_fingerprint(self.draft)
        paper = result["paper_px"] or ("未选查图", "—")
        self._plan_detail = (
            f"{result['stitches']:,} 针 · {result['colors']} 色 · "
            f"画心 {art_size} cm · "
            f"含留边 {cloth_size} cm\n"
            f"生产 {result['production_px'][0]} × {result['production_px'][1]} px / "
            f"{result['production_dpi']} DPI\n"
            f"查图 {paper[0]} × {paper[1]} px / "
            f"{result['reference_dpi']} DPI · "
            f"{result['page_count']} 页 · 每页容量 {result['capacity']}\n"
            f"配线 CT：{', '.join(result['ct_plans']) or '未配置'} "
            f"· 电商 {result['commerce_files']} 张 "
            f"· 工作磁盘预留约 {result['estimate_bytes'] / 1024**2:.1f} MiB"
        )

        self.post_summary.setText(
            f"{result['stitches']:,} 针 · {result['colors']} 色 · "
            f"整布 {cloth_size} cm\n"
            f"生产 {result['production_dpi']} DPI · 查图 {result['reference_dpi']} DPI / "
            f"{result['page_count']} 页 · 电商 {result['commerce_files']} 张"
        )
        self.post_summary.setToolTip(self._plan_detail)

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
                self.invalidate_post_preview("正在生成实际预览 · 可在底部取消")
        except Exception as exc:
            self.error(exc)

    def show_post_preview(self, result):
        from PIL import Image

        checks = result["checks"]
        if self.reference_pending():
            self.invalidate_post_preview("有未应用的更改 · 应用后重新预览")
            return
        if (
            checks.get("target") != self.post_preview_target.currentData()
            or checks.get("page", 1) != self.post_preview_number.value()
        ):
            self.invalidate_post_preview("当前目标已改变 · 请刷新当前目标")
            return
        size = " × ".join(str(v) for v in checks.get("preview_size", ()))
        title = TARGET_NAMES.get(checks.get("target"), "商品图片")
        with Image.open(result["preview"]) as source:
            self.post_preview_canvas.set_image(source.copy(), fit=True)
        self._reference_preview_sha = post_fingerprint(self.draft)
        note = "完整缩略概览 · 非生产原像素 · " if checks.get("overview") else ""
        self.preview_state.setText(f"{note}{title} · {size} px · 第 {checks.get('page', 1)} 页")
        self.post_actual.setText("100% 概览像素" if checks.get("overview") else "100% 原像素")
        self.post_preview_stack.setCurrentIndex(1)
        self.post_tabs.setCurrentIndex(0)

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
            from stitchpress.core.identity import safe_filename, valid_directory_name

            name, ok = QInputDialog.getText(
                self,
                "新版本输出目录",
                "在父目录下建立新目录（已有目录不会覆盖）",
                text=safe_filename(self.draft.entities["metadata"]["code"]) + "-" + uuid4().hex[:6],
            )
            if not ok or not name.strip():
                return
            if not valid_directory_name(name):
                raise StitchPressError(
                    "目录名不能包含路径分隔符或特殊字符，不能以空格或句点结尾，请使用简短名称。"
                )
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
