"""Visual symbol selection and a bounded cache for the active library only."""

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from stitchpress.core.identity import digest
from stitchpress.core.render import screen_rgb
from stitchpress.core.resources import library_from_assets


class SymbolArtwork:
    """Bytes are immutable; replacing either metadata or asset bytes invalidates it.

    Retain one library per window, rather than every library ever opened. Keep
    references to the source bytes so object identities cannot be reused.
    """

    def __init__(self):
        self.token = None
        self.sources = ()
        self.icons = {}

    def library(self, project):
        record = project.entities["library"]
        names = [e["asset"] for e in record["entries"]]
        if record.get("icc_asset"):
            names.append(record["icc_asset"])
        sources = tuple(project.assets[n] for n in names)
        token = (digest(record), tuple(id(b) for b in sources))
        if token != self.token:
            library = library_from_assets(record, project.assets)
            self.current = library
            self.sources = sources
            self.token = token
            self.icons.clear()
        return self.current

    def icon(self, key):
        if key not in self.icons:
            with self.current.by_key[key].image() as image:
                with screen_rgb(image, self.current.icc) as rgb:
                    self.icons[key] = QIcon(QPixmap.fromImage(ImageQt(rgb)))
        return self.icons[key]


class SymbolPicker(QDialog):
    def __init__(self, owner, color):
        super().__init__(owner)
        self.setWindowTitle("替换线色符号")
        self.resize(680, 560)
        layout = QVBoxLayout(self)
        title = QLabel(f"为 {color.brand} {color.code} · {color.name} 选择符号")
        title.setWordWrap(True)
        title.setProperty("role", "heading")
        layout.addWidget(title)
        note = QLabel("图块显示符号库原色；选择已占用符号会交换两色绑定，针格与绣线颜色保持不变。")
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        layout.addWidget(note)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索符号编号、文件名或已绑定线号")
        self.search.setAccessibleName("搜索可替换符号")
        layout.addWidget(self.search)
        self.symbols = QListWidget()
        self.symbols.setAccessibleName("符号图案及已绑定线色")
        self.symbols.setViewMode(QListWidget.ViewMode.IconMode)
        self.symbols.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.symbols.setMovement(QListWidget.Movement.Static)
        self.symbols.setIconSize(QSize(40, 40))
        self.symbols.setGridSize(QSize(116, 94))
        self.symbols.setSpacing(4)
        layout.addWidget(self.symbols, 1)
        self.selection = QLabel()
        self.selection.setWordWrap(True)
        layout.addWidget(self.selection)
        self.buttons = QDialogButtonBox()
        self.apply_button = self.buttons.addButton(
            "应用符号", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        library = owner.symbol_library()
        mapping = owner.draft.entities["mapping"]
        colors = {c.key: f"{c.brand} {c.code}" for c in owner.design.palette}
        occupied = {s: colors[k] for k, s in mapping.items() if k in colors}
        current = mapping[color.key]
        for symbol in library.symbols:
            identity = str(symbol.external_id)
            usage = "当前符号" if symbol.key == current else occupied.get(symbol.key, "未分配")
            item = QListWidgetItem(owner.symbol_artwork.icon(symbol.key), f"{identity}\n{usage}")
            item.setData(Qt.ItemDataRole.UserRole, symbol.key)
            detail = f" · {occupied[symbol.key]}" if symbol.key == current else ""
            item.setToolTip(f"符号 {identity} · {symbol.filename} · {usage}{detail}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.symbols.addItem(item)
            if symbol.key == current:
                self.symbols.setCurrentItem(item)
        self.symbols.currentItemChanged.connect(self.update_selection)
        self.search.textChanged.connect(self.filter)
        self.update_selection()

    @property
    def chosen(self):
        item = self.symbols.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item and not item.isHidden() else None

    def filter(self, query):
        first = None
        for row in range(self.symbols.count()):
            item = self.symbols.item(row)
            item.setHidden(query.strip().casefold() not in item.toolTip().casefold())
            if first is None and not item.isHidden():
                first = item
        if not self.chosen and first is not None:
            self.symbols.setCurrentItem(first)
        self.update_selection()

    def update_selection(self, *_):
        item = self.symbols.currentItem()
        self.apply_button.setEnabled(self.chosen is not None)
        self.selection.setText(item.toolTip() if self.chosen else "请选择一个可见的符号图块")
