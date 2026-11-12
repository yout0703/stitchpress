"""Versioned, data-only commerce layouts. No executable template expressions."""

import copy
import math
import re

from .errors import StitchPressError
from .identity import digest

ROLES = ("scene", "spec", "compare", "print", "care", "closeup", "showcase")
BOXES = {"title", "kicker", "body", "footer", "caption"}


def _pages(title, kicker, body, caption, footer, *, split, compare):
    return {
        role: dict(
            title=title,
            kicker=kicker,
            body=body,
            caption=caption,
            footer=footer,
            split=split,
            compare=compare,
        )
        for role in ROLES
    }


def _template(identity, name, description, colors, portrait, square, ornament, scenes):
    return dict(
        schema_version=1,
        id=identity,
        version=1,
        name=name,
        description=description,
        source="StitchPress original layout",
        ornament=ornament,
        pages={"portrait": portrait, "square": square},
        defaults=dict(
            background=colors[0],
            ink=colors[1],
            accent=colors[2],
            title="{title}",
            footer="{code} · {width} × {height} 格 · {ct} CT",
            care="洗护方法请以所购绣布与绣线的说明为准。\n如有疑问，请向商家确认后再操作。",
            scenes=scenes,
        ),
    )


_BUILTINS = (
    _template(
        "gallery",
        "简约画廊",
        "以作品为中心 · 明亮留白与画廊式排版",
        ((250, 249, 245), (31, 47, 48), (63, 112, 100)),
        _pages(
            (0.075, 0.095, 0.925, 0.18),
            (0.075, 0.045, 0.925, 0.075),
            (0.075, 0.23, 0.925, 0.78),
            (0.075, 0.81, 0.925, 0.86),
            (0.075, 0.925, 0.925, 0.965),
            split="bottom",
            compare="columns",
        ),
        _pages(
            (0.065, 0.10, 0.935, 0.19),
            (0.065, 0.035, 0.935, 0.075),
            (0.065, 0.24, 0.935, 0.77),
            (0.065, 0.79, 0.935, 0.85),
            (0.065, 0.925, 0.935, 0.97),
            split="side",
            compare="columns",
        ),
        "line",
        (8, 0, 6),
    ),
    _template(
        "home",
        "温暖家居",
        "把画带入生活 · 奶油底色与场景叙事",
        ((247, 240, 229), (79, 57, 45), (155, 102, 69)),
        _pages(
            (0.08, 0.70, 0.92, 0.785),
            (0.08, 0.65, 0.92, 0.68),
            (0.055, 0.045, 0.945, 0.61),
            (0.08, 0.82, 0.92, 0.875),
            (0.08, 0.93, 0.92, 0.97),
            split="side",
            compare="columns",
        ),
        _pages(
            (0.075, 0.695, 0.925, 0.785),
            (0.075, 0.645, 0.925, 0.68),
            (0.045, 0.04, 0.955, 0.61),
            (0.075, 0.82, 0.925, 0.87),
            (0.075, 0.935, 0.925, 0.975),
            split="side",
            compare="rows",
        ),
        "arch",
        (1, 7, 4),
    ),
    _template(
        "oriental",
        "东方雅韵",
        "疏朗题签 · 纸色、墨色与不对称构图",
        ((244, 241, 231), (46, 59, 53), (151, 66, 51)),
        _pages(
            (0.14, 0.12, 0.89, 0.205),
            (0.14, 0.06, 0.89, 0.09),
            (0.14, 0.27, 0.94, 0.79),
            (0.14, 0.825, 0.89, 0.875),
            (0.14, 0.935, 0.89, 0.97),
            split="side",
            compare="rows",
        ),
        _pages(
            (0.16, 0.12, 0.93, 0.205),
            (0.16, 0.05, 0.93, 0.09),
            (0.16, 0.27, 0.94, 0.76),
            (0.16, 0.80, 0.93, 0.86),
            (0.16, 0.935, 0.93, 0.975),
            split="bottom",
            compare="columns",
        ),
        "rule",
        (5, 3, 8),
    ),
    _template(
        "product",
        "清晰商品",
        "信息清楚 · 模块化规格与细节说明",
        ((238, 243, 245), (27, 46, 58), (38, 99, 126)),
        _pages(
            (0.065, 0.11, 0.935, 0.20),
            (0.065, 0.05, 0.935, 0.08),
            (0.065, 0.25, 0.935, 0.78),
            (0.065, 0.82, 0.935, 0.875),
            (0.065, 0.93, 0.935, 0.97),
            split="side",
            compare="rows",
        ),
        _pages(
            (0.06, 0.115, 0.94, 0.21),
            (0.06, 0.04, 0.94, 0.08),
            (0.06, 0.265, 0.94, 0.77),
            (0.06, 0.81, 0.94, 0.87),
            (0.06, 0.935, 0.94, 0.975),
            split="bottom",
            compare="rows",
        ),
        "grid",
        (0, 4, 8),
    ),
)


def templates():
    return copy.deepcopy(_BUILTINS)


def template_by_id(identity):
    for template in _BUILTINS:
        if template["id"] == identity:
            return copy.deepcopy(template)
    raise StitchPressError("未知商品模板")


def validate_template(value):
    if value is None:
        return
    expected = {
        "schema_version",
        "id",
        "version",
        "name",
        "description",
        "source",
        "ornament",
        "pages",
        "defaults",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise StitchPressError("商品模板描述字段无效")
    if value["schema_version"] != 1 or type(value["version"]) is not int or value["version"] < 1:
        raise StitchPressError("商品模板版本不支持")
    if not isinstance(value["id"], str) or not re.fullmatch(r"[a-z0-9-]{1,80}", value["id"]):
        raise StitchPressError("商品模板ID无效")
    for key in ("name", "description", "source"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 200:
            raise StitchPressError("商品模板名称或来源无效")
    if value["ornament"] not in {"line", "arch", "rule", "grid"}:
        raise StitchPressError("商品模板装饰类型不支持")
    pages = value["pages"]
    if not isinstance(pages, dict) or set(pages) != {"portrait", "square"}:
        raise StitchPressError("商品模板缺少详情或方图布局")
    for layouts in pages.values():
        if not isinstance(layouts, dict) or set(layouts) != set(ROLES):
            raise StitchPressError("商品模板页面不完整")
        for layout in layouts.values():
            if not isinstance(layout, dict) or set(layout) != BOXES | {"split", "compare"}:
                raise StitchPressError("商品模板组件无效")
            if layout["split"] not in {"bottom", "side"} or layout["compare"] not in {
                "rows",
                "columns",
            }:
                raise StitchPressError("商品模板排布不支持")
            for name in BOXES:
                box = layout[name]
                if (
                    not isinstance(box, (list, tuple))
                    or len(box) != 4
                    or any(
                        type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1
                        for v in box
                    )
                    or box[0] >= box[2]
                    or box[1] >= box[3]
                ):
                    raise StitchPressError("商品模板组件越界")
            boxes = list(BOXES)
            for i, name in enumerate(boxes):
                a = layout[name]
                for other in boxes[i + 1 :]:
                    b = layout[other]
                    if min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1]):
                        raise StitchPressError("商品模板组件重叠")
    defaults = value["defaults"]
    if not isinstance(defaults, dict) or set(defaults) != {
        "background",
        "ink",
        "accent",
        "title",
        "footer",
        "care",
        "scenes",
    }:
        raise StitchPressError("商品模板默认配置无效")
    from .commerce import color
    from .text import FIELDS, expand

    for key in ("background", "ink", "accent"):
        color(defaults[key])
    for key in ("title", "footer", "care"):
        if not isinstance(defaults[key], str) or len(defaults[key]) > 3000:
            raise StitchPressError("商品模板默认文字无效")
        expand(defaults[key], dict.fromkeys(FIELDS, ""))
    if len(defaults["scenes"]) != 3 or any(
        type(n) is not int or not 0 <= n < 9 for n in defaults["scenes"]
    ):
        raise StitchPressError("商品模板默认场景无效")


def template_fingerprint(value):
    validate_template(value)
    return digest(value) if value is not None else "classic-v1"
