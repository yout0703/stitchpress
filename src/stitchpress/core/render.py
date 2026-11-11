"""Local alpha renderer. Every output is derived from the same grid and mapping."""

import io
from dataclasses import dataclass
from decimal import Decimal
from math import ceil
from pathlib import Path

from PIL import Image, ImageChops, ImageCms, ImageDraw

from .errors import ResourceError, StitchPressError
from .layout import Fabric, Page, PagePlan, page_coordinates, physical_layout, px
from .models import Design
from .resources import SymbolLibrary, load_font, natural_color_key
from .surface import BandSurface, draw_on
from .text import draw_layer, parse_layers, values_for

WHITE = (0, 0, 0, 0)
BLACK = (255, 255, 255, 255)
RED = (0, 255, 255, 0)
MAX_RENDER_PIXELS = 60_000_000  # Measured alpha guard, streaming not yet implemented.


@dataclass(frozen=True)
class CellRaster:
    window: tuple[int, int, int, int]
    xs: tuple[int, ...]
    ys: tuple[int, ...]
    inset: int
    style: str = "symbols"


class Renderer:
    def __init__(
        self,
        design: Design,
        library: SymbolLibrary,
        mapping: dict[str, str],
        fabric: Fabric,
        code: str = "",
        font_data: bytes | None = None,
        title: str = "",
        text_layers: dict | None = None,
    ):
        self.design, self.library, self.mapping, self.fabric = design, library, mapping, fabric
        self.code = code
        self.font_data = font_data
        self.fonts = {}
        self.title = title
        self.text_layers = parse_layers(text_layers or {})
        used = {c.key for c, n in zip(design.palette, design.counts, strict=True) if n}
        if set(mapping) != used or len(set(mapping.values())) != len(mapping):
            raise StitchPressError("映射必须覆盖且仅覆盖每种有效线色，符号不能复用")
        if any(v not in library.by_key for v in mapping.values()):
            raise StitchPressError("映射引用缺失符号")
        self.layout = physical_layout(design.width, design.height, fabric)
        self.tiles = {s.key: s.image() for s in library.symbols}
        self.cache = {}
        self.color_cache = {}
        self.entries = sorted(
            [
                (i, c)
                for i, (c, n) in enumerate(zip(design.palette, design.counts, strict=True))
                if n
            ],
            key=lambda entry: natural_color_key(entry[1]),
        )

    def canvas(self, size):
        if size[0] * size[1] > MAX_RENDER_PIXELS:
            raise ResourceError("输出超过6000万像素本地试验限制；尚未实现流式，不能静默缩小")
        return Image.new("CMYK", size, WHITE)

    def text(self, image, xy, text: str, size: int, anchor="lt"):
        factor = self.fabric.dpi / 300
        font = self.font(ceil(size * factor))
        if any(ord(c) > 127 for c in text):
            missing = bytes(font.getmask("\U0010ffff"))
            if any(not c.isspace() and bytes(font.getmask(c)) == missing for c in text):
                raise ResourceError(
                    "所选输出字体缺少文字所需字形，请导入匹配字体；不能静默印出方框"
                )
        draw = draw_on(image)
        box = draw.textbbox(xy, text, font=font, anchor=anchor)
        if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height:
            raise ResourceError(f"文字超出画布，请调整版式：{text[:60]}")
        draw.text(xy, text, fill=BLACK, font=font, anchor=anchor)

    def font(self, pixels):
        if pixels not in self.fonts:
            self.fonts[pixels] = load_font(pixels, self.font_data)
        return self.fonts[pixels]

    def tile(self, index: int, w: int, h: int) -> Image.Image:
        key = self.mapping[self.design.palette[index].key]
        if (key, w, h) not in self.cache:
            self.cache[(key, w, h)] = self.tiles[key].resize((w, h), Image.Resampling.NEAREST)
        return self.cache[(key, w, h)]

    def reference_tile(self, index, w, h, style):
        tile = self.tile(index, w, h)
        if style == "symbols":
            return tile
        key = index, w, h, style
        if key not in self.color_cache:
            rgb = self.design.palette[index].rgb
            if style == "pale":
                rgb = tuple(round(v * 0.22 + 255 * 0.78) for v in rgb)
            background = Image.new("RGB", (1, 1), rgb)
            if self.library.icc:
                background = ImageCms.profileToProfile(
                    background,
                    ImageCms.createProfile("sRGB"),
                    ImageCms.ImageCmsProfile(io.BytesIO(self.library.icc)),
                    outputMode="CMYK",
                    renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                )
            else:
                background = background.convert("CMYK")  # Explicit demo-only fallback.
            self.color_cache[key] = ImageChops.lighter(background.resize((w, h)), tile)
        return self.color_cache[key]

    def cells(self, image: Image.Image, raster: CellRaster):
        x0, y0, x1, y1 = raster.window
        inset = raster.inset
        for gy in range(y0, y1):
            if isinstance(image, BandSurface) and (
                raster.ys[gy - y0 + 1] <= image.band_top
                or raster.ys[gy - y0] >= image.band_top + image.image.height
            ):
                continue
            for gx in range(x0, x1):
                index = self.design.labels[gy][gx]
                if index >= 0:
                    x, y = gx - x0, gy - y0
                    w = raster.xs[x + 1] - raster.xs[x] - 2 * inset
                    h = raster.ys[y + 1] - raster.ys[y] - 2 * inset
                    if min(w, h) < 3:
                        raise ResourceError("当前格距不足以容纳可检查符号")
                    image.paste(
                        self.reference_tile(index, w, h, raster.style),
                        (raster.xs[x] + inset, raster.ys[y] + inset),
                    )

    def legend_entry_width(self) -> int:
        if not self.entries:
            raise StitchPressError("母版没有有效针格，无法生成查图图例")
        font = self.font(39)
        return max(345, ceil(max(font.getlength(c.code) for _, c in self.entries)) + 185)

    def reference(self, page: Page, plan: PagePlan, *, style="symbols", minor_grid=True):
        if style not in {"symbols", "pale", "color"}:
            raise StitchPressError("未知查图样式")
        image = self.canvas(plan.paper_px)
        draw = ImageDraw.Draw(image)
        factor = self.fabric.dpi / 300

        def sc(n):
            return ceil(n * factor)

        xs, ys = page_coordinates(self.layout, page, plan)
        raster = CellRaster(page.visible, xs, ys, sc(2), style)
        self.cells(image, raster)
        x0, y0, x1, y1 = page.visible
        if xs[-1] >= image.width - sc(50) or ys[-1] >= plan.legend_top - sc(50):
            raise ResourceError("重叠窗口/针格超出版式可用区域")
        for j, x in enumerate(xs):
            if not minor_grid and (x0 + j) % 10:
                continue
            draw.line(
                (x, ys[0], x, ys[-1]), fill=BLACK, width=sc(3) if (x0 + j) % 10 == 0 else sc(1)
            )
            if (x0 + j) % 10 == 0:
                self.text(image, (x, ys[0] - sc(30)), str(x0 + j), 36, "mb")
        for j, y in enumerate(ys):
            if not minor_grid and (y0 + j) % 10:
                continue
            draw.line(
                (xs[0], y, xs[-1], y), fill=BLACK, width=sc(3) if (y0 + j) % 10 == 0 else sc(1)
            )
            if (y0 + j) % 10 == 0:
                self.text(image, (xs[0] - sc(18), y), str(y0 + j), 36, "rm")
        draw.rectangle(
            (xs[0] - sc(2), ys[0] - sc(2), xs[-1] + sc(2), ys[-1] + sc(2)),
            outline=BLACK,
            width=sc(2),
        )
        self.text(
            image, (sc(150), sc(60)), f"StitchPress | Page {page.number}/{len(plan.pages)}", 34
        )
        self.text(
            image,
            (sc(150), sc(130)),
            f"Columns {x0 + 1}-{x1} | Rows {y0 + 1}-{y1} | Scale {plan.scale}:1",
            32,
        )
        self.text(image, (sc(150), plan.legend_top - sc(50)), "KEY: No. / Symbol / DMC", 32)
        cell_w = (image.width - 2 * sc(150)) // plan.legend_columns
        for k, (index, color) in enumerate(self.entries):
            col, row = divmod(k, plan.legend_rows)
            x, y = sc(150) + col * cell_w, plan.legend_top + row * plan.row_height
            self.text(image, (x + sc(8), y + sc(12)), str(k + 1), 36)
            side = sc(42)
            image.paste(self.tile(index, side, side), (x + sc(75), y + sc(8)))
            self.text(image, (x + sc(135), y + sc(8)), color.code, 39)
        ruler_cells = min(100, plan.capacity[0])
        ruler_y = image.height - sc(155)
        length = px(Decimal(self.fabric.sx) * ruler_cells * Decimal(plan.scale), self.fabric.dpi)
        draw.line((sc(150), ruler_y, sc(150) + length, ruler_y), fill=BLACK, width=sc(3))
        self.text(
            image,
            (sc(150), ruler_y + sc(22)),
            f"{ruler_cells} cells = {Decimal(self.fabric.sx) * ruler_cells} cm "
            f"| Print {plan.scale}:1",
            32,
        )
        self.text(
            image,
            (sc(150), image.height - sc(65)),
            f"Neighbors L/R/U/D: {' / '.join(str(n or '-') for n in page.neighbors)}",
            32,
        )
        self.custom_text(image, "reference", plan=plan, page=page)
        return image, raster

    def production(self, *, band=None):
        image = (
            BandSurface(self.layout.canvas_px, *band)
            if band
            else self.canvas(self.layout.canvas_px)
        )
        draw = draw_on(image)
        factor = self.fabric.dpi / 300

        def sc(n):
            return ceil(n * factor)

        xs, ys = self.layout.xs, self.layout.ys
        raster = CellRaster((0, 0, self.design.width, self.design.height), xs, ys, 0)
        self.cells(image, raster)
        draw.rectangle((0, 0, image.width - 1, image.height - 1), outline=RED, width=sc(6))
        draw.rectangle(
            (xs[0] - sc(2), ys[0] - sc(2), xs[-1] + sc(1), ys[-1] + sc(1)),
            outline=BLACK,
            width=sc(2),
        )
        for i in range(0, self.design.width + 1, 10):
            draw.line((xs[i], ys[0] - sc(40), xs[i], ys[0] - sc(8)), fill=BLACK, width=sc(3))
            if i:
                self.text(image, (xs[i], ys[0] - sc(65)), str(i), 48, "mb")
        for i in range(0, self.design.height + 1, 10):
            draw.line((xs[0] - sc(40), ys[i], xs[0] - sc(8), ys[i]), fill=BLACK, width=sc(3))
            if i:
                self.text(image, (xs[0] - sc(60), ys[i]), str(i), 48, "rm")
        self.text(
            image, (xs[0], sc(90)), f"StitchPress | {self.code}" if self.code else "StitchPress", 48
        )
        self.text(image, (xs[0], sc(170)), "Production | Actual Size 100%", 32)
        count = min(100, self.design.width)
        y = ys[-1] + sc(90)
        length = px(Decimal(self.fabric.sx) * count, self.fabric.dpi)
        draw.line((xs[0], y, xs[0] + length, y), fill=BLACK, width=sc(3))
        label = f"{count} cells = {Decimal(self.fabric.sx) * count} cm"
        self.text(image, (xs[0], y + sc(22)), label, 32)
        # Fit measured entries into free perimeter. Never increase fabric or shrink text.
        row_h = sc(96)
        max_code = max(self.font(sc(56)).getlength(c.code) for _, c in self.entries)
        entry_w = sc(220) + ceil(max_code)
        bottom_reserve = sc(180) if self.text_layers.get("production_bottom") else 0
        rects = [
            (xs[-1] + sc(65), ys[0], image.width - sc(30), image.height - sc(60) - bottom_reserve),
            (sc(30), ys[0], xs[0] - sc(150), image.height - sc(60) - bottom_reserve),
            (xs[0], ys[-1] + sc(250), xs[-1], image.height - sc(40) - bottom_reserve),
            (xs[0], sc(310), xs[-1], ys[0] - sc(150)),
        ]
        slots = []
        headers = {}
        for x0, y0, x1, y1 in rects:
            for col in range(max(0, (x1 - x0) // entry_w)):
                for row in range(max(0, (y1 - y0 - sc(65)) // row_h)):
                    slot = (x0 + col * entry_w, y0 + sc(65) + row * row_h)
                    slots.append(slot)
                    headers[slot] = (x0 + col * entry_w, y0)
        if len(slots) < len(self.entries):
            raise ResourceError("生产余量无法放下完整图例，须调整独立SKU/版式，不能缩字或漏色")
        for header in sorted({headers[slot] for slot in slots[: len(self.entries)]}):
            self.text(image, header, "No. / Symbol / DMC", 32)
        for k, ((index, color), (x, y)) in enumerate(zip(self.entries, slots, strict=False)):
            self.text(image, (x, y + sc(12)), str(k + 1), 50)
            image.paste(self.tile(index, sc(70), sc(70)), (x + sc(85), y))
            self.text(image, (x + sc(180), y + sc(5)), color.code, 56)
        self.custom_text(image, "production")
        return image, raster

    def custom_text(self, image, kind, *, plan=None, page=None):
        active = {k: v for k, v in self.text_layers.items() if k.startswith(kind) and v.template}
        if not active:
            return
        factor = self.fabric.dpi / 300

        def sc(n):
            return ceil(n * factor)

        if not hasattr(self, "_text_values"):
            self._text_values = values_for(
                self.design, self.fabric, code=self.code, title=self.title
            )
        values = {
            **self._text_values,
            "page": page.number if page else "",
            "pages": len(plan.pages) if plan else "",
            "scale": plan.scale if plan else "1",
        }
        regions = {
            "production_top": (
                sc(30),
                sc(40),
                image.width - sc(30),
                min(sc(280), self.layout.ys[0] - sc(150)),
            ),
            "production_bottom": (
                sc(30),
                image.height - sc(140),
                image.width - sc(30),
                image.height - sc(20),
            ),
            "reference_top": (sc(150), sc(25), image.width - sc(150), sc(220)),
            "reference_bottom": (
                sc(150),
                image.height - sc(100),
                image.width - sc(150),
                image.height - sc(5),
            ),
        }
        for key, layer in active.items():
            if key.startswith(kind) and layer.template:
                box = regions[key]
                if (
                    kind == "production"
                    and key.endswith("bottom")
                    and self.layout.ys[-1] + sc(180) > box[1]
                ):
                    raise ResourceError("生产底栏没有足够留白；请调整SKU留白，不会覆盖针格或校尺")
                draw_on(image).rectangle(box, fill=WHITE)
                draw_layer(
                    image,
                    box,
                    layer,
                    values,
                    font_data=self.font_data,
                    dpi=self.fabric.dpi,
                    name=key,
                )

    def verify_cells(self, image: Image.Image, raster: CellRaster) -> dict:
        x0, y0, x1, y1 = raster.window
        counts = [0] * len(self.design.palette)
        for gy in range(y0, y1):
            for gx in range(x0, x1):
                index = self.design.labels[gy][gx]
                x, y, s = gx - x0, gy - y0, raster.inset
                box = (
                    raster.xs[x] + s,
                    raster.ys[y] + s,
                    raster.xs[x + 1] - s,
                    raster.ys[y + 1] - s,
                )
                got = image.crop(box)
                expected = (
                    self.reference_tile(index, got.width, got.height, raster.style)
                    if index >= 0
                    else Image.new("CMYK", got.size, WHITE)
                )
                if got.tobytes() != expected.tobytes():
                    raise StitchPressError(f"TIFF针格反读不符：({gx},{gy})")
                if index >= 0:
                    counts[index] += 1
        return {
            "window": raster.window,
            "stitches": sum(counts),
            "counts": counts,
            "blank": (x1 - x0) * (y1 - y0) - sum(counts),
        }

    def save_tiff(self, image: Image.Image, path: Path, raster: CellRaster) -> dict:
        image.save(
            path,
            "TIFF",
            compression="tiff_lzw",
            dpi=(self.fabric.dpi, self.fabric.dpi),
            icc_profile=self.library.icc,
        )
        with Image.open(path) as actual:
            actual.load()
            if (
                actual.mode != "CMYK"
                or actual.size != image.size
                or actual.n_frames != 1
                or actual.tag_v2[259] != 5
                or actual.tag_v2[296] != 2
                or tuple(float(n) for n in actual.info["dpi"]) != (self.fabric.dpi,) * 2
                or actual.info.get("icc_profile") != self.library.icc
                or actual.tobytes() != image.tobytes()
            ):
                raise StitchPressError("TIFF编码属性/ICC/完整解码像素不符")
            return self.verify_cells(actual, raster)


def screen_rgb(image: Image.Image, icc: bytes | None) -> Image.Image:
    if icc and image.mode == "CMYK":
        return ImageCms.profileToProfile(
            image,
            ImageCms.ImageCmsProfile(io.BytesIO(icc)),
            ImageCms.createProfile("sRGB"),
            outputMode="RGB",
            renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
        )
    return image.convert("RGB")


def stitch_preview(
    design: Design, cell: int = 18, style: str = "x", highlight: int | None = None
) -> Image.Image:
    if style not in {"x", "dot"} or not 6 <= cell <= 36:
        raise StitchPressError("未知模拟样式或不支持的像素密度")
    width, height = design.width * cell, design.height * cell
    k = max(ceil(width / 3), ceil(height / 4))
    width, height = 3 * k, 4 * k
    if width * height > MAX_RENDER_PIXELS:
        raise ResourceError("模拟超过6000万像素试验限制")
    im = Image.new("RGB", (width, height), "white")
    dx, dy = (width - design.width * cell) // 2, (height - design.height * cell) // 2
    tiles = []
    for i, color in enumerate(design.palette):
        tile = Image.new("RGB", (cell, cell), "white")
        draw = ImageDraw.Draw(tile)
        rgb = color.rgb
        if highlight is not None and i != highlight:
            rgb = tuple(round(v * 0.2 + 255 * 0.8) for v in rgb)
        if style == "dot":
            draw.ellipse((1, 1, cell - 2, cell - 2), fill=rgb)
        else:
            shadow = tuple(max(0, round(v * 0.82)) for v in rgb)
            for points in ((1, 1, cell - 2, cell - 2), (1, cell - 2, cell - 2, 1)):
                draw.line(points, fill=shadow, width=max(3, cell // 3))
                draw.line(points, fill=rgb, width=max(1, cell // 6))
        tiles.append(tile)
    for y, row in enumerate(design.labels):
        for x, index in enumerate(row):
            if index >= 0:
                im.paste(tiles[index], (dx + x * cell, dy + y * cell))
    return im
