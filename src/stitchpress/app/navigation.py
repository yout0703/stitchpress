"""One R1 navigation hierarchy; legacy tools remain explicitly experimental."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .appearance import bind_icon

ROUTES = (
    (3, "作品", "mdi6.file-document-outline", "导入图纸，确认作品与对应原图"),
    (4, "印花与图纸", "mdi6.ruler-square", "设置尺寸、阅读图纸和标题页脚"),
    (6, "符号与配线", "mdi6.palette-outline", "分配符号，设置各布号配线规则"),
    (8, "商品图片", "mdi6.image-multiple-outline", "配置展示效果、场景与商品文案"),
    (7, "检查与导出", "mdi6.tray-arrow-up", "预览实际文件，核对后输出"),
)


def menu_button(title, items):
    button = QToolButton()
    button.setText(title)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    button.setProperty("role", "quiet")
    menu = QMenu(button)
    for label, callback in items:
        if label is None:
            menu.addSeparator()
        else:
            menu.addAction(label, callback)
    button.setMenu(menu)
    return button


def disclosure(title, content):
    panel = QWidget()
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    toggle = QPushButton(title)
    toggle.setProperty("role", "quiet")
    toggle.setCheckable(True)
    toggle.setAccessibleName(title + "，展开或收起")
    toggle.toggled.connect(content.setVisible)
    content.hide()
    layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addWidget(content)
    return panel


def build_menus(w):
    bar = w.menuBar()
    bar.setNativeMenuBar(False)
    files = bar.addMenu("文件")
    for title, callback, shortcut in (
        ("导入全针 XSD…", w.import_reference_pattern, None),
        ("打开作品…", w.open_project, QKeySequence.StandardKey.Open),
        ("保存作品", w.save, QKeySequence.StandardKey.Save),
        ("另存作品…", w.save_as, QKeySequence.StandardKey.SaveAs),
    ):
        item = QAction(title, w)
        if shortcut:
            item.setShortcut(shortcut)
        item.triggered.connect(callback)
        files.addAction(item)
        w.addAction(item)
    files.addSeparator()
    files.addAction("打开作品设置 JSON…", w.open_reference_settings)
    files.addAction("导出作品设置 JSON…", w.save_reference_settings)
    files.addSeparator()
    files.addAction("恢复未保存草稿…", w.recover_draft)
    files.addAction("恢复保存历史…", w.restore_version)
    files.addAction("新建示例", w.new_demo)
    view = bar.addMenu("查看")
    for number, (index, title, _, _) in enumerate(ROUTES, 1):
        item = view.addAction(title, lambda checked=False, i=index: w.show_page(i))
        item.setShortcut(QKeySequence(f"Ctrl+{number}"))
        w.addAction(item)
    view.addSeparator()
    view.addAction("切换白天 / 夜间", w.toggle_theme)
    view.addAction("本地任务记录…", w.post_task_history)
    extension = bar.addMenu("扩展（实验）")
    extension.addAction("针格编辑台", lambda: w.show_page(0))
    extension.addAction("AI 助手", lambda: w.show_page(1))
    extension.addAction("AI 服务配置…", w.ai_settings)
    extension.addAction("专用交付", lambda: w.show_page(2))
    for title, actions in (
        (
            "旧格式与资源工具",
            (
                ("导入 XSD / OXS / JSON…", w.import_design),
                ("导出全针 OXS…", w.export_oxs_file),
                ("导出符号卡 PDF / PNG…", w.export_symbol_card),
                ("导入源图输入 ICC…", w.import_input_icc),
                ("清除输入 ICC 覆盖", w.clear_input_icc),
            ),
        ),
        (
            "蓝图与修订",
            (
                ("针格候选与修订比较…", w.candidate_browser),
                ("制版流程与检查点…", w.workflow_board),
                ("导入蓝图候选…", w.import_blueprint_candidate),
                ("蓝图版本与比较…", w.blueprint_browser),
                ("同画幅局部图像合成…", w.local_image_candidate),
            ),
        ),
        (
            "专用交付工具",
            (
                ("批量导出已保存作品…", w.batch_export),
                ("按需导出补件…", w.export_selected),
                ("恢复本机导出批次…", w.resume_batch),
                ("登记交付/试印/洗水验收…", w.delivery_review),
                ("压缩已核验交付为 ZIP…", w.zip_delivery),
                ("旧配线交付规则…", w.material_settings),
            ),
        ),
    ):
        menu = extension.addMenu(title)
        for name, callback in actions:
            menu.addAction(name, callback)
    help_menu = bar.addMenu("帮助")
    help_menu.addAction("使用帮助与当前范围…", w.help_local)
    help_menu.addAction("本地反馈与附件…", w.feedback_local)


def install_navigation(w):
    root = w.centralWidget().layout()
    root.removeWidget(w.view_toolbar)
    # Canvas tools belong only to the experimental canvas, never the other pages.
    w.pages.widget(0).layout().insertWidget(0, w.view_toolbar)
    w.workbench_nav.hide()
    root.removeWidget(w.pages)
    shell = QWidget()
    horizontal = QHBoxLayout(shell)
    horizontal.setContentsMargins(0, 0, 0, 0)
    horizontal.setSpacing(0)
    rail = QFrame()
    rail.setObjectName("navigationRail")
    rail.setFixedWidth(196)
    layout = QVBoxLayout(rail)
    layout.setContentsMargins(12, 22, 12, 16)
    layout.setSpacing(8)
    title = QLabel("制作工作台")
    title.setProperty("role", "muted")
    layout.addWidget(title)
    w.reference_nav = []
    for index, name, icon, tip in ROUTES:
        nav = QPushButton(name)
        nav.setProperty("role", "navigation")
        nav.setProperty("pageIndex", index)
        nav.setCheckable(True)
        nav.setToolTip(tip)
        bind_icon(nav, icon)
        nav.toggled.connect(lambda checked, i=index: w.show_page(i) if checked else None)
        w.navigation_group.addButton(nav)
        w.reference_nav.append(nav)
        layout.addWidget(nav)
    layout.addStretch()
    tasks = QPushButton("任务记录")
    tasks.setProperty("role", "quiet")
    bind_icon(tasks, "mdi6.history")
    tasks.clicked.connect(w.post_task_history)
    layout.addWidget(tasks)
    layout.addWidget(w.theme_button)
    help_button = QPushButton("帮助与反馈")
    help_button.setProperty("role", "quiet")
    help_button.clicked.connect(w.help_local)
    layout.addWidget(help_button)
    horizontal.addWidget(rail)
    horizontal.addWidget(w.pages, 1)
    root.addWidget(shell, 1)
    w.navigation_rail = rail
    w.cancel_button.setText("取消任务")
    w.statusBar().addPermanentWidget(w.cancel_button)
