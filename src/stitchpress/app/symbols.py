"""Explicit library revisions and portable symbol cards; old assets remain available."""

import copy
import tempfile
from datetime import UTC
from pathlib import Path
from uuid import uuid4

from PIL import ImageQt
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from stitchpress.core.documents import write_pdf
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest, sha256, sha256_file
from stitchpress.core.layout import decimal, px
from stitchpress.core.render import Renderer, screen_rgb
from stitchpress.core.resources import (
    assign_symbols,
    library_assets,
    library_from_assets,
    load_font,
    normalize_symbol_image,
    revise_library,
)
from stitchpress.platform.files import exclusive_lock

from .context import export_context


def symbol_cards(context):
    renderer = Renderer(
        context["design"],
        context["library"],
        context["mapping"],
        context["fabric"],
        code=context["code"],
        font_data=context["font_data"],
    )
    factor = context["fabric"].dpi / 300

    def sc(value):
        return round(value * factor)

    paper = ("21", "29.7")
    size = tuple(px(decimal(value), context["fabric"].dpi) for value in paper)
    font = load_font(sc(39), context["font_data"])
    if not renderer.entries:
        raise StitchPressError("空母版没有可导出的符号")
    code_width = max(font.getlength(f"{c.brand} {c.code}") for _, c in renderer.entries)
    entry_width = max(sc(600), round(code_width) + sc(220))
    rows = max(1, (size[1] - sc(500)) // sc(100))
    columns = (size[0] - sc(300)) // entry_width
    if columns < 1:
        raise StitchPressError("线色文字无法放入速查卡，请调整字体或代码；不会缩字")
    capacity = rows * columns
    for offset in range(0, len(renderer.entries), capacity):
        image = renderer.canvas(size)
        renderer.text(
            image, (sc(150), sc(70)), f"StitchPress | Symbol Card {offset // capacity + 1}", 42
        )
        renderer.text(image, (sc(150), sc(150)), f"{context['code']} | No. / Symbol / Thread", 36)
        for k, (index, color) in enumerate(renderer.entries[offset : offset + capacity], offset):
            column, row = divmod(k - offset, rows)
            x, y = sc(150) + column * entry_width, sc(300) + row * sc(100)
            renderer.text(image, (x, y + sc(10)), str(k + 1), 39)
            image.paste(renderer.tile(index, sc(60), sc(60)), (x + sc(80), y))
            renderer.text(image, (x + sc(165), y + sc(8)), f"{color.brand} {color.code}", 39)
        yield image, paper


def export_symbol_card(project, target: Path):
    context = export_context(project)
    if target.exists() or target.suffix.lower() not in {".pdf", ".png"}:
        raise StitchPressError("速查卡须另存为新的PDF或PNG")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".symbol-card-", dir=target.parent) as work:
        pages = []
        paper = ("21", "29.7")
        for i, (image, _paper) in enumerate(symbol_cards(context)):
            path = Path(work) / f"page-{i:03}.tif"
            image.save(
                path,
                "TIFF",
                compression="tiff_lzw",
                dpi=(context["fabric"].dpi,) * 2,
                icc_profile=context["library"].icc,
            )
            if target.suffix.lower() == ".png":
                if i:
                    raise StitchPressError("多页速查卡请使用PDF，不能静默只保留第一页")
                rgb = screen_rgb(image, context["library"].icc)
                rgb.save(Path(work) / "card.png", dpi=(context["fabric"].dpi,) * 2)
                rgb.close()
            image.close()
            pages.append(path)
        if target.suffix.lower() == ".pdf":
            result = write_pdf(
                pages, Path(work) / "card.pdf", context["fabric"].dpi, paper, context["library"].icc
            )
            ready = Path(work) / "card.pdf"
        else:
            result = {"pages": 1}
            ready = Path(work) / "card.png"
        with exclusive_lock(target.with_suffix(target.suffix + ".lock")):
            if target.exists():
                raise StitchPressError("速查卡目标已由另一任务建立，未覆盖")
            # Same-volume rename publishes only a complete file.
            ready.rename(target)
    return {
        **result,
        "sha256": sha256_file(target),
        "mapping_sha": digest(context["mapping"]),
        "colors": len(context["mapping"]),
        "path": str(target),
    }


class SymbolLibraryDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.original = owner.draft.entities["library"]
        self.imports = []
        self.import_assets = {}
        self.library = library_from_assets(self.original, owner.draft.assets)
        self.setWindowTitle("符号库管理 · 保存为新修订")
        self.resize(800, 740)
        layout = QVBoxLayout(self)
        note = QLabel(
            "库ID保留原类型；使用中的符号先调整绑定再删除。支持TIF/PNG/BMP，"
            "按当前库ICC规范化并保留原件；透明背景合成到白底。无ICC的RGB按sRGB解释。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["精确 ID", "显示名称", "当前使用"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setIconSize(QSize(30, 30))
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.preview = QLabel("选择符号查看放大图")
        self.preview.setMinimumHeight(130)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.preview)
        self.table.itemSelectionChanged.connect(self.show_symbol)
        actions = QHBoxLayout()
        for text, callback in (
            ("新增图块…", lambda: self.import_tile(False)),
            ("替换所选图块…", lambda: self.import_tile(True)),
            ("重命名显示名称…", self.rename),
            ("移除未使用符号", self.remove),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def selected(self):
        selected = self.table.selectedItems()
        if not selected:
            raise StitchPressError("请先选择一个符号")
        return self.library.symbols[selected[0].row()]

    def refresh(self):
        self.table.setRowCount(0)
        mapping = self.owner.draft.entities["mapping"]
        for row, symbol in enumerate(self.library.symbols):
            self.table.insertRow(row)
            used = [
                f"{c.brand} {c.code}"
                for c in self.owner.design.palette
                if mapping.get(c.key) == symbol.key
            ]
            for col, text in enumerate((symbol.key, symbol.filename, ", ".join(used) or "未使用")):
                item = QTableWidgetItem(text)
                if col == 0:
                    image = screen_rgb(symbol.image(), self.library.icc)
                    item.setIcon(QIcon(QPixmap.fromImage(ImageQt.ImageQt(image))))
                self.table.setItem(row, col, item)
            self.table.setRowHeight(row, 42)
        self.table.resizeColumnsToContents()

    def show_symbol(self):
        if self.table.selectedItems():
            image = screen_rgb(self.selected().image(), self.library.icc)
            self.preview.setPixmap(
                QPixmap.fromImage(ImageQt.ImageQt(image)).scaled(
                    140,
                    140,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
            )

    def import_tile(self, replacing):
        try:
            selected = self.selected() if replacing else None
            external_id = None
            if not replacing:
                dialog = QDialog(self)
                dialog.setWindowTitle("精确外部ID")
                form = QFormLayout(dialog)
                kind, value = QComboBox(), QLineEdit()
                kind.addItems(["文本（保留0.1、前导零等）", "整数"])
                form.addRow("ID类型", kind)
                form.addRow("ID原值", value)
                buttons = QDialogButtonBox(
                    QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                )
                buttons.accepted.connect(dialog.accept)
                buttons.rejected.connect(dialog.reject)
                form.addRow(buttons)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                external_id = value.text() if kind.currentIndex() == 0 else int(value.text())
            path, _ = QFileDialog.getOpenFileName(
                self, "导入符号图块", "", "Symbols (*.tif *.tiff *.png *.bmp)"
            )
            if not path:
                return
            raw = Path(path).read_bytes()
            normalized, receipt = normalize_symbol_image(raw, self.library.icc)
            updated = revise_library(
                self.library,
                "replace" if replacing else "add",
                key=selected.key if selected else None,
                external_id=external_id,
                data=normalized,
                name=Path(path).name,
            )
            source_asset = f"resources/symbol-originals/{sha256(raw)}{Path(path).suffix.lower()}"
            self.import_assets[source_asset] = raw
            self.imports.append(
                {
                    **receipt,
                    "asset": source_asset,
                    "filename": Path(path).name,
                    "library_revision": updated.revision,
                }
            )
            self.library = updated
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "符号库未改变", str(exc))

    def rename(self):
        try:
            symbol = self.selected()
            name, accepted = QInputDialog.getText(
                self, "新显示名称", "不改变精确ID或已有绑定", text=symbol.filename
            )
            if accepted:
                self.library = revise_library(self.library, "rename", key=symbol.key, name=name)
                self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "符号库未改变", str(exc))

    def remove(self):
        try:
            symbol = self.selected()
            self.library = revise_library(
                self.library,
                "remove",
                key=symbol.key,
                used=frozenset(self.owner.draft.entities["mapping"].values()),
            )
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "符号库未改变", str(exc))


class SymbolFeatures:
    def manage_symbols(self):
        try:
            dialog = SymbolLibraryDialog(self)
            if (
                dialog.exec() != QDialog.DialogCode.Accepted
                or dialog.library.revision == dialog.original["revision"]
            ):
                return
            mapping = assign_symbols(self.design, dialog.library, self.draft.entities["mapping"])
            record, assets = library_assets(dialog.library)
            self.draft.entities[f"library:{dialog.original['revision']}"] = copy.deepcopy(
                dialog.original
            )
            self.draft.entities["library"] = record
            self.draft.assets.update(assets)
            self.draft.assets.update(dialog.import_assets)
            self.draft.entities.setdefault("symbol_imports", {"records": []})["records"].extend(
                dialog.imports
            )
            self.draft.entities["mapping"] = mapping
            self.dirty = True
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def reassign_symbols(self):
        from datetime import datetime

        library = library_from_assets(self.draft.entities["library"], self.draft.assets)
        mapping = assign_symbols(self.design, library)
        if mapping == self.draft.entities["mapping"]:
            return self.statusBar().showMessage("当前绑定已与默认分配相同")
        answer = QMessageBox.question(
            self,
            "重新自动分配？",
            "将按自然线号顺序重新分配；已有人工绑定会保留为历史记录，生产与查图需要重新导出。",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.draft.entities[f"mapping:{uuid4().hex}"] = {
                "before": copy.deepcopy(self.draft.entities["mapping"]),
                "after": mapping,
                "library_revision": library.revision,
                "design_sha": self.design.content_sha,
                "algorithm": "natural-thread-source-symbol-v2",
                "created_at": datetime.now(UTC).isoformat(),
            }
            self.draft.entities["mapping"] = mapping
            self.dirty = True
            self.refresh()

    def export_symbol_card(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出同源符号速查卡（扩展产物）", "符号速查卡.pdf", "PDF (*.pdf);;PNG (*.png)"
        )
        if path:
            try:
                self.start_worker(
                    "symbol-card", {"target": str(Path(path)), "include_image": False}
                )
            except Exception as exc:
                self.error(exc)
