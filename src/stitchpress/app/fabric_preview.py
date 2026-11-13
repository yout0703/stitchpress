"""Physical cloth preview driven by the same decimal geometry as production."""

from decimal import Decimal

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from stitchpress.core.layout import physical_layout


class ClothDiagram(QWidget):
    def __init__(self):
        super().__init__()
        self.measurements = None
        self.setMinimumSize(280, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("画心与四边留白比例示意")

    def paintEvent(self, event):
        if self.measurements is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cw, ch, aw, ah, left, top, right, bottom = self.measurements
        scale = min((self.width() - 100) / cw, (self.height() - 100) / ch)
        cloth = QRectF(
            (self.width() - cw * scale) / 2,
            (self.height() - ch * scale) / 2,
            cw * scale,
            ch * scale,
        )
        art = QRectF(cloth.x() + left * scale, cloth.y() + top * scale, aw * scale, ah * scale)
        painter.setPen(QPen(QColor("#b9c9c4"), 1.5, Qt.PenStyle.DashLine))
        painter.setBrush(QColor("#f1eee5"))
        painter.drawRect(cloth)
        painter.setPen(QPen(QColor("#317b69"), 1.5))
        painter.setBrush(QColor("#c9e3d9"))
        painter.drawRect(art)
        painter.setPen(QColor("#245d50"))
        if art.width() >= 120 and art.height() >= 60:
            painter.drawText(art, Qt.AlignmentFlag.AlignCenter, f"画心\n{aw:.2f} × {ah:.2f} cm")
        painter.setPen(self.palette().text().color())
        painter.drawText(
            QRectF(cloth.x(), cloth.y() - 32, cloth.width(), 24),
            Qt.AlignmentFlag.AlignCenter,
            f"整布宽 {cw:.2f} cm",
        )
        painter.drawText(
            QRectF(cloth.x(), cloth.bottom() + 8, cloth.width(), 24),
            Qt.AlignmentFlag.AlignCenter,
            f"整布高 {ch:.2f} cm",
        )
        painter.end()


class FabricPreview(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("fabricPreview")
        layout = QVBoxLayout(self)
        title = QLabel("裁布尺寸预览")
        title.setProperty("role", "sectionTitle")
        layout.addWidget(title)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.diagram = ClothDiagram()
        layout.addWidget(self.diagram, 1)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setProperty("role", "muted")
        layout.addWidget(self.detail)
        note = QLabel("绿色为画心，浅色为留白。示意按实际比例绘制；修改尺寸不会改变针数。")
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        layout.addWidget(note)
        self.plan = None

    def refresh(self, width, height, fabric):
        plan = physical_layout(width, height, fabric)
        self.plan = plan
        aw, ah = Decimal(fabric.sx) * width, Decimal(fabric.sy) * height
        margins = [Decimal(getattr(fabric, k)) for k in ("left", "top", "right", "bottom")]
        cw, ch = aw + margins[0] + margins[2], ah + margins[1] + margins[3]
        self.diagram.measurements = tuple(map(float, (cw, ch, aw, ah, *margins)))
        self.diagram.update()
        self.summary.setText(f"画心  {aw:.2f} × {ah:.2f} cm\n整布  {cw:.2f} × {ch:.2f} cm")
        self.detail.setText(
            f"左 {margins[0]:.2f} · 右 {margins[2]:.2f} · "
            f"上 {margins[1]:.2f} · 下 {margins[3]:.2f} cm\n"
            f"{width} × {height} 格 · {fabric.dpi} DPI · "
            f"实际格距 {Decimal(fabric.sx):.4f} × {Decimal(fabric.sy):.4f} cm"
        )

    def invalid(self, message):
        self.plan = None
        self.diagram.measurements = None
        self.diagram.update()
        self.summary.setText("请输入完整、有效的尺寸")
        self.detail.setText(str(message))
