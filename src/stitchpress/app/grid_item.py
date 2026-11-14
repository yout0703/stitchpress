"""Viewport-only X rendering with a small color overview for large stitch matrices."""

import math

import numpy as np
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem

from stitchpress.core.models import Design
from stitchpress.core.render import stitch_preview


class StitchGridItem(QGraphicsItem):
    cell = 18

    def __init__(self, design, highlight=None):
        super().__init__()
        self.design = design
        k = max(math.ceil(design.width * self.cell / 3), math.ceil(design.height * self.cell / 4))
        self.bounds = QRectF(0, 0, 3 * k, 4 * k)
        self.dx = (3 * k - design.width * self.cell) // 2
        self.dy = (4 * k - design.height * self.cell) // 2
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)
        self.set_highlight(highlight)

    def set_highlight(self, highlight):
        design = self.design
        palette = []
        self.tiles = {}
        for i, color in enumerate(design.palette):
            rgb = color.rgb
            if highlight is not None and highlight != i:
                rgb = tuple(round(v * 0.2 + 255 * 0.8) for v in rgb)
            palette.append(rgb)
            tiny = Design(
                width=1,
                height=1,
                palette=design.palette,
                labels=((i,),),
                revision_id="viewport-tile",
            )
            sample = stitch_preview(tiny, cell=self.cell, highlight=highlight)
            x, y = (sample.width - self.cell) // 2, (sample.height - self.cell) // 2
            tile = sample.crop((x, y, x + self.cell, y + self.cell))
            self.tiles[i] = QImage(ImageQt(tile)).copy()
            tile.close()
            sample.close()
        labels = np.asarray(design.labels, dtype=np.int16)
        colors = np.asarray([*palette, (255, 255, 255)], dtype=np.uint8)
        rgb = colors[np.where(labels < 0, len(palette), labels)]
        overview = Image.fromarray(rgb)
        overview.thumbnail((2048, 2048), Image.Resampling.NEAREST)
        self.overview = QImage(ImageQt(overview)).copy()
        overview.close()
        self.last_visible_cells = 0
        self.update()

    def boundingRect(self):
        return self.bounds

    def paint(self, painter, option, widget=None):
        painter.fillRect(self.bounds, QColor("white"))
        grid = QRectF(
            self.dx, self.dy, self.design.width * self.cell, self.design.height * self.cell
        )
        if QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform()) < 0.6:
            painter.drawImage(grid, self.overview)
            self.last_visible_cells = 0
            return
        visible = option.exposedRect.intersected(grid)
        x0 = max(0, math.floor((visible.left() - self.dx) / self.cell))
        y0 = max(0, math.floor((visible.top() - self.dy) / self.cell))
        x1 = min(self.design.width, math.ceil((visible.right() - self.dx) / self.cell))
        y1 = min(self.design.height, math.ceil((visible.bottom() - self.dy) / self.cell))
        self.last_visible_cells = max(0, x1 - x0) * max(0, y1 - y0)
        for y in range(y0, y1):
            for x in range(x0, x1):
                index = self.design.labels[y][x]
                if index >= 0:
                    painter.drawImage(
                        QRectF(
                            self.dx + x * self.cell, self.dy + y * self.cell, self.cell, self.cell
                        ),
                        self.tiles[index],
                    )
