"""Portable commerce presets and isolated, revision-bound contact sheets."""

import copy
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from stitchpress.core.commerce import TARGETS, CommerceRenderer, CommerceSettings, SceneSettings
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest, sha256
from stitchpress.platform.files import atomic_write

from .export import Cancelled
from .store import MAX_PROJECT_BYTES, Project


def apply_template(settings, template, *, keep_scenes=True, reset_text=False):
    data = settings.to_dict()
    data["template"] = copy.deepcopy(template)
    defaults = template["defaults"] if template else CommerceSettings().to_dict()
    previous = settings.template["defaults"] if settings.template else CommerceSettings().to_dict()
    for key in ("background", "ink", "accent"):
        data[key] = defaults[key]
    for key in ("title", "footer", "care"):
        if reset_text or data[key] == previous[key]:
            data[key] = defaults[key]
    if not keep_scenes:
        data["scenes"] = (
            tuple(asdict(SceneSettings(scene=n)) for n in defaults["scenes"])
            if template
            else CommerceSettings().scenes
        )
    return CommerceSettings(**data)


def template_font_sha(project):
    settings = CommerceSettings(**project.entities.get("commerce", {}))
    if settings.font_asset:
        return sha256(project.assets[settings.font_asset])
    if settings.template:
        from stitchpress.core.commerce_layout import commerce_font

        return sha256(commerce_font())
    return None


def fingerprint(project):
    return digest(
        dict(
            project=project.project_id,
            template_font_sha=template_font_sha(project),
            entities=project.entities,
            assets={k: sha256(v) for k, v in project.assets.items()},
        )
    )


def preset_project(settings, assets, metadata, name, *, inherited_font=None):
    if not name.strip() or len(name.strip()) > 80:
        raise StitchPressError("预设名称须为1–80字")
    data = settings.to_dict()
    for key in ("title", "footer", "care"):
        for field in ("title", "code"):
            literal = metadata.get(field, "")
            if literal and literal not in ("{title}", "{code}"):
                data[key] = data[key].replace(literal, "{" + field + "}")
    assets = dict(assets)
    if not data["font_asset"] and data["template"]:
        from stitchpress.core.commerce_layout import commerce_font

        raw = commerce_font()
        font_name = "resources/commerce-fonts/" + sha256(raw) + ".otf"
        assets[font_name] = raw
        data["font_asset"] = font_name
    elif not data["font_asset"] and inherited_font:
        data["font_asset"] = inherited_font
    value = CommerceSettings(**data)
    used = {scene["background_asset"] for scene in value.scenes if scene.get("background_asset")}
    if value.font_asset:
        used.add(value.font_asset)
    if not used <= assets.keys():
        raise StitchPressError("预设引用的背景或字体缺失")
    result = Project.create("")
    result.entities.update(
        commerce=value.to_dict(),
        commerce_preset=dict(
            format="StitchPress.Commerce.Preset.v1", id=uuid4().hex, version=1, name=name.strip()
        ),
    )
    result.assets = {key: assets[key] for key in used}
    result.encode()
    return result


def save_preset(preset, path):
    path = Path(path)
    if path.exists():
        raise StitchPressError("预设已存在，请另存新版本")
    atomic_write(path, preset.encode())


def load_preset(path):
    path = Path(path)
    if path.stat().st_size > MAX_PROJECT_BYTES:
        raise StitchPressError("预设超过资源上限")
    project = Project.decode(path.read_bytes())
    info = project.entities.get("commerce_preset", {})
    if (
        set(project.entities) != {"metadata", "commerce", "commerce_preset"}
        or info.get("format") != "StitchPress.Commerce.Preset.v1"
        or type(info.get("version")) is not int
        or info["version"] < 1
        or not isinstance(info.get("name"), str)
    ):
        raise StitchPressError("不是支持的商品模板预设")
    clean = preset_project(
        CommerceSettings(**project.entities["commerce"]), project.assets, {}, info["name"]
    )
    if clean.assets.keys() != project.assets.keys():
        raise StitchPressError("预设含未引用资源")
    return project


def apply_preset(current, preset, *, keep_scenes=True, reset_text=False):
    stored = CommerceSettings(**preset.entities["commerce"])
    data = stored.to_dict()
    # A style preset does not silently change the current deliverable's size, content or source.
    for key in (
        "detail_width",
        "detail_height",
        "detail_dpi",
        "square_dpi",
        "squares",
        "art_source",
    ):
        data[key] = getattr(current, key)
    if keep_scenes:
        data["scenes"] = current.scenes
    if not reset_text:
        previous = (
            current.template["defaults"] if current.template else CommerceSettings().to_dict()
        )
        for key in ("title", "footer", "care"):
            if getattr(current, key) != previous[key]:
                data[key] = getattr(current, key)
    return CommerceSettings(**data)


def render_sheet(project, folder, *, cancel=lambda: False, catalog=False):
    from .reference_package import render_context

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    context, _ = render_context(project)
    renderer = CommerceRenderer(
        context, CommerceSettings(**project.entities.get("commerce", {})), project.assets, demo=True
    )
    pages = []
    try:
        for target in TARGETS:
            if cancel():
                raise Cancelled("商品预览已取消")
            try:
                image, checks = renderer.render(target)
                path = folder / (target + ".png")
                image.save(path)
                image.thumbnail((240, 240))
                thumb = folder / (target + "-thumb.png")
                image.save(thumb)
                image.close()
                pages.append(
                    dict(target=target, preview=str(path), thumb=str(thumb), checks=checks)
                )
            except StitchPressError as exc:
                pages.append(dict(target=target, error=str(exc)))
        if cancel():
            raise Cancelled("商品预览已取消")
        covers = {}
        if catalog:
            from stitchpress.core.commerce_templates import templates

            for template in templates():
                if cancel():
                    raise Cancelled("模板缩略图已取消")
                variant = CommerceRenderer(
                    context,
                    apply_template(renderer.settings, template, keep_scenes=False),
                    project.assets,
                    demo=True,
                )
                try:
                    image, _ = variant.render("detail-scene-1")
                    image.thumbnail((180, 240))
                    path = folder / ("cover-" + template["id"] + ".png")
                    image.save(path)
                    image.close()
                    covers[template["id"]] = str(path)
                except StitchPressError:
                    pass  # The full-set board reports the selected template's exact error.
                finally:
                    variant.subject.close()
        return dict(fingerprint=fingerprint(project), pages=pages, covers=covers)
    finally:
        renderer.subject.close()


def recommended_settings():
    from stitchpress.core.commerce_templates import template_by_id

    return apply_template(CommerceSettings(), template_by_id("gallery"), keep_scenes=False)
