"""Candidate comparison and explicit acceptance; never overwrite the old master."""

import copy
from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stitchpress.core.errors import StitchPressError
from stitchpress.core.models import Design


def label_color(value):
    return "留空" if value is None else f"{value['brand']} {value['code']}"


class CandidateDialog(QDialog):
    def __init__(self, owner, result):
        super().__init__(owner)
        self.setWindowTitle("针格候选比较 · 接受后仍须重新审核")
        self.resize(1180, 880)
        self.action = None
        self.selected = []
        self.checked_cells = set()
        self.loading_table = False
        self.result = result
        self.key = result["candidate_key"]
        candidate = owner.draft.entities[self.key]
        self.candidate = Design.from_dict(candidate["design"])
        layout = QVBoxLayout(self)
        stats = result["comparison"]
        delta = (
            f"变化 {stats['changed_cells']} 格"
            if stats["same_grid"]
            else "尺寸改变，不支持坐标迁移"
        )
        summary = QLabel(
            f"当前 {stats['grid_before']} → 候选 {stats['grid_after']} · "
            f"{delta} · 增减 {stats['stitch_delta']:+d} 针 · "
            f"{stats['colors_before']} → {stats['colors_after']} 色\n"
            "左右均来自真实矩阵。旧母版/原始转换/补丁保留，模型或本地转换完成不代表内容合格。"
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)
        self.views = []
        for title, design in (("当前采用版", owner.design), ("待采用候选", self.candidate)):
            pane = QWidget()
            box = QVBoxLayout(pane)
            box.addWidget(QLabel(title))
            canvas = type(owner.canvas)()
            canvas.set_design(design)
            box.addWidget(canvas, 1)
            split.addWidget(pane)
            self.views.append(canvas)
        QTimer.singleShot(0, lambda: [v.fit() for v in self.views])
        proposal = result.get("migration")
        self.table = None
        if proposal is not None:
            counts = {
                kind: sum(e["status"] == kind for e in proposal["entries"])
                for kind in ("ready", "conflict", "already")
            }
            note = QLabel(
                f"原有补丁链 {len(result['patch_chain'])} 段 · 可迁移 {counts['ready']} 格 · "
                f"冲突 {counts['conflict']} 格 · 已包含 {counts['already']} 格。"
                "冲突项不能勾选；默认不迁移。选中后先生成新候选，再比较采用。"
            )
            note.setWordWrap(True)
            layout.addWidget(note)
            self.table = QTableWidget(0, 4)
            self.table.setHorizontalHeaderLabels(
                ["选择/坐标（0基）", "原修订", "新候选原值", "状态/原因"]
            )
            self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.table.horizontalHeader().setStretchLastSection(True)
            self.table.setMaximumHeight(240)
            self.migration_entries = proposal["entries"]
            self.fill_page(1)
            self.table.itemChanged.connect(self.remember_check)
            layout.addWidget(self.table)
            if len(self.migration_entries) > 2000:
                pages = QHBoxLayout()
                pages.addWidget(QLabel("修订列表页（每页2000格，跨页保留勾选）"))
                self.page = QSpinBox()
                self.page.setRange(1, (len(self.migration_entries) + 1999) // 2000)
                self.page.valueChanged.connect(self.fill_page)
                pages.addWidget(self.page)
                layout.addLayout(pages)
            row = QHBoxLayout()
            all_button = QPushButton("勾选本表可迁移项")
            all_button.clicked.connect(self.check_ready)
            row.addWidget(all_button)
            migrate = QPushButton("生成所选修订的迁移候选")
            migrate.clicked.connect(self.migrate)
            row.addWidget(migrate)
            layout.addLayout(row)
        row = QHBoxLayout()
        adopt = QPushButton("采用右侧候选（不自动迁移旧修订）")
        adopt.clicked.connect(lambda: self.choose("adopt"))
        row.addWidget(adopt)
        reject = QPushButton("拒绝本次候选，保留当前母版")
        reject.clicked.connect(lambda: self.choose("reject"))
        row.addWidget(reject)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.table:
            self.table.itemSelectionChanged.connect(self.locate)

    def fill_page(self, page):
        self.loading_table = True
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            entries = self.migration_entries[(page - 1) * 2000 : page * 2000]
            for i, entry in enumerate(entries):
                self.table.insertRow(i)
                item = QTableWidgetItem(f"({entry['x']},{entry['y']})")
                item.setData(Qt.ItemDataRole.UserRole, (entry["x"], entry["y"]))
                if entry["status"] == "ready":
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if (entry["x"], entry["y"]) in self.checked_cells
                        else Qt.CheckState.Unchecked
                    )
                else:
                    item.setFlags(
                        item.flags()
                        & ~(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    )
                self.table.setItem(i, 0, item)
                for col, text in enumerate(
                    (
                        label_color(entry["before"]) + " → " + label_color(entry["after"]),
                        label_color(entry["candidate_before"]),
                        entry["reason"],
                    ),
                    1,
                ):
                    self.table.setItem(i, col, QTableWidgetItem(text))
        finally:
            self.table.blockSignals(False)
            self.loading_table = False

    def remember_check(self, item):
        if self.loading_table or item.column() != 0:
            return
        xy = tuple(item.data(Qt.ItemDataRole.UserRole))
        if (
            item.checkState() == Qt.CheckState.Checked
            and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
        ):
            self.checked_cells.add(xy)
        else:
            self.checked_cells.discard(xy)

    def choose(self, action):
        self.action = action
        self.accept()

    def check_ready(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked)

    def migrate(self):
        self.selected = sorted(self.checked_cells, key=lambda xy: (xy[1], xy[0]))
        if self.selected:
            self.choose("migrate")

    def locate(self):
        row = self.table.currentRow()
        if row >= 0:
            x, y = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            for view, grid in zip(
                self.views,
                (self.result["comparison"]["grid_before"], self.result["comparison"]["grid_after"]),
                strict=True,
            ):
                if x < grid[0] and y < grid[1]:
                    dx = (view.sceneRect().width() - grid[0] * 18) // 2
                    dy = (view.sceneRect().height() - grid[1] * 18) // 2
                    rect = QRectF(dx + x * 18, dy + y * 18, 18, 18)
                    view.actual_pixels()
                    view.draw_selection(rect)
                    view.centerOn(rect.center())


class CandidateFeatures:
    def review_current(self):
        from stitchpress.core.models import ReviewRecord

        from .context import review_context

        record = self.draft.entities.get("freeze")
        if not record:
            return False
        try:
            review = ReviewRecord(
                **{**record, "detail_regions": tuple(tuple(r) for r in record["detail_regions"])}
            )
            review.validate(self.design)
            review.validate_context(review_context(self.draft))
            return True
        except (StitchPressError, KeyError, TypeError):
            return False

    def invalidate_review(self, reason):
        if old := self.draft.entities.pop("freeze", None):
            self.draft.entities[f"freeze:{uuid4().hex}"] = {"record": old, "reason": reason}
            self.dirty = True
            self.refresh()

    def record_candidate(self, payload, *, base_sha=None, context_sha=None):
        design = Design.from_dict(payload["design"])
        key = "candidate:" + uuid4().hex
        self.draft.entities[key] = {
            **payload,
            "created_at": datetime.now(UTC).isoformat(),
            "base_design_sha": base_sha,
            "context_sha": context_sha,
            "result_sha": design.content_sha,
        }
        self.dirty = True
        self.update_document_state()
        return key

    def candidate_browser(self):
        records = [
            (key, value)
            for key, value in self.draft.entities.items()
            if key.startswith("candidate:")
        ]
        if not records:
            return self.error("尚未产生针格候选；可先做本地转换")
        labels = [
            f"{value.get('created_at', '历史')} · "
            f"{value['design']['width']}×{value['design']['height']} · {key[-8:]}"
            for key, value in records
        ]
        chosen, accepted = QInputDialog.getItem(
            self,
            "针格候选与修订",
            "选择真实矩阵进行比较，不重新转换或调用模型",
            labels,
            len(labels) - 1,
            False,
        )
        if accepted:
            try:
                self.start_worker(
                    "compare-candidate",
                    {"candidate_key": records[labels.index(chosen)][0], "include_image": False},
                )
            except Exception as exc:
                self.error(exc)

    def show_candidate_comparison(self, result):
        key = result["candidate_key"]
        if key not in self.draft.entities:
            raise StitchPressError("原候选已不在当前草稿")
        dialog = CandidateDialog(self, result)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.action == "migrate":
            self.start_worker(
                "migrate-candidate",
                {
                    "candidate_key": key,
                    "proposal": result["migration"],
                    "selected": [list(xy) for xy in dialog.selected],
                    "include_image": False,
                },
            )
            return
        candidate = self.draft.entities[key]
        if dialog.action == "adopt":
            from stitchpress.core.identity import sha256

            conversion = candidate.get("conversion", {})
            source_sha = conversion.get(
                "source_sha", conversion.get("source", {}).get("source_sha")
            )
            if source_sha != (sha256(self.source_image) if self.source_image else None):
                raise StitchPressError(
                    "候选来自其他源图或缺少来源记录；请在任务记录恢复原输入后再比较，不套用到当前分支"
                )
        self.draft.entities[f"candidate-decision:{uuid4().hex}"] = {
            "candidate": key,
            "result_sha": dialog.candidate.content_sha,
            "decision": dialog.action,
            "previous_sha": self.design.content_sha,
            "at": datetime.now(UTC).isoformat(),
        }
        if dialog.action == "adopt":
            self.adopt_design(Design.from_dict(candidate["design"]))
            if patch := candidate.get("patch"):
                self.draft.entities[f"patch:{self.design.revision_id}"] = copy.deepcopy(patch)
            if candidate["design"]["origin"] == "converted":
                self.draft.entities["active_conversion"] = copy.deepcopy(candidate["conversion"])
            elif candidate.get("parent_candidate"):
                parent = self.draft.entities[candidate["parent_candidate"]]
                self.draft.entities["active_conversion"] = copy.deepcopy(parent["conversion"])
            self.draft.entities["active_candidate"] = {
                "key": key,
                "result_sha": self.design.content_sha,
            }
        self.dirty = True
        self.update_document_state()
        self.statusBar().showMessage("候选决定已记录；旧母版与未迁移修订保留，新版仍须人工审核")

    def convert_selection(self):
        try:
            if not self.region or not self.source_image:
                raise StitchPressError("请先选择对应蓝图并框选明确针格区域")
            self.capture_conversion_options()
            if (self.grid_w.value(), self.grid_h.value()) != (
                self.design.width,
                self.design.height,
            ):
                raise StitchPressError("局部转换不能改变针格尺寸")
            self.start_worker(
                "convert-region",
                {
                    "region": list(self.region),
                    "options": self.conversion_options(),
                    "include_image": True,
                },
            )
        except Exception as exc:
            self.error(exc)
