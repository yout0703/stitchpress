"""Deterministic merchandising layouts. Scenes are original schematic illustrations."""

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from PIL import Image, ImageDraw, ImageOps

from .conversion import normalize_image
from .errors import ResourceError, StitchPressError
from .layout import decimal
from .render import MAX_RENDER_PIXELS, screen_rgb, stitch_preview
from .text import FIELDS, checked_font, expand, values_for

SCENES = (
    "纯净墙面",
    "客厅沙发",
    "卧室床头",
    "书房桌面",
    "陈列搁架",
    "玄关矮柜",
    "绿植角落",
    "窗边",
    "画廊墙面",
)
FRAMES = ("细黑框", "纯白框", "浅橡木", "深胡桃", "银色金属", "哑金框", "圆角框", "悬浮框")
CONTENTS = {
    "scene-1": "场景一",
    "scene-2": "场景二",
    "scene-3": "场景三",
    "spec": "作品规格",
    "compare": "来源图与模拟对照",
    "print": "印花符号示意",
    "care": "注意事项",
    "closeup": "针格局部",
    "showcase": "母版模拟",
}
TARGETS = tuple(
    [f"detail-{k}" for k in ("scene-1", "scene-2", "scene-3", "spec", "compare", "print", "care")]
    + [f"square-{i}" for i in range(1, 6)]
)


def color(value):
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 3
        or any(type(n) is not int or not 0 <= n <= 255 for n in value)
    ):
        raise StitchPressError("模板颜色须为三个0–255整数")
    return tuple(value)


@dataclass(frozen=True)
class SceneSettings:
    scene: int = 0
    frame: int = 0
    scale: str = "1"
    dx: str = "0"
    dy: str = "0"
    border: int = 16
    mat: int = 28
    background_asset: str | None = None
    mat_color: tuple[int, int, int] = (249, 247, 240)

    def __post_init__(self):
        if (
            type(self.scene) is not int
            or not 0 <= self.scene < 9
            or type(self.frame) is not int
            or not 0 <= self.frame < 8
        ):
            raise StitchPressError("场景须0–8，相框须0–7")
        if not Decimal(".2") <= decimal(self.scale) <= Decimal("1.4") or any(
            abs(decimal(v)) > Decimal(".5") for v in (self.dx, self.dy)
        ):
            raise StitchPressError("场景缩放须0.2–1.4，归一化偏移须±0.5内")
        if any(type(v) is not int or not 0 <= v <= 100 for v in (self.border, self.mat)):
            raise StitchPressError("框边与卡纸须0–100设计像素")
        if self.background_asset is not None and not isinstance(self.background_asset, str):
            raise StitchPressError("背景须为作品内嵌资源")
        color(self.mat_color)


@dataclass(frozen=True)
class CommerceSettings:
    scenes: tuple[dict, ...] = field(
        default_factory=lambda: tuple(asdict(SceneSettings(scene=n)) for n in (1, 2, 3))
    )
    squares: tuple[str, ...] = ("scene-1", "scene-2", "scene-3", "closeup", "spec")
    title: str = "StitchPress | {code}"
    footer: str = "Chart simulation | {width} x {height} stitches"
    care: str = "Care and washing instructions require manufacturer confirmation."
    font_pixels: int = 48
    background: tuple[int, int, int] = (247, 246, 242)
    ink: tuple[int, int, int] = (35, 43, 49)
    accent: tuple[int, int, int] = (75, 128, 117)
    closeup_x: str = "0.5"
    closeup_y: str = "0.5"
    closeup_zoom: str = "3"
    square_dpi: int = 150
    schema_version: int = 1

    def __post_init__(self):
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or len(self.scenes) != 3
        ):
            raise StitchPressError("商品模板须为schema1和三个独立场景")
        for scene in self.scenes:
            SceneSettings(**scene)
        if len(self.squares) != 5 or any(item not in CONTENTS for item in self.squares):
            raise StitchPressError("五张方图内容无效")
        if type(self.font_pixels) is not int or not 20 <= self.font_pixels <= 80:
            raise StitchPressError("标题字号须20–80设计像素")
        for text in (self.title, self.footer, self.care):
            if not isinstance(text, str) or len(text) > 3000:
                raise StitchPressError("模板文字格式无效或过长")
            expand(text, dict.fromkeys(FIELDS, ""))
        for value in (self.background, self.ink, self.accent):
            color(value)
        if (
            any(not 0 <= decimal(v) <= 1 for v in (self.closeup_x, self.closeup_y))
            or not 1 <= decimal(self.closeup_zoom) <= 12
        ):
            raise StitchPressError("局部中心须归一化0–1，放大倍率须1–12")
        if type(self.square_dpi) is not int or not 72 <= self.square_dpi <= 600:
            raise StitchPressError("方图DPI须72–600；不改变800×800像素")

    def to_dict(self):
        return asdict(self)


class CommerceRenderer:
    def __init__(self, context, settings, assets, *, demo=False):
        self.context, self.settings, self.assets, self.demo = context, settings, assets, demo
        self.design = context["design"]
        self.fields = values_for(
            self.design, context["fabric"], code=context["code"], title=context["title"]
        )
        # Density is a presentation raster choice only; the stitch matrix stays authoritative.
        cell = 18
        from math import ceil

        while cell > 6:
            k = max(ceil(self.design.width * cell / 3), ceil(self.design.height * cell / 4))
            if 12 * k * k <= MAX_RENDER_PIXELS:
                break
            cell -= 1
        simulated = stitch_preview(self.design, cell=cell, style=context["showcase_options"].style)
        x, y = (
            (simulated.width - self.design.width * cell) // 2,
            (simulated.height - self.design.height * cell) // 2,
        )
        self.subject = simulated.crop(
            (x, y, x + self.design.width * cell, y + self.design.height * cell)
        )
        simulated.close()
        self.symbol_renderer = None

    def text(self, image, value, box, size, *, ink=None, max_lines=6):
        value = expand(value, self.fields)
        font = checked_font(max(10, round(size)), self.context["font_data"], value)
        draw = ImageDraw.Draw(image)
        lines, current = [], ""
        for char in value:
            if char == "\n" or font.getlength(current + char) > box[2] - box[0]:
                if not current and char != "\n":
                    raise ResourceError("商品文字单个字形超过文本区域")
                lines.append(current.rstrip())
                current = "" if char == "\n" else char.lstrip()
            else:
                current += char
        if current:
            lines.append(current)
        spacing = round(size * 1.35)
        if len(lines) > max_lines or spacing * len(lines) > box[3] - box[1]:
            raise ResourceError("商品文字溢出，请修改内容/字号；不自动缩字")
        for i, line in enumerate(lines):
            draw.text(
                (box[0], box[1] + i * spacing),
                line,
                font=font,
                fill=ink or color(self.settings.ink),
                anchor="lt",
            )

    @staticmethod
    def contained(image, source, box):
        fitted = ImageOps.contain(
            source, (box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS
        )
        xy = (
            box[0] + (box[2] - box[0] - fitted.width) // 2,
            box[1] + (box[3] - box[1] - fitted.height) // 2,
        )
        image.paste(fitted, xy)
        fitted.close()

    def background_scene(self, image, box, scene):
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        draw = ImageDraw.Draw(image)
        walls = [
            (236, 233, 226),
            (230, 233, 228),
            (238, 231, 226),
            (225, 234, 235),
            (238, 235, 225),
            (227, 230, 227),
            (226, 234, 222),
            (229, 234, 239),
            (225, 224, 221),
        ]
        draw.rectangle(box, fill=walls[scene])
        floor_y = y0 + round(h * 0.82)
        draw.rectangle((x0, floor_y, x1, y1), fill=(202, 186, 165))
        if scene in {1, 2}:
            draw.rounded_rectangle(
                (x0 + w * 0.12, y0 + h * 0.7, x1 - w * 0.12, y0 + h * 0.94),
                radius=w * 0.03,
                fill=(168, 174, 168) if scene == 1 else (216, 206, 193),
            )
            for a in (0.2, 0.52):
                draw.rounded_rectangle(
                    (x0 + w * a, y0 + h * 0.72, x0 + w * (a + 0.24), y0 + h * 0.85),
                    radius=w * 0.015,
                    fill=(205, 207, 198),
                )
        elif scene in {3, 4, 5}:
            yy = y0 + h * (0.78 if scene != 4 else 0.68)
            draw.rectangle((x0 + w * 0.08, yy, x1 - w * 0.08, yy + h * 0.035), fill=(150, 115, 86))
            if scene != 4:
                draw.rectangle(
                    (x0 + w * 0.12, yy + h * 0.035, x1 - w * 0.12, y1 - h * 0.03),
                    fill=(185, 151, 113),
                )
        elif scene in {6, 7}:
            draw.rectangle(
                (x1 - w * 0.18, y0 + h * 0.68, x1 - w * 0.07, y0 + h * 0.87), fill=(164, 135, 111)
            )
            draw.line(
                (x1 - w * 0.125, y0 + h * 0.68, x1 - w * 0.125, y0 + h * 0.40),
                fill=(75, 100, 74),
                width=max(2, w // 100),
            )
            for yy in (0.42, 0.50, 0.58):
                draw.ellipse(
                    (x1 - w * 0.25, y0 + h * yy, x1 - w * 0.125, y0 + h * (yy + 0.07)),
                    fill=(105, 136, 91),
                )
        if scene == 7:
            draw.rectangle(
                (x0 + w * 0.04, y0 + h * 0.10, x0 + w * 0.2, y0 + h * 0.6),
                fill=(245, 249, 247),
                outline=(199, 208, 204),
                width=max(2, w // 100),
            )
        if scene == 8:
            for a in (0.04, 0.82):
                draw.rectangle(
                    (x0 + w * a, y0 + h * 0.3, x0 + w * (a + 0.13), y0 + h * 0.52),
                    outline=(100, 98, 91),
                    width=max(2, w // 100),
                )

    def scene(self, image, box, spec):
        self.background_scene(image, box, spec.scene)
        if spec.background_asset:
            if spec.background_asset not in self.assets:
                raise ResourceError("场景缺少内嵌背景资源")
            normalized, _ = normalize_image(self.assets[spec.background_asset])
            white = Image.new("RGBA", normalized.size, "white")
            white.alpha_composite(normalized)
            self.contained(image, white.convert("RGB"), box)
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        available = (
            max(1, round(w * 0.62 * float(spec.scale))),
            max(1, round(h * 0.54 * float(spec.scale))),
        )
        art = ImageOps.contain(self.subject, available, Image.Resampling.LANCZOS)
        factor = image.width / 1200
        border, mat = round(spec.border * factor), round(spec.mat * factor)
        fw, fh = art.width + 2 * (border + mat), art.height + 2 * (border + mat)
        cx = x0 + w * (0.5 + float(spec.dx))
        cy = y0 + h * (0.39 + float(spec.dy))
        left, top = round(cx - fw / 2), round(cy - fh / 2)
        if left < x0 or top < y0 or left + fw > x1 or top + fh > y1:
            raise ResourceError("完整画框超出场景区域，请调整缩放/偏移；不会裁切作品")
        frame_colors = (
            (43, 43, 39),
            (245, 245, 242),
            (184, 145, 99),
            (91, 63, 44),
            (168, 175, 179),
            (180, 151, 91),
            (72, 91, 89),
            (52, 53, 50),
        )
        draw = ImageDraw.Draw(image)
        draw.rectangle((left + 8, top + 10, left + fw + 8, top + fh + 10), fill=(162, 159, 151))
        if spec.frame == 6:
            draw.rounded_rectangle(
                (left, top, left + fw, top + fh),
                radius=max(2, border),
                fill=frame_colors[spec.frame],
            )
        else:
            draw.rectangle((left, top, left + fw, top + fh), fill=frame_colors[spec.frame])
        draw.rectangle(
            (left + border, top + border, left + fw - border, top + fh - border),
            fill=color(spec.mat_color),
        )
        if spec.frame in {2, 3}:
            for offset in range(4, max(4, border), 5):
                draw.line(
                    (left + offset, top + offset, left + fw - offset, top + offset),
                    fill=(126, 99, 71),
                    width=1,
                )
        if spec.frame == 7:
            draw.rectangle(
                (
                    left + border + 3,
                    top + border + 3,
                    left + fw - border - 3,
                    top + fh - border - 3,
                ),
                outline=(91, 90, 83),
                width=max(1, border // 3),
            )
        image.paste(art, (left + border + mat, top + border + mat))
        art.close()
        return (left, top, left + fw, top + fh)

    def closeup(self):
        cx, cy = (
            float(self.settings.closeup_x) * self.subject.width,
            float(self.settings.closeup_y) * self.subject.height,
        )
        side = max(1, round(min(self.subject.size) / float(self.settings.closeup_zoom)))
        x = min(max(0, round(cx - side / 2)), self.subject.width - side)
        y = min(max(0, round(cy - side / 2)), self.subject.height - side)
        return self.subject.crop((x, y, x + side, y + side))

    def render(self, target):
        if target not in TARGETS:
            raise StitchPressError("未知商品导出项")
        square = target.startswith("square")
        content = (
            self.settings.squares[int(target[-1]) - 1] if square else target.removeprefix("detail-")
        )
        size = (800, 800) if square else (1200, 1600)
        image = Image.new("RGB", size, color(self.settings.background))
        w, h = size
        f = w / 1200
        self.text(
            image,
            self.settings.title,
            (round(w * 0.06), round(h * 0.035), round(w * 0.94), round(h * 0.14)),
            self.settings.font_pixels * f,
            max_lines=2,
        )
        box = (round(w * 0.06), round(h * 0.18), round(w * 0.94), round(h * 0.82))
        art_box = None
        if content.startswith("scene-"):
            art_box = self.scene(
                image, box, SceneSettings(**self.settings.scenes[int(content[-1]) - 1])
            )
        elif content == "spec":
            self.contained(image, self.subject, (box[0], box[1], box[2], round(h * 0.58)))
            spec = (
                "{width} x {height} cells\n{colors} colors | {stitches} full crosses\n"
                "Artwork {width_cm} x {height_cm} cm\nNominal CT {ct} | Code {code}"
            )
            self.text(
                image, spec, (box[0], round(h * 0.62), box[2], round(h * 0.82)), 34 * f, max_lines=4
            )
        elif content == "compare":
            source = self.context.get("source_data")
            if not source:
                raise ResourceError("效果对比缺少对应来源图片，请导入后导出该项；不伪造原图")
            normalized, _ = normalize_image(source, self.context.get("input_icc"))
            white = Image.new("RGBA", normalized.size, "white")
            white.alpha_composite(normalized)
            mid = (box[0] + box[2]) // 2
            self.contained(
                image, white.convert("RGB"), (box[0], box[1], mid - 12, box[3] - round(60 * f))
            )
            self.contained(image, self.subject, (mid + 12, box[1], box[2], box[3] - round(60 * f)))
            self.text(
                image,
                "Source / Chart simulation",
                (box[0], box[3] - round(40 * f), box[2], box[3]),
                26 * f,
                max_lines=1,
            )
        elif content == "care":
            self.contained(image, self.subject, (box[0], box[1], box[2], round(h * 0.52)))
            self.text(
                image,
                self.settings.care,
                (box[0], round(h * 0.57), box[2], box[3]),
                36 * f,
                max_lines=7,
            )
        elif content == "print":
            from .render import Renderer

            if self.symbol_renderer is None:
                self.symbol_renderer = Renderer(
                    self.design,
                    self.context["library"],
                    self.context["mapping"],
                    self.context["fabric"],
                )
            # An explicit finite stitch window; not a production-sized factory TIFF.
            cx, cy = (
                round(float(self.settings.closeup_x) * (self.design.width - 1)),
                round(float(self.settings.closeup_y) * (self.design.height - 1)),
            )
            x0, y0 = max(0, cx - 12), max(0, cy - 12)
            x1, y1 = min(self.design.width, x0 + 24), min(self.design.height, y0 + 24)
            chart = Image.new("CMYK", ((x1 - x0) * 24, (y1 - y0) * 24), (0, 0, 0, 0))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    index = self.design.labels[y][x]
                    if index >= 0:
                        chart.paste(
                            self.symbol_renderer.tile(index, 24, 24), ((x - x0) * 24, (y - y0) * 24)
                        )
            rgb = screen_rgb(chart, self.context["library"].icc)
            self.contained(image, rgb, box)
            rgb.close()
            chart.close()
        else:
            close = self.closeup() if content == "closeup" else None
            self.contained(image, close or self.subject, box)
            if close:
                close.close()
        draw = ImageDraw.Draw(image)
        self.text(
            image,
            ("DRAFT | " if self.demo else "") + "Chart simulation; illustrative layout",
            (round(w * 0.06), round(h * 0.835), round(w * 0.94), round(h * 0.867)),
            20 * f,
            max_lines=1,
        )
        draw.line(
            (round(w * 0.06), round(h * 0.87), round(w * 0.94), round(h * 0.87)),
            fill=color(self.settings.accent),
            width=max(1, round(3 * f)),
        )
        self.text(
            image,
            self.settings.footer,
            (round(w * 0.06), round(h * 0.89), round(w * 0.94), round(h * 0.98)),
            25 * f,
            max_lines=2,
        )
        return image, {
            "content": content,
            "size": size,
            "design_sha": self.design.content_sha,
            "scene_art_box": art_box,
            "illustrative_scene": content.startswith("scene-"),
            "source_type": "deterministic-stitch-simulation",
            "demo": self.demo,
        }
