"""Native workspace composition, kept separate from the project coordinator."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .appearance import bind_icon


def label(text="", role="", wrap=False):
    item = QLabel(text)
    item.setProperty("role", role)
    item.setWordWrap(wrap)
    return item


def frame(name, *, horizontal=False, margins=(16, 12, 16, 12), spacing=10):
    widget = QFrame()
    widget.setObjectName(name)
    layout = QHBoxLayout(widget) if horizontal else QVBoxLayout(widget)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return widget, layout


def button(w, text, callback, icon="", role="", checkable=False, toggle_off=False):
    if checkable:
        # Accessibility toggles change checked state without emitting clicked().
        item = QPushButton(text)
        item.toggled.connect(lambda checked: callback() if checked or toggle_off else None)
    else:
        item = w.button(text, callback)
    item.setProperty("role", role)
    item.setCheckable(checkable)
    if icon:
        bind_icon(item, icon)
    return item


def build_workspace(w, canvas_type):
    root = QWidget()
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    w.setCentralWidget(root)

    header, hl = frame("documentHeader", horizontal=True, margins=(20, 12, 20, 12))
    hl.addWidget(label("StitchPress", "brand"))
    hl.addWidget(label("绣印工坊", "muted"))
    w.document_title = label("", "document")
    w.document_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    hl.addWidget(w.document_title, 1)
    w.document_state = label("示例 · 未保存", "badge")
    hl.addWidget(w.document_state)

    file_button = QToolButton()
    file_button.setText("母版")
    file_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    file_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    bind_icon(file_button, "mdi6.file-document-outline")
    file_menu = QMenu(file_button)
    for text, callback, shortcut in (
        ("新建示例", w.new_demo, QKeySequence.StandardKey.New),
        ("打开作品…", w.open_project, QKeySequence.StandardKey.Open),
        ("保存", w.save, QKeySequence.StandardKey.Save),
        ("另存为…", w.save_as, QKeySequence.StandardKey.SaveAs),
        ("导入 XSD / OXS / JSON…", w.import_design, None),
        ("导入客户资源…", w.import_resources, None),
    ):
        action = QAction(text, w)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(callback)
        file_menu.addAction(action)
        w.addAction(action)
    file_menu.addSeparator()
    for title, actions in (
        (
            "制版与查图",
            (
                ("尺寸、查图与预设…", w.specification_settings),
                ("顶底栏文字与占位符…", w.edit_text_layers),
                ("查图分页与原像素预览…", w.plan_preview),
                ("母版数字检查…", w.inspect_master),
                ("导出全针 OXS…", w.export_oxs_file),
            ),
        ),
        (
            "符号与资源",
            (
                ("符号库管理…", w.manage_symbols),
                ("重新自动分配符号…", w.reassign_symbols),
                ("导出符号速查卡…", w.export_symbol_card),
                ("配线规则与多CT…", w.material_settings),
                ("导入源图输入 ICC…", w.import_input_icc),
                ("清除源图输入 ICC 覆盖", w.clear_input_icc),
                ("清除当前蒙版标注", w.clear_annotations),
            ),
        ),
        (
            "版本与恢复",
            (
                ("恢复未保存草稿…", w.recover_draft),
                ("恢复保存历史…", w.restore_version),
            ),
        ),
        (
            "批量与交付",
            (
                ("批量导出已保存作品…", w.batch_export),
                ("按需导出补件…", w.export_selected),
                ("商品模板设置…", w.edit_commerce),
                ("商品素材预览…", w.preview_commerce),
                ("导出商品7+5或单项…", w.export_commerce_files),
                ("恢复本机导出批次…", w.resume_batch),
                ("登记交付/试印/洗水验收…", w.delivery_review),
                ("压缩已核验交付为 ZIP…", w.zip_delivery),
            ),
        ),
    ):
        menu = file_menu.addMenu(title)
        for text, callback in actions:
            menu.addAction(text, callback)
    file_menu.addSeparator()
    file_menu.addAction("AI 服务配置…", w.ai_settings)
    file_menu.addAction("本地反馈与附件…", w.feedback_local)
    file_menu.addAction("使用帮助与当前范围…", w.help_local)
    file_button.setMenu(file_menu)
    hl.addWidget(file_button)
    w.export_nav = button(w, "交付", lambda: w.show_page(2), "mdi6.tray-arrow-up", checkable=True)
    hl.addWidget(w.export_nav)
    w.ai_nav = button(w, "AI 助手", lambda: w.show_page(1), "mdi6.creation", checkable=True)
    hl.addWidget(w.ai_nav)
    w.theme_button = button(w, "白天", w.toggle_theme, "mdi6.white-balance-sunny")
    w.theme_button.setToolTip("切换白天 / 夜间主题；只影响界面，记住本机选择")
    hl.addWidget(w.theme_button)
    hl.addWidget(button(w, "保存", w.save, "mdi6.content-save-outline"))
    hl.addWidget(button(w, "审查并冻结", w.freeze, role="primary"))
    layout.addWidget(header)

    toolbar, tl = frame("viewToolbar", horizontal=True, margins=(20, 8, 20, 8), spacing=8)
    w.workbench_nav = button(w, "制版台", lambda: w.show_page(0), role="quiet", checkable=True)
    w.navigation_group = QButtonGroup(w)
    for nav in (w.workbench_nav, w.ai_nav, w.export_nav):
        w.navigation_group.addButton(nav)
    w.workbench_nav.blockSignals(True)
    w.workbench_nav.setChecked(True)
    w.workbench_nav.blockSignals(False)
    tl.addWidget(w.workbench_nav)
    w.metrics = label("", "muted")
    tl.addWidget(w.metrics)
    tl.addStretch()
    w.view_controls = QWidget()
    vc = QHBoxLayout(w.view_controls)
    vc.setContentsMargins(0, 0, 0, 0)
    vc.setSpacing(5)
    w.tool_group = QButtonGroup(w)
    w.select_tool = button(
        w, "", lambda: w.set_canvas_tool(False), "mdi6.cursor-default-outline", "icon", True
    )
    w.select_tool.setToolTip("选区工具 · 拖动检查或修订针格")
    w.select_tool.setAccessibleName("选区工具")
    w.pan_tool = button(
        w, "", lambda: w.set_canvas_tool(True), "mdi6.hand-back-right-outline", "icon", True
    )
    w.pan_tool.setToolTip("平移工具 · 拖动画布")
    w.pan_tool.setAccessibleName("平移工具")
    w.tool_group.addButton(w.select_tool)
    w.tool_group.addButton(w.pan_tool)
    w.select_tool.blockSignals(True)
    w.select_tool.setChecked(True)
    w.select_tool.blockSignals(False)
    vc.addWidget(w.select_tool)
    vc.addWidget(w.pan_tool)
    for name, scale, tip in (
        ("mdi6.magnify-minus-outline", 1 / 1.2, "缩小画布"),
        ("mdi6.magnify-plus-outline", 1.2, "放大画布"),
    ):
        item = button(
            w, "", lambda checked=False, factor=scale: w.canvas.zoom(factor), name, "icon"
        )
        item.setToolTip(tip)
        item.setAccessibleName(tip)
        vc.addWidget(item)
    w.zoom_label = button(w, "100%", w.actual_pixels, role="quiet")
    w.zoom_label.setToolTip("恢复 100% 像素显示")
    vc.addWidget(w.zoom_label)
    vc.addWidget(button(w, "适应画布", lambda: w.canvas.fit(), "mdi6.fit-to-screen-outline"))
    w.compare_button = button(w, "原图对照", w.toggle_source, checkable=True, toggle_off=True)
    vc.addWidget(w.compare_button)
    w.inspector_button = button(
        w, "规格与转换", w.toggle_inspector, "mdi6.tune-variant", checkable=True, toggle_off=True
    )
    vc.addWidget(w.inspector_button)
    tl.addWidget(w.view_controls)
    layout.addWidget(toolbar)

    w.pages = QStackedWidget()
    layout.addWidget(w.pages, 1)
    workspace = QWidget()
    wl = QVBoxLayout(workspace)
    wl.setContentsMargins(0, 0, 0, 0)
    wl.setSpacing(0)
    w.workspace_splitter = QSplitter()
    wl.addWidget(w.workspace_splitter, 1)
    palette, pl = frame("palettePanel", margins=(14, 18, 14, 12))
    palette.setMinimumWidth(260)
    palette.setMaximumWidth(380)
    ph = QHBoxLayout()
    ph.addWidget(label("线色与符号", "heading"))
    ph.addStretch()
    clear = button(w, "显示全部", w.clear_highlight, role="quiet")
    clear.setToolTip("清除线色高亮，恢复完整母版")
    ph.addWidget(clear)
    pl.addLayout(ph)
    w.palette_search = QLineEdit()
    w.palette_search.setPlaceholderText("搜索线号或颜色名称")
    w.palette_search.setAccessibleName("搜索线色")
    w.palette_search.textChanged.connect(w.filter_palette)
    pl.addWidget(w.palette_search)
    w.palette_count = label("", "muted")
    pl.addWidget(w.palette_count)
    w.table = QTableWidget()
    w.table.setColumnCount(4)
    w.table.setHorizontalHeaderLabels(["", "线色", "针数", "符号"])
    w.table.verticalHeader().hide()
    w.table.setShowGrid(False)
    w.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    w.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    w.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    w.table.setIconSize(QSize(24, 24))
    w.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    for col, size in ((0, 30), (2, 51), (3, 39)):
        w.table.setColumnWidth(col, size)
        w.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
    w.table.itemSelectionChanged.connect(w.highlight)
    pl.addWidget(w.table, 1)
    w.symbol_button = button(w, "调整所选符号", w.change_symbol, "mdi6.swap-horizontal")
    w.symbol_button.setEnabled(False)
    pl.addWidget(w.symbol_button)
    pl.addWidget(label("线色高亮只影响预览，不会修改母版。", "muted", True))
    w.workspace_splitter.addWidget(palette)

    art = QWidget()
    art_layout = QVBoxLayout(art)
    art_layout.setContentsMargins(0, 0, 0, 0)
    art_layout.setSpacing(0)
    w.canvas_splitter = QSplitter()
    w.source_panel, sp = frame("sourcePanel", margins=(0, 0, 0, 0))
    sp.addWidget(label("  对照原图", "muted"))
    w.source_canvas = canvas_type()
    sp.addWidget(w.source_canvas, 1)
    w.source_empty = label("尚未选择对照图\n从「规格与转换」选择图片。", "muted", True)
    w.source_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
    sp.addWidget(w.source_empty, 1)
    w.source_canvas.hide()
    w.source_panel.hide()
    w.canvas_splitter.addWidget(w.source_panel)
    master, ml = frame("masterPanel", margins=(20, 18, 20, 18))
    w.canvas = canvas_type()
    w.canvas.region_selected.connect(w.select_region)
    w.canvas.zoom_changed.connect(lambda value: w.zoom_label.setText(f"{value:.0f}%"))
    ml.addWidget(w.canvas, 1)
    w.canvas_splitter.addWidget(master)
    art_layout.addWidget(w.canvas_splitter, 1)
    w.workspace_splitter.addWidget(art)
    w.inspector = build_inspector(w)
    w.workspace_splitter.addWidget(w.inspector)
    w.inspector.hide()
    w.workspace_splitter.setStretchFactor(1, 1)
    w.workspace_splitter.setSizes([290, 1030, 310])

    dock_wrap = QWidget()
    dw = QVBoxLayout(dock_wrap)
    dw.setContentsMargins(12, 10, 12, 8)
    dock, dl = frame("selectionDock", horizontal=True, margins=(18, 14, 18, 14), spacing=16)
    region_text = QVBoxLayout()
    region_text.addWidget(label("局部审查与修订", "document"))
    w.region_label = label("拖动框选针格，检查关键局部", "muted")
    region_text.addWidget(w.region_label)
    dl.addLayout(region_text)
    dl.addStretch()
    dl.addWidget(label("替换为", "muted"))
    w.patch_color = QComboBox()
    w.patch_color.setMinimumWidth(200)
    w.patch_color.setMaximumWidth(300)
    w.patch_color.setAccessibleName("选区替换线色或留空")
    dl.addWidget(w.patch_color)
    w.patch_button = button(w, "替换针格", w.patch_region, "mdi6.swap-horizontal", "outline")
    dl.addWidget(w.patch_button)
    w.undo_button = button(w, "撤销", w.undo_patch, "mdi6.undo-variant")
    dl.addWidget(w.undo_button)
    mask = QToolButton()
    mask.setText("蒙版")
    mask.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    mask_menu = QMenu(mask)
    mask_menu.addAction("标注主体有针（下次转换）", lambda: w.annotate_region("subject"))
    mask_menu.addAction("标注负空间留空（下次转换）", lambda: w.annotate_region("blank"))
    mask_menu.addAction("保护选区（禁止局部改针）", lambda: w.annotate_region("protected"))
    mask_menu.addSeparator()
    mask_menu.addAction("清除当前蒙版（旧记录保留）", w.clear_annotations)
    mask.setMenu(mask_menu)
    dl.addWidget(mask)
    note = label("区域外保持不变\n白色有针，留空不绣", "muted")
    dl.addWidget(note)
    dw.addWidget(dock)
    wl.addWidget(dock_wrap)
    w.pages.addWidget(workspace)
    w.pages.addWidget(build_ai(w))
    w.pages.addWidget(build_export(w))
    w.statusBar().showMessage("开发试验版 · X 针格模拟，非实绣照片 · 尚未经过生产验收")


def build_inspector(w):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setMinimumWidth(300)
    area.setMaximumWidth(340)
    panel, pl = frame("inspector")
    heading = QHBoxLayout()
    heading.addWidget(label("规格与转换", "heading"))
    heading.addStretch()
    heading.addWidget(button(w, "收起", w.toggle_inspector, role="quiet"))
    pl.addLayout(heading)
    w.title_edit, w.code_edit = QLineEdit(), QLineEdit()
    form = QFormLayout()
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.addRow("作品名称", w.title_edit)
    form.addRow("客户货号", w.code_edit)
    w.sx, w.sy = QLineEdit("0.181"), QLineEdit("0.181")
    form.addRow("横向格距 cm", w.sx)
    form.addRow("纵向格距 cm", w.sy)
    w.margins = {key: QLineEdit("5.430") for key in ("left", "top", "right", "bottom")}
    for text, key in (
        ("左留白 cm", "left"),
        ("上留白 cm", "top"),
        ("右留白 cm", "right"),
        ("下留白 cm", "bottom"),
    ):
        form.addRow(text, w.margins[key])
    pl.addLayout(form)
    for item in (w.title_edit, w.code_edit, w.sx, w.sy, *w.margins.values()):
        item.editingFinished.connect(w.apply_fields)
    w.summary = label("", "muted", True)
    pl.addWidget(w.summary)
    pl.addWidget(button(w, "尺寸、查图与预设…", w.specification_settings, "mdi6.tune-variant"))
    source = QGroupBox("图片 → 针格候选")
    sf = QFormLayout(source)
    sf.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    sf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    w.grid_w, w.grid_h, w.color_limit = QSpinBox(), QSpinBox(), QSpinBox()
    for item, value, limit in (
        (w.grid_w, 160, 1000),
        (w.grid_h, 160, 1000),
        (w.color_limit, 32, 66),
    ):
        item.setRange(1, limit)
        item.setValue(value)
    sf.addRow("针格宽", w.grid_w)
    sf.addRow("针格高", w.grid_h)
    sf.addRow("最多颜色", w.color_limit)
    w.alpha = QComboBox()
    w.alpha.addItems(["alpha <128 留空", "合成白底 / 全部有针"])
    w.alpha.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    w.alpha.setMinimumContentsLength(8)
    w.alpha.setToolTip("阈值处理透明边缘，不把不透明白色当作空白。合成白底模式全格有针。")
    sf.addRow("半透明处理", w.alpha)
    w.source_label = label("尚未选择蓝图；干净平面图可直接转换", "muted", True)
    sf.addRow(w.source_label)
    sf.addRow(button(w, "选择图片…", w.choose_image, "mdi6.image-outline"))
    sf.addRow(button(w, "本地转换候选", w.convert, role="outline"))
    pl.addWidget(source)
    for field in (w.grid_w, w.grid_h, w.color_limit):
        field.valueChanged.connect(w.capture_conversion_options)
    w.alpha.currentIndexChanged.connect(w.capture_conversion_options)
    pl.addStretch()
    area.setWidget(panel)
    return area


def page(w, title, subtitle):
    area = QScrollArea()
    area.setWidgetResizable(True)
    body = QWidget()
    bl = QVBoxLayout(body)
    bl.setContentsMargins(36, 28, 36, 24)
    bl.setSpacing(18)
    bl.addWidget(label(title, "pageTitle"))
    bl.addWidget(label(subtitle, "muted", True))
    area.setWidget(body)
    return area, bl


def build_ai(w):
    area, al = page(w, "AI 制版助手", "整理设计要求与图片分析；模型建议由你决定是否采用。")
    service, sl = frame("pageCard", horizontal=True)
    sl.addWidget(label("DeepSeek Flash · 文字 / 图片理解", "document"))
    sl.addStretch()
    sl.addWidget(button(w, "配置 AI 服务", w.ai_settings, "mdi6.cog-outline"))
    al.addWidget(service)
    brief_card, brief_layout = frame("pageCard")
    brief_layout.addWidget(
        button(w, "设计要求与输入路径…", w.edit_brief, "mdi6.clipboard-text-outline")
    )
    w.brief_summary = label("", "muted", True)
    brief_layout.addWidget(w.brief_summary)
    al.addWidget(brief_card)
    history_row = QHBoxLayout()
    history_row.addWidget(button(w, "调用计划…", w.ai_plan, "mdi6.counter"))
    history_row.addWidget(button(w, "任务记录与恢复…", w.task_inbox, "mdi6.history"))
    history_row.addStretch()
    al.addLayout(history_row)
    al.addWidget(label("本次设计要求", "document"))
    w.prompt = QTextEdit()
    w.prompt.setPlaceholderText("描述必须保留的对象、构图、负空间和允许简化的部分……")
    w.prompt.setMinimumHeight(110)
    w.prompt.setMaximumHeight(160)
    al.addWidget(w.prompt)
    w.ai_include_image = QCheckBox("本次附带当前对照图片，发送到已配置的 AI 服务")
    w.ai_include_image.setChecked(True)
    al.addWidget(w.ai_include_image)
    w.prompt.textChanged.connect(w.capture_creative_prompt)
    w.ai_include_image.toggled.connect(w.capture_creative_prompt)
    row = QHBoxLayout()
    row.addWidget(button(w, "分析设计要求", w.run_ai, "mdi6.creation", "primary"))
    row.addWidget(label("单次调用 · 使用你配置的账号计费", "muted"))
    row.addStretch()
    al.addLayout(row)
    al.addWidget(label("分析结果", "document"))
    w.ai_result = QTextEdit()
    w.ai_result.setReadOnly(True)
    w.ai_result.setMinimumHeight(160)
    w.ai_result.setPlaceholderText("尚未调用。模型建议不会自动修改针格或冻结母版。")
    al.addWidget(w.ai_result, 1)
    al.addWidget(button(w, "将最近分析填入设计要求草稿…", w.analysis_to_brief, role="outline"))
    al.addWidget(label("图像生成 / 编辑：当前仅支持保存配置，待供应商接口接入。", "muted", True))
    return area


def build_export(w):
    area, el = page(w, "导出与交付", "从当前母版生成同源交付文件，交付前请核对规格与审查状态。")
    card, cl = frame("pageCard", margins=(24, 22, 24, 22), spacing=14)
    cl.addWidget(label("N + 6 整包", "heading"))
    cl.addWidget(
        label(
            "生产 TIFF  /  查图  /  PDF  /  配线 Excel  /  母版模拟  /  中英文说明", "muted", True
        )
    )
    w.export_summary = label("", "document", True)
    cl.addWidget(w.export_summary)
    w.export_review = label("", "badge", True)
    cl.addWidget(w.export_review)
    configure_row = QHBoxLayout()
    configure_row.addWidget(button(w, "尺寸与查图设置", w.specification_settings))
    configure_row.addWidget(button(w, "分页与原像素预览", w.plan_preview))
    configure_row.addWidget(button(w, "母版数字检查", w.inspect_master))
    configure_row.addWidget(button(w, "顶底文字", w.edit_text_layers))
    configure_row.addWidget(button(w, "展示模拟预览", w.preview_simulation))
    configure_row.addStretch()
    cl.addLayout(configure_row)
    commerce_row = QHBoxLayout()
    commerce_row.addWidget(button(w, "商品模板设置…", w.edit_commerce))
    commerce_row.addWidget(button(w, "配线规则…", w.material_settings))
    commerce_row.addWidget(button(w, "商品实际预览…", w.preview_commerce))
    commerce_row.addWidget(button(w, "导出7+5或单张…", w.export_commerce_files))
    commerce_row.addStretch()
    cl.addLayout(commerce_row)
    w.demo_mode = QCheckBox("演示模式（不能作为正式生产验收）")
    w.demo_mode.setChecked(True)
    cl.addWidget(w.demo_mode)
    export_row = QHBoxLayout()
    export_row.addWidget(button(w, "选择位置并导出整包", w.export, "mdi6.tray-arrow-up", "primary"))
    export_row.addWidget(button(w, "按需导出补件…", w.export_selected))
    export_row.addStretch()
    cl.addLayout(export_row)
    el.addWidget(card)
    el.addWidget(
        label(
            "示例库只供演示。正式导出需要客户资源及当前母版审核记录。\n"
            "Windows、工厂试印与洗水尚未验证；外部 PDF / Excel 显示另行登记。",
            "muted",
            True,
        )
    )
    task_header = QHBoxLayout()
    task_header.addWidget(label("任务与诊断", "heading"))
    task_header.addStretch()
    w.cancel_button = button(w, "取消当前本地任务", w.cancel_job, role="quiet")
    w.cancel_button.setEnabled(False)
    task_header.addWidget(w.cancel_button)
    el.addLayout(task_header)
    w.log = QTextEdit()
    w.log.setReadOnly(True)
    w.log.setMinimumHeight(150)
    w.log.setPlaceholderText("尚无任务。转换和导出进度将在这里显示。")
    el.addWidget(w.log, 1)
    return area
