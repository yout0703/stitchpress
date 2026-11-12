"""Shared component renderer for the versioned commerce template snapshots."""

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

from .conversion import normalize_image
from .errors import ResourceError
from .text import checked_font, expand


@lru_cache(maxsize=1)
def commerce_font():
    return (Path(__file__).parents[1] / "resources/fonts/NotoSansCJKsc-Regular.otf").read_bytes()


def render_template(renderer, target):
    settings, template = renderer.settings, renderer.settings.template
    square = target.startswith("square-")
    content = settings.squares[int(target[-1]) - 1] if square else target.removeprefix("detail-")
    role = "scene" if content.startswith("scene-") else content
    layout = template["pages"]["square" if square else "portrait"][role]
    size = (800, 800) if square else (settings.detail_width, settings.detail_height)
    w, h = size
    f = min(w / 1200, h / 1100)
    # Printed facts remain authoritative; display dimensions at a readable precision.
    fields = dict(renderer.fields)
    for key in ("width_cm", "height_cm"):
        fields[key] = f"{float(fields[key]):.2f}".rstrip("0").rstrip(".")
    image = Image.new("RGB", size, tuple(settings.background))
    draw = ImageDraw.Draw(image)
    ink, accent = tuple(settings.ink), tuple(settings.accent)
    boxes = {
        key: tuple(round(v * (w if i % 2 == 0 else h)) for i, v in enumerate(layout[key]))
        for key in ("title", "kicker", "body", "caption", "footer")
    }
    font_data = renderer.context.get("font_data") or commerce_font()
    fonts = {}

    def text(value, box, pixels, *, fill=ink, lines=2):
        value = expand(value, fields)
        pixels = max(10, round(pixels * f))
        key = (pixels, value)
        if key not in fonts:
            fonts[key] = checked_font(pixels, font_data, value)
        font = fonts[key]
        wrapped, line = [], ""
        for c in value:
            if c == "\n" or font.getlength(line + c) > box[2] - box[0]:
                if not line and c != "\n":
                    raise ResourceError("模板文字单字超出区域，请调整字号或尺寸")
                wrapped.append(line)
                line = "" if c == "\n" else c
            else:
                line += c
        if line:
            wrapped.append(line)
        spacing = round(pixels * 1.25)
        if len(wrapped) > lines or len(wrapped) * spacing > box[3] - box[1]:
            raise ResourceError("模板文字溢出，请缩短文字或调整字号；不会截断文字")
        for i, line in enumerate(wrapped):
            draw.text((box[0], box[1] + i * spacing), line, font=font, fill=fill, anchor="lt")

    ornament = template["ornament"]
    if ornament == "line":
        draw.line(
            (w * 0.075, h * 0.90, w * 0.925, h * 0.90), fill=accent, width=max(1, round(f * 2))
        )
    elif ornament == "arch":
        draw.rounded_rectangle(
            (w * 0.035, h * 0.63, w * 0.965, h * 0.90),
            radius=round(w * 0.025),
            fill=(255, 252, 247),
        )
    elif ornament == "rule":
        draw.line((w * 0.07, h * 0.05, w * 0.07, h * 0.95), fill=accent, width=max(1, round(f * 2)))
        draw.rectangle((w * 0.045, h * 0.055, w * 0.095, h * 0.092), fill=accent)
        draw.line((w * 0.14, h * 0.91, w * 0.89, h * 0.91), fill=accent, width=max(1, round(f)))
    else:
        draw.rectangle((0, 0, w, h * 0.018), fill=accent)
        draw.rounded_rectangle(
            (w * 0.04, h * 0.23, w * 0.96, h * 0.895), radius=round(w * 0.02), fill=(255, 255, 255)
        )
    labels = {
        "scene": "家居展示",
        "spec": "作品规格",
        "compare": "原图与效果对照",
        "print": "印花符号细节",
        "care": "使用与洗护",
        "closeup": "局部细节",
        "showcase": "作品全貌",
    }
    number = (
        int(content[-1])
        if role == "scene"
        else {"spec": 4, "compare": 5, "print": 6, "care": 7}.get(role, 1)
    )
    text(f"{number:02d}  /  {labels[role]}", boxes["kicker"], 23, fill=accent, lines=1)
    text(settings.title, boxes["title"], settings.font_pixels * 0.83, lines=2)
    body = boxes["body"]
    x0, y0, x1, y1 = body
    bw, bh = x1 - x0, y1 - y0
    art_box = None
    if role == "scene":
        from .commerce import SceneSettings

        art_box = renderer.scene(
            image, body, SceneSettings(**settings.scenes[int(content[-1]) - 1])
        )
    elif role in {"spec", "care"}:
        if layout["split"] == "side":
            art = (x0, y0, x0 + round(bw * 0.52), y1)
            notes = (x0 + round(bw * 0.58), y0 + round(bh * 0.06), x1, y1 - round(bh * 0.03))
        else:
            art = (x0, y0, x1, y0 + round(bh * 0.58))
            notes = (x0, y0 + round(bh * 0.65), x1, y1)
        renderer.contained(image, renderer.subject, art)
        if role == "spec":
            facts = [
                "画心  {width_cm} × {height_cm} cm",
                "针格  {width} × {height} 格",
                "绣制  {stitches} 针 · {colors} 色",
                "布号  {ct} CT",
                "货号  {code}",
            ]
            row_h = (notes[3] - notes[1]) / len(facts)
            for i, fact in enumerate(facts):
                yy = round(notes[1] + i * row_h)
                text(
                    fact,
                    (notes[0], yy, notes[2], round(yy + row_h * 0.90)),
                    25 if square else 27,
                    lines=2,
                )
                draw.line(
                    (notes[0], round(yy + row_h * 0.92), notes[2], round(yy + row_h * 0.92)),
                    fill=accent,
                    width=max(1, round(f)),
                )
        else:
            text(settings.care, notes, 27, lines=14)
    elif role == "compare":
        source = renderer.context.get("source_data")
        if not source:
            raise ResourceError("效果对比缺少对应原图，请先导入或取消该导出项")
        normalized, _ = normalize_image(source, renderer.context.get("input_icc"))
        white = Image.new("RGBA", normalized.size, "white")
        white.alpha_composite(normalized)
        gap = round(28 * f)
        if layout["compare"] == "columns":
            mid = (x0 + x1) // 2
            first, second = (x0, y0, mid - gap, y1), (mid + gap, y0, x1, y1)
        else:
            mid = (y0 + y1) // 2
            first, second = (x0, y0, x1, mid - gap), (x0, mid + gap, x1, y1)
        rgb = white.convert("RGB")
        for source_image, area, label in (
            (rgb, first, "对应原图"),
            (renderer.subject, second, "当前展示效果"),
        ):
            renderer.contained(
                image, source_image, (area[0], area[1], area[2], area[3] - round(38 * f))
            )
            text(label, (area[0], area[3] - round(30 * f), area[2], area[3]), 20, lines=1)
        rgb.close()
        white.close()
        normalized.close()
    elif role == "print":
        from .render import Renderer, screen_rgb

        symbols = Renderer(
            renderer.design,
            renderer.context["library"],
            renderer.context["mapping"],
            renderer.context["fabric"],
        )
        cx, cy = (
            round(float(settings.closeup_x) * (renderer.design.width - 1)),
            round(float(settings.closeup_y) * (renderer.design.height - 1)),
        )
        span = max(2, round(72 / float(settings.closeup_zoom)))
        x, y = max(0, cx - span // 2), max(0, cy - span // 2)
        endx, endy = min(renderer.design.width, x + span), min(renderer.design.height, y + span)
        chart = Image.new("CMYK", ((endx - x) * 24, (endy - y) * 24), (0, 0, 0, 0))
        for yy in range(y, endy):
            for xx in range(x, endx):
                index = renderer.design.labels[yy][xx]
                if index >= 0:
                    chart.paste(symbols.tile(index, 24, 24), ((xx - x) * 24, (yy - y) * 24))
        rgb = screen_rgb(chart, renderer.context["library"].icc)
        renderer.contained(image, rgb, body)
        rgb.close()
        chart.close()
    else:
        close = renderer.closeup() if role == "closeup" else None
        renderer.contained(image, close or renderer.subject, body)
        if close:
            close.close()
    captions = {
        "scene": "场景合成示意 · 画作按原比例呈现",
        "spec": "规格引用当前作品 · 画心尺寸不含留白",
        "compare": "展示来源可切换 · 请以实际商品为准",
        "print": "符号来自当前图纸 · 局部放大示意",
        "care": "请保留产品说明，并确认适用的洗护方式",
        "closeup": "局部放大展示 · 请以实际商品为准",
        "showcase": "当前作品展示 · 请以实际商品为准",
    }
    text(captions[role], boxes["caption"], 22, lines=2)
    text(("草稿 · " if renderer.demo else "") + settings.footer, boxes["footer"], 20, lines=2)
    return image, dict(
        content=content,
        size=size,
        design_sha=renderer.design.content_sha,
        template_id=template["id"],
        template_version=template["version"],
        template_sha=renderer.template_sha,
        scene_art_box=art_box,
        source_type=settings.art_source,
        illustrative_scene=role == "scene",
        demo=renderer.demo,
    )
