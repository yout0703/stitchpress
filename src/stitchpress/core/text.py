"""Shared strict text placeholders and explicit fixed-area typesetting."""

from dataclasses import asdict, dataclass
from string import Formatter

from .errors import ResourceError, StitchPressError
from .layout import decimal, physical_layout, px
from .resources import load_font
from .surface import draw_on

SLOTS = {
    "production_top": "生产顶栏",
    "production_bottom": "生产底栏",
    "reference_top": "查图顶栏",
    "reference_bottom": "查图底栏",
    "symbols_top": "符号表/卡顶栏",
    "symbols_bottom": "符号表/卡底栏",
}
FIELDS = {
    "title",
    "code",
    "width",
    "height",
    "colors",
    "stitches",
    "width_cm",
    "height_cm",
    "ct",
    "page",
    "pages",
    "scale",
}


@dataclass(frozen=True)
class TextLayer:
    template: str = ""
    points: str = "12"
    align: str = "left"
    dx_mm: str = "0"
    dy_mm: str = "0"
    cmyk: tuple[int, int, int, int] = (255, 255, 255, 255)
    enabled: bool = True
    font_asset: str | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool or (
            self.font_asset is not None and not isinstance(self.font_asset, str)
        ):
            raise StitchPressError("文字显示开关/字体引用无效")
        if (
            not isinstance(self.template, str)
            or len(self.template) > 2000
            or "\n" in self.template
            or "\r" in self.template
        ):
            raise StitchPressError("顶底栏须为2000字符内单行文字")
        if self.align not in {"left", "center", "right"} or not 6 <= decimal(self.points) <= 36:
            raise StitchPressError("对齐须左/中/右，字号须6–36pt")
        if abs(decimal(self.dx_mm)) > 20 or abs(decimal(self.dy_mm)) > 10:
            raise StitchPressError("文字横向偏移须±20mm以内，纵向须±10mm以内")
        if (
            not isinstance(self.cmyk, (list, tuple))
            or len(self.cmyk) != 4
            or any(type(n) is not int or not 0 <= n <= 255 for n in self.cmyk)
        ):
            raise StitchPressError("CMYK通道须为0–255整数")
        expand(self.template, dict.fromkeys(FIELDS, ""))


def parse_layers(record):
    if not isinstance(record, dict) or set(record) - SLOTS.keys():
        raise StitchPressError("未知文字区域")
    return {key: TextLayer(**value) for key, value in record.items()}


def expand(template, values):
    try:
        parts = []
        for literal, field, spec, conversion in Formatter().parse(template):
            parts.append(literal)
            if field is not None:
                if field not in FIELDS or spec or conversion:
                    raise ValueError
                parts.append(str(values[field]))
        return "".join(parts)
    except (ValueError, KeyError) as exc:
        raise StitchPressError(
            "占位符无效；只支持列出的字段，不支持属性、格式表达式或转换"
        ) from exc


def values_for(design, fabric, *, code="", title="", page="", pages="", scale="1"):
    layout = physical_layout(design.width, design.height, fabric)
    return {
        "title": title,
        "code": code,
        "width": design.width,
        "height": design.height,
        "colors": sum(n > 0 for n in design.counts),
        "stitches": sum(design.counts),
        # Text is presentation, not a geometry input. CT-derived dimensions can
        # have 28 recurring decimal digits and overflow an otherwise valid page.
        "width_cm": f"{decimal(layout.width_cm):.4f}".rstrip("0").rstrip("."),
        "height_cm": f"{decimal(layout.height_cm):.4f}".rstrip("0").rstrip("."),
        "ct": fabric.nominal_ct,
        "page": page,
        "pages": pages,
        "scale": scale,
    }


def validate_glyphs(font, text):
    missing = bytes(font.getmask("\U0010ffff"))
    absent = list(
        dict.fromkeys(c for c in text if not c.isspace() and bytes(font.getmask(c)) == missing)
    )
    if absent:
        details = "、".join(f"“{c}”(U+{ord(c):04X})" for c in absent[:8])
        raise ResourceError(
            f"输出字体缺少字形：{details}。请在「印花与图纸 → 阅读图纸 → 输出字体」"
            "恢复内置中英文字体，或导入包含这些字形的字体；"
            "若顶栏或页脚单独指定了字体，请在「标题与页脚」改为沿用公共字体。"
        )


def checked_font(size, data, text):
    font = load_font(size, data)
    validate_glyphs(font, text)
    return font


def draw_layer(image, region, layer, values, *, font_data, dpi, name):
    if not layer.enabled:
        return
    text = expand(layer.template, values)
    if not text:
        return
    font = checked_font(round(decimal(layer.points) * dpi / 72), font_data, text)
    draw = draw_on(image)
    x0, y0, x1, y1 = region
    anchor = {"left": "lt", "center": "mt", "right": "rt"}[layer.align]
    x = {"left": x0, "center": (x0 + x1) // 2, "right": x1}[layer.align]
    y = y0 + (y1 - y0 - (font.getbbox(text)[3] - font.getbbox(text)[1])) // 2
    xy = (x + px(decimal(layer.dx_mm) / 10, dpi), y + px(decimal(layer.dy_mm) / 10, dpi))
    box = draw.textbbox(xy, text, font=font, anchor=anchor)
    if (
        box[0] < x0
        or box[1] < y0
        or box[2] > x1
        or box[3] > y1
        or x0 < 0
        or y0 < 0
        or x1 > image.width
        or y1 > image.height
        or y1 <= y0
    ):
        raise ResourceError(
            f"{name}文字溢出保留区域：{text[:50]}；请调整字号/偏移/内容，不自动缩字"
        )
    draw.text(xy, text, fill=tuple(layer.cmyk), font=font, anchor=anchor)
    return {"region": region, "bbox": box, "text": text, "style": asdict(layer)}
