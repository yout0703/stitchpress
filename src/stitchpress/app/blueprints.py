"""Versioned local blueprint candidates, preserving raw and normalized originals."""

import copy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.blueprints import ORIGINS, check_blueprint
from stitchpress.core.brief import DesignBrief
from stitchpress.core.conversion import normalize_image
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import sha256


class BlueprintDialog(QDialog):
    def __init__(self, owner, key):
        super().__init__(owner)
        from PIL import Image

        self.owner = owner
        self.key = key
        self.record = owner.draft.entities[key]
        self.adopt = False
        self.setWindowTitle("蓝图候选比较 · 采用后再本地制版")
        self.resize(1080, 780)
        layout = QVBoxLayout(self)
        note = QLabel(
            f"{self.record.get('label', '蓝图')} · {ORIGINS[self.record['origin']]} · "
            f"{self.record['normalized_size']} · {self.record['normalized_sha'][:12]}\n"
            "保留原文件与规范化版本。外部AI信息由用户填写，不代表本软件调用或验证了该供应商。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)
        views = []
        for title, data in (
            ("当前对照输入", owner.source_image),
            ("蓝图候选", owner.draft.assets[self.record["normalized_asset"]]),
        ):
            pane = QWidget()
            box = QVBoxLayout(pane)
            box.addWidget(QLabel(title))
            view = type(owner.canvas)()
            box.addWidget(view, 1)
            views.append(view)
            split.addWidget(pane)
            if data:
                try:
                    image, _ = normalize_image(
                        data,
                        owner.draft.assets[owner.draft.entities["input_profile"]["asset"]]
                        if title == "当前对照输入" and owner.draft.entities.get("input_profile")
                        else None,
                    )
                except Exception as exc:
                    if title != "当前对照输入":
                        raise
                    warning = QLabel(f"当前输入无法预览，请核对源ICC：{exc}")
                    warning.setWordWrap(True)
                    box.addWidget(warning)
                    continue
                display = Image.new("RGBA", image.size, "white")
                display.alpha_composite(image)
                display.thumbnail((2048, 2048))
                view.set_image(display.convert("RGB"))
                image.close()
                display.close()
        QTimer.singleShot(0, lambda: [v.fit() for v in views])
        self.confirm = QCheckBox("我已核对构图/对象及来源，确认这是可用于制版的干净蓝图")
        layout.addWidget(self.confirm)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "采用蓝图（保留当前针格，等待转换）"
        )
        buttons.accepted.connect(self.accept_reviewed)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept_reviewed(self):
        if self.confirm.isChecked():
            self.adopt = True
            self.accept()


class BlueprintFeatures:
    def import_blueprint_candidate(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入蓝图候选（不自动采用）",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.tif *.tiff)",
        )
        if not path:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("蓝图来源记录")
        form = QFormLayout(dialog)
        origin = QComboBox()
        for key, label in ORIGINS.items():
            if key != "local-composite":
                origin.addItem(label, key)
        note, provider, model = QLineEdit(), QLineEdit(), QLineEdit()
        for label, widget in [
            ("来源类型", origin),
            ("需保留/已知限制", note),
            ("供应商（可留空）", provider),
            ("模型（可留空）", model),
        ]:
            form.addRow(label, widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.start_worker(
                    "blueprint-import",
                    {
                        "file": path,
                        "origin": origin.currentData(),
                        "note": note.text(),
                        "provider": provider.text(),
                        "model": model.text(),
                        "include_image": False,
                    },
                )
            except Exception as exc:
                self.error(exc)

    def local_image_candidate(self):
        try:
            if not self.source_image or not self.region:
                raise StitchPressError("请先选择对照图，并框选明确的针格区域")
            path, _ = QFileDialog.getOpenFileName(
                self,
                "选择一张同画幅、已对齐的局部编辑候选",
                "",
                "Images (*.png *.jpg *.jpeg *.webp)",
            )
            if not path:
                return
            answer = QMessageBox.question(
                self,
                "确认候选画幅与对象对齐？",
                "尺寸相同不代表内容已对齐。请确认候选与当前蓝图的对象/位置对应；程序只取选区像素，外区保留当前蓝图。无法对齐应按整体候选处理。",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.start_worker(
                "blueprint-composite",
                {"file": path, "region": list(self.region), "include_image": True},
            )
        except Exception as exc:
            self.error(exc)

    def attach_blueprint(self, result, job):
        raw = Path(result["original_path"]).read_bytes()
        normalized = Path(result["normalized_path"]).read_bytes()
        record = result["record"]
        if sha256(raw) != record["original_sha"] or sha256(normalized) != record["normalized_sha"]:
            raise StitchPressError("后台蓝图文件已改变，未采用")
        original = f"blueprints/original/{record['original_sha']}.image"
        converted = f"blueprints/normalized/{record['normalized_sha']}.png"
        assets = {original: raw, converted: normalized}
        key = "blueprint:" + uuid4().hex
        metadata = {
            **record,
            "original_asset": original,
            "normalized_asset": converted,
            "created_at": datetime.now(UTC).isoformat(),
            "label": result["label"],
            "base_design_sha": job["base_sha"],
            "base_source_sha": job["source_sha"],
            **({"composite": result["composite"]} if result.get("composite") else {}),
        }
        if result.get("edited_path"):
            edited = Path(result["edited_path"]).read_bytes()
            edited_sha = sha256(edited)
            if edited_sha != result["composite"]["candidate"]["source_sha"]:
                raise StitchPressError("局部编辑原件已改变")
            asset = f"blueprints/edits/{edited_sha}.image"
            assets[asset] = edited
            metadata.update(edited_asset=asset, edited_sha=edited_sha)
        check_blueprint(metadata, {**self.draft.assets, **assets})
        self.draft.assets.update(assets)
        self.draft.entities[key] = metadata
        self.dirty = True
        self.update_document_state()
        return key

    def blueprint_browser(self):
        records = [
            (key, r) for key, r in self.draft.entities.items() if key.startswith("blueprint:")
        ]
        if not records:
            return self.error("尚未导入蓝图候选")
        labels = [
            f"{r.get('label', '蓝图')} · {ORIGINS[r['origin']]} · {r['revision'][:8]}"
            for _, r in records
        ]
        selected, ok = QInputDialog.getItem(
            self, "蓝图版本", "选择已留存的真实文件", labels, len(labels) - 1, False
        )
        if ok:
            try:
                self.show_blueprint(records[labels.index(selected)][0])
            except Exception as exc:
                self.error(exc)

    def show_blueprint(self, key):
        dialog = BlueprintDialog(self, key)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.adopt:
            return
        self.adopt_blueprint(key)

    def adopt_blueprint(self, key):
        record = self.draft.entities[key]
        check_blueprint(record, self.draft.assets)
        if record.get("composite") and (
            record["base_source_sha"] != (sha256(self.source_image) if self.source_image else None)
            or record["base_design_sha"] != self.design.content_sha
        ):
            raise StitchPressError("局部合成的基准已改变，不能重绑旧坐标；请重新比较")
        before = copy.deepcopy(self.draft.entities.get("source"))
        self.import_source(
            self.draft.assets[record["normalized_asset"]], record.get("label", "blueprint") + ".png"
        )
        self.draft.entities["source"].update(
            role="human-adopted-clean-blueprint", blueprint_revision=record["revision"]
        )
        self.draft.entities.setdefault(
            "original_source", before or copy.deepcopy(self.draft.entities["source"])
        )
        if old := self.draft.entities.get("brief"):
            self.draft.entities[f"brief:{uuid4().hex}"] = copy.deepcopy(old)
        catalog = self.draft.entities.get("catalog")
        brief = {
            **self.draft.entities.get("brief", {}),
            "input_kind": "flat_image",
            "route": "direct_blueprint",
            "source_sha": record["normalized_sha"],
            "catalog_revision": sha256(self.draft.assets[catalog["asset"]]) if catalog else None,
            "width": self.grid_w.value(),
            "height": self.grid_h.value(),
            "max_colors": self.color_limit.value(),
            "confirmed": True,
        }
        self.draft.entities["brief"] = DesignBrief(**brief).to_dict()
        if record.get("composite") and (annotations := self.draft.entities.get("annotations")):
            self.draft.entities[f"annotations:{uuid4().hex}"] = copy.deepcopy(annotations)
            annotations["source_sha"] = record["normalized_sha"]
        self.draft.entities["active_blueprint"] = {"key": key, "revision": record["revision"]}
        self.dirty = True
        self.restore_ai_state()
        self.refresh_annotations()
        self.update_document_state()
        self.statusBar().showMessage("蓝图已采用，针格保持原版；请转换候选或只转换选区，并重新审核")
