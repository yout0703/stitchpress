"""Portable blueprint normalization and original-file provenance; no UI dependencies."""

import io

from PIL import Image, ImageCms

from .conversion import normalize_image
from .errors import StitchPressError
from .identity import digest, sha256

ORIGINS = {
    "local-import": "外部文件（来源由你核对）",
    "customer-image": "客户来图",
    "self-authored": "自行绘制蓝图",
    "external-ai": "外部AI生成/编辑文件",
    "local-composite": "同画幅局部合成",
}


def prepare_blueprint(data, *, origin, note="", provider="", model="", parent_sha=None):
    if (
        origin not in ORIGINS
        or len(data) > 64 * 1024**2
        or any(not isinstance(v, str) or len(v) > 8000 for v in (note, provider, model))
    ):
        raise StitchPressError("蓝图来源、文字或资源大小无效")
    normalized, color = normalize_image(data)
    buffer = io.BytesIO()
    normalized.save(
        buffer,
        "PNG",
        icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
    )
    payload = buffer.getvalue()
    record = {
        "schema_version": 1,
        "origin": origin,
        "note": note,
        "provider": provider,
        "model": model,
        "parent_source_sha": parent_sha,
        "original_sha": sha256(data),
        "normalized_sha": sha256(payload),
        "normalized_size": list(normalized.size),
        "color": color,
        "provider_call_verified": False,
    }
    normalized.close()
    record["revision"] = digest(record)
    if len(payload) > 64 * 1024**2:
        raise StitchPressError("规范蓝图超过64MiB作品资源上限，不能静默降质")
    return payload, record


def check_blueprint(record, assets):
    if record.get("schema_version") != 1 or record.get("origin") not in ORIGINS:
        raise StitchPressError("蓝图记录版本或来源无效")
    for field in ("original", "normalized"):
        path = record.get(field + "_asset")
        if path not in assets or sha256(assets[path]) != record.get(field + "_sha"):
            raise StitchPressError("蓝图原件/规范输入SHA核对失败")
    with Image.open(io.BytesIO(assets[record["normalized_asset"]])) as image:
        if image.width * image.height > 40_000_000:
            raise StitchPressError("规范蓝图超出像素上限")
        image.load()
        if (
            image.format != "PNG"
            or image.mode != "RGBA"
            or list(image.size) != record.get("normalized_size")
            or sha256(image.tobytes()) != record.get("color", {}).get("normalized_pixel_sha")
        ):
            raise StitchPressError("规范蓝图尺寸/模式/像素与来源记录不一致")
    if record.get("edited_asset"):
        asset = record["edited_asset"]
        if asset not in assets or sha256(assets[asset]) != record.get("edited_sha"):
            raise StitchPressError("局部编辑原件SHA不符")
    content = {
        k: v
        for k, v in record.items()
        if k
        not in {
            "original_asset",
            "normalized_asset",
            "created_at",
            "base_design_sha",
            "revision",
            "composite",
            "base_source_sha",
            "label",
            "edited_asset",
            "edited_sha",
        }
    }
    if record.get("revision") != digest(content):
        raise StitchPressError("蓝图版本记录已改变")
