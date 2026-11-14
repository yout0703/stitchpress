"""Local alpha renderer. Every output is derived from the same grid and mapping."""

import io
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from math import ceil
from pathlib import Path

from PIL import Image, ImageChops, ImageCms, ImageDraw

from .errors import ResourceError, StitchPressError
from .layout import Fabric, Page, PagePlan, page_coordinates, physical_layout, px
from .models import Design
from .resources import SymbolLibrary, load_font, natural_color_key
from .surface import BandSurface, draw_on
from .text import draw_layer, parse_layers, validate_glyphs, values_for

WHITE = (0, 0, 0, 0)
BLACK = (255, 255, 255, 255)
RED = (0, 255, 255, 0)
MAX_RENDER_PIXELS = 80_000_000  # Single-image guard; production export uses a separate band writer.


def ruler_label(count: int, pitch: str) -> str:
    # Printed text needs useful precision, not a repeating Decimal expansion.
    # Keep the physical line, cell boundaries and output dimensions at full precision.
    cm = f"{Decimal(pitch) * count:.4f}".rstrip("0").rstrip(".")
    return f"{count} cells = {cm} cm"


@dataclass(frozen=True)
class CellRaster:
    window: tuple[int, int, int, int]
    xs: tuple[int, ...]
    ys: tuple[int, ...]
    inset: int
    style: str = "symbols"
    dpi: int | None = None


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
        font_assets: dict | None = None,
    ):
        self.design, self.library, self.mapping, self.fabric = design, library, mapping, fabric
        self.code = code
        self.font_data = font_data
        self.fonts = {}
        self.title = title
        self.text_layers = parse_layers(text_layers or {})
        self.font_assets = font_assets or {}
        self.reading = None
        for layer in self.text_layers.values():
            if layer.font_asset and layer.font_asset not in self.font_assets:
                raise ResourceError("文字区域缺少绑定字体，请重新导入")
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
            raise ResourceError("输出超过8000万像素单页限制；生产大图请使用分条导出，不能静默缩小")
        return Image.new("CMYK", size, WHITE)

    def text(self, image, xy, text: str, size: int, anchor="lt"):
        factor = self.fabric.dpi / 300
        font = self.font(ceil(size * factor))
        validate_glyphs(font, text)
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
        if self.reading is not None:
            return self.reading.reference(page, plan, style=style, minor_grid=minor_grid)
        if style not in {"symbols", "pale", "color"}:
            raise StitchPressError("未知查图样式")
        image = self.canvas(plan.paper_px)
        draw = ImageDraw.Draw(image)
        factor = self.fabric.dpi / 300

        def sc(n):
            return ceil(n * factor)

        xs, ys = page_coordinates(self.layout, page, plan)
        raster = CellRaster(
            page.visible, xs, ys, max(sc(2), sc(3) // 2 + 1), style, self.fabric.dpi
        )
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
            image,
            (plan.origin[0], sc(60)),
            f"StitchPress | Page {page.number}/{len(plan.pages)}",
            34,
        )
        self.text(
            image,
            (plan.origin[0], sc(130)),
            f"Columns {x0 + 1}-{x1} | Rows {y0 + 1}-{y1} | Scale {plan.scale}:1",
            32,
        )
        self.text(image, (plan.origin[0], plan.legend_top - sc(50)), "KEY: No. / Symbol / DMC", 32)
        cell_w = (
            (plan.content_right or image.width - plan.origin[0]) - plan.origin[0]
        ) // plan.legend_columns
        for k, (index, color) in enumerate(self.entries):
            col, row = divmod(k, plan.legend_rows)
            x, y = plan.origin[0] + col * cell_w, plan.legend_top + row * plan.row_height
            self.text(image, (x + sc(8), y + sc(12)), str(k + 1), 36)
            side = sc(42)
            image.paste(self.tile(index, side, side), (x + sc(75), y + sc(8)))
            self.text(image, (x + sc(135), y + sc(8)), color.code, 39)
        ruler_cells = min(100, plan.capacity[0])
        ruler_y = image.height - sc(155)
        length = px(Decimal(self.fabric.sx) * ruler_cells * Decimal(plan.scale), self.fabric.dpi)
        draw.line(
            (plan.origin[0], ruler_y, plan.origin[0] + length, ruler_y), fill=BLACK, width=sc(3)
        )
        self.text(
            image,
            (plan.origin[0], ruler_y + sc(22)),
            ruler_label(ruler_cells, self.fabric.sx) + f" | Print {plan.scale}:1",
            32,
        )
        self.text(
            image,
            (plan.origin[0], image.height - sc(65)),
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
            (xs[0] - sc(2), ys[0] - sc(2), xs[-1] + sc(2) - 1, ys[-1] + sc(2) - 1),
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
        label = ruler_label(count, self.fabric.sx)
        self.text(image, (xs[0], y + sc(22)), label, 32)
        # Fit measured entries into free perimeter. Never increase fabric or shrink text.
        row_h = sc(96)
        max_code = max(self.font(sc(56)).getlength(c.code) for _, c in self.entries)
        entry_w = sc(220) + ceil(max_code)
        bottom_layer = self.text_layers.get("production_bottom")
        bottom_reserve = sc(180) if bottom_layer and bottom_layer.enabled else 0
        rects = [
            (xs[-1] + sc(65), ys[0], image.width - sc(30), image.height - sc(60) - bottom_reserve),
            (sc(30), ys[0], xs[0] - sc(150), image.height - sc(60) - bottom_reserve),
            (xs[0], ys[-1] + sc(250), xs[-1], image.height - sc(40) - bottom_reserve),
            (xs[0], sc(310), xs[-1], ys[0] - sc(150)),
        ]
        # The physical ruler and its label can extend beyond a tiny chart's right edge.
        # Reserve measured space before packing the perimeter legend.
        ruler_box = (
            xs[0] - sc(8),
            y - sc(10),
            max(xs[0] + length, xs[0] + ceil(self.font(sc(32)).getlength(label))) + sc(16),
            y + sc(85),
        )

        def intersects(a, b):
            return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]

        def pack(width, height):
            slots, headers = [], {}
            for x0, y0, x1, y1 in rects:
                for col in range(max(0, (x1 - x0) // width)):
                    for row in range(max(0, (y1 - y0 - sc(65)) // height)):
                        slot = (x0 + col * width, y0 + sc(65) + row * height)
                        header = (x0 + col * width, y0)
                        if intersects(
                            (slot[0], slot[1], slot[0] + width, slot[1] + height), ruler_box
                        ) or intersects(
                            (header[0], header[1], header[0] + width, header[1] + sc(50)),
                            ruler_box,
                        ):
                            continue
                        slots.append(slot)
                        headers[slot] = header
            return slots, headers

        slots, headers = pack(entry_w, row_h)
        compact = len(slots) < len(self.entries)
        if compact:
            # Stack the ordinal above its symbol in narrow margins. Keep the same
            # type sizes and 70px symbol; never resize the cloth or omit entries.
            ordinal_w = ceil(self.font(sc(50)).getlength(str(len(self.entries))))
            code_offset = max(sc(70), ordinal_w) + sc(20)
            entry_w = max(
                code_offset + ceil(max_code) + sc(10),
                ceil(self.font(sc(32)).getlength("No. / DMC")),
            )
            slots, headers = pack(entry_w, sc(136))
        if len(slots) < len(self.entries):
            raise ResourceError("生产余量无法放下完整图例，须调整独立SKU/版式，不能缩字或漏色")
        for header in sorted({headers[slot] for slot in slots[: len(self.entries)]}):
            self.text(image, header, "No. / DMC" if compact else "No. / Symbol / DMC", 32)
        for k, ((index, color), (x, y)) in enumerate(zip(self.entries, slots, strict=False)):
            self.text(image, (x, y if compact else y + sc(12)), str(k + 1), 50)
            image.paste(
                self.tile(index, sc(70), sc(70)),
                (x, y + sc(56)) if compact else (x + sc(85), y),
            )
            self.text(
                image,
                (x + code_offset, y + sc(62)) if compact else (x + sc(180), y + sc(5)),
                color.code,
                56,
            )
        self.custom_text(image, "production")
        return image, raster

    def custom_text(self, image, kind, *, plan=None, page=None):
        active = {k: v for k, v in self.text_layers.items() if k.startswith(kind)}
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
            "symbols_top": (sc(150), sc(25), image.width - sc(150), sc(220)),
            "symbols_bottom": (
                sc(150),
                image.height - sc(100),
                image.width - sc(150),
                image.height - sc(5),
            ),
            "reference_top": (
                plan.origin[0] if plan else sc(150),
                sc(25),
                (plan.content_right or image.width - sc(150)) if plan else image.width - sc(150),
                min(sc(220), plan.origin[1] - sc(50)) if plan else sc(220),
            ),
            "reference_bottom": (
                sc(150),
                image.height - sc(100),
                image.width - sc(150),
                image.height - sc(5),
            ),
        }
        for key, layer in active.items():
            if key.startswith(kind):
                # Production has no built-in footer to erase. A disabled custom
                # footer must neither reserve nor paint over usable legend space.
                if key == "production_bottom" and not layer.enabled:
                    continue
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
                    font_data=self.font_assets[layer.font_asset]
                    if layer.font_asset
                    else self.font_data,
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
        dpi = raster.dpi or self.fabric.dpi
        image.save(
            path,
            "TIFF",
            compression="tiff_lzw",
            dpi=(dpi, dpi),
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
                or tuple(float(n) for n in actual.info["dpi"]) != (dpi,) * 2
                or actual.info.get("icc_profile") != self.library.icc
                or actual.tobytes() != image.tobytes()
            ):
                raise StitchPressError("TIFF编码属性/ICC/完整解码像素不符")
            return self.verify_cells(actual, raster)


@lru_cache(maxsize=2)
def _screen_transform(icc: bytes):
    return ImageCms.buildTransformFromOpenProfiles(
        ImageCms.ImageCmsProfile(io.BytesIO(icc)),
        ImageCms.createProfile("sRGB"),
        "CMYK",
        "RGB",
        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
    )


def screen_rgb(image: Image.Image, icc: bytes | None) -> Image.Image:
    if icc and image.mode == "CMYK":
        return ImageCms.applyTransform(image, _screen_transform(icc))
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
        raise ResourceError("模拟超过8000万像素试验限制")
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
            # Deterministic raised gem: one tile for each occupied grid cell.
            # Shading remains visible for white stitches; empty cells stay white.
            edge, end = 1, cell - 2
            inset = max(1, cell // 4)
            hi = tuple(round(v * 0.65 + 255 * 0.35) for v in rgb)
            low = tuple(round(v * 0.64) for v in rgb)
            mid = tuple(round(v * 0.9) for v in rgb)
            draw.rectangle((edge, edge, end, end), fill=mid)
            draw.polygon(
                (
                    (edge, edge),
                    (end, edge),
                    (end - inset, edge + inset),
                    (edge + inset, edge + inset),
                ),
                fill=hi,
            )
            draw.polygon(
                (
                    (edge, edge),
                    (edge + inset, edge + inset),
                    (edge + inset, end - inset),
                    (edge, end),
                ),
                fill=hi,
            )
            draw.polygon(
                ((end, edge), (end, end), (end - inset, end - inset), (end - inset, edge + inset)),
                fill=low,
            )
            draw.polygon(
                ((edge, end), (edge + inset, end - inset), (end - inset, end - inset), (end, end)),
                fill=low,
            )
            draw.rectangle((edge + inset, edge + inset, end - inset, end - inset), fill=rgb)
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
