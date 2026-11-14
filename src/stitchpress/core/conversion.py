import io
from dataclasses import asdict, dataclass

import numpy as np
from PIL import Image, ImageCms, ImageOps

from stitchpress import __version__

from .errors import StitchPressError
from .identity import digest, image_pixel_sha, sha256
from .limits import MAX_SOURCE_BYTES, MAX_SOURCE_PIXELS
from .models import MAX_CELLS, ThreadColor, new_design


@dataclass(frozen=True)
class ConversionOptions:
    width: int
    height: int
    max_colors: int
    alpha_mode: str = "threshold"
    alpha_threshold: int = 128
    background: tuple[int, int, int] = (255, 255, 255)
    resampling: str = "BOX"

    def __post_init__(self):
        if any(type(n) is not int or n < 1 for n in (self.width, self.height, self.max_colors)):
            raise StitchPressError("针格和色数须为正整数")
        if self.width * self.height > MAX_CELLS or self.max_colors > 512:
            raise StitchPressError("转换超出试验版资源/色数上限")
        if self.alpha_mode not in {"threshold", "composite"} or self.resampling != "BOX":
            raise StitchPressError("未知alpha或采样策略")
        if type(self.alpha_threshold) is not int or not 1 <= self.alpha_threshold <= 255:
            raise StitchPressError("alpha阈值须为1–255")
        if (
            not isinstance(self.background, (tuple, list))
            or len(self.background) != 3
            or any(type(v) is not int or not 0 <= v <= 255 for v in self.background)
        ):
            raise StitchPressError("合成背景须为三个0–255整数")


def normalize_image(data: bytes, override_icc: bytes | None = None) -> tuple[Image.Image, dict]:
    if len(data) > MAX_SOURCE_BYTES:
        raise StitchPressError("原图文件超过128MiB上限")
    with Image.open(io.BytesIO(data)) as source:
        if getattr(source, "n_frames", 1) != 1:
            raise StitchPressError("当前蓝图仅支持单帧图像")
        if source.width * source.height > MAX_SOURCE_PIXELS:
            raise StitchPressError(f"原图{source.width:,}×{source.height:,}像素，超过1亿像素上限")
        source.load()
        orientation = source.getexif().get(274, 1)
        im = ImageOps.exif_transpose(source)
        icc = override_icc or source.info.get("icc_profile")
        alpha = im.convert("RGBA").getchannel("A")
        base = im.convert("RGB") if im.mode in {"P", "RGBA", "RGB", "LA", "L", "1"} else im
        assumption = None
        if icc:
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                # Grayscale profiles need an L input; RGB profiles need an RGB input.
                space = profile.profile.xcolor_space.strip()
                if space == "GRAY" and im.mode in {"L", "LA", "1"}:
                    base = im.convert("L")
                rgb = ImageCms.profileToProfile(
                    base,
                    profile,
                    ImageCms.createProfile("sRGB"),
                    outputMode="RGB",
                    renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                )
            except Exception as exc:
                raise StitchPressError("源ICC损坏或与颜色模式不符，请指定正确输入ICC") from exc
        elif im.mode in {"RGB", "RGBA", "P", "L", "LA", "1"}:
            rgb = im.convert("RGB")
            assumption = "untagged RGB/gray explicitly interpreted as sRGB"
        else:
            raise StitchPressError("无ICC的CMYK/特殊色彩输入不能猜测转换，请导入输入ICC")
        result = rgb.convert("RGBA")
        result.putalpha(alpha)
        # Avoid several full-resolution NumPy/RGBA copies for large source images.
        transparent = alpha.point(lambda value: 255 if value == 0 else 0)
        result.paste((0, 0, 0, 0), mask=transparent)
        transparent.close()
        return result, {
            "source_sha": sha256(data),
            "source_icc_sha": sha256(icc) if icc else None,
            "assumption": assumption,
            "orientation_applied": orientation,
            "normalized_pixel_sha": image_pixel_sha(result),
            "normalized_size": result.size,
            "working_space": "sRGB",
            "intent": "relative-colorimetric",
        }


def source_preview(data, override_icc=None, *, max_edge=2048):
    """Decode/thumbnail before color normalization; never used as a production master."""
    if len(data) > MAX_SOURCE_BYTES:
        raise StitchPressError("原图文件超过128MiB上限")
    with Image.open(io.BytesIO(data)) as source:
        if getattr(source, "n_frames", 1) != 1 or source.width * source.height > MAX_SOURCE_PIXELS:
            raise StitchPressError("原图须为单帧且不超过1亿像素")
        icc = override_icc or source.info.get("icc_profile")
        # JPEG can downsample in its decoder, instead of allocating the full raster.
        source.draft(source.mode, (max_edge, max_edge))
        source.thumbnail((max_edge, max_edge))
        preview = ImageOps.exif_transpose(source)
        stream = io.BytesIO()
        preview.save(stream, "TIFF" if preview.mode == "CMYK" else "PNG", icc_profile=icc)
        preview.close()
    normalized, _ = normalize_image(stream.getvalue(), icc)
    white = Image.new("RGBA", normalized.size, "white")
    white.alpha_composite(normalized)
    normalized.close()
    rgb = white.convert("RGB")
    white.close()
    return rgb


def _nearest(pixels: np.ndarray, palette: np.ndarray) -> np.ndarray:
    result = np.empty(len(pixels), dtype=np.int32)
    for start in range(0, len(pixels), 512):
        # int32 prevents uint8 subtraction wraparound and int16 square overflow.
        dif = pixels[start : start + 512, None, :].astype(np.int32) - palette[None].astype(np.int32)
        result[start : start + 512] = np.argmin(np.sum(dif * dif, axis=2), axis=1)
    return result


def convert_image(
    data: bytes,
    catalog: tuple[ThreadColor, ...],
    options: ConversionOptions,
    blank_mask: np.ndarray | None = None,
    subject_mask: np.ndarray | None = None,
    override_icc: bytes | None = None,
):
    if not catalog:
        raise StitchPressError("未选择线色目录")
    normalized, record = normalize_image(data, override_icc)
    sample = np.array(normalized.resize((options.width, options.height), Image.Resampling.BOX))
    alpha = sample[:, :, 3]
    if options.alpha_mode == "composite":
        a = alpha[:, :, None].astype(np.float64) / 255
        rgb = np.floor(sample[:, :, :3] * a + np.asarray(options.background) * (1 - a) + 0.5)
        valid = np.ones(alpha.shape, dtype=bool)
    else:
        rgb = sample[:, :, :3]
        valid = alpha >= options.alpha_threshold
    for mask in (blank_mask, subject_mask):
        if mask is not None and (mask.shape != alpha.shape or mask.dtype != np.bool_):
            raise StitchPressError("蒙版须为目标针格坐标的布尔矩阵，不允许隐式拉伸")
    if subject_mask is not None and np.any(subject_mask & ~valid):
        raise StitchPressError("主体蒙版与透明空白冲突，须人工修正")
    if blank_mask is not None:
        if subject_mask is not None and np.any(subject_mask & blank_mask):
            raise StitchPressError("主体与负空间蒙版冲突")
        valid &= ~blank_mask
    if not np.any(valid):
        raise StitchPressError("转换没有有效针格，不能产生伪合格母版")
    pixels, inverse, counts = np.unique(
        rgb[valid].astype(np.uint8), axis=0, return_inverse=True, return_counts=True
    )
    catalog_rgb = np.array([c.rgb for c in catalog])
    nearest = _nearest(pixels, catalog_rgb)
    histogram = np.bincount(nearest, weights=counts, minlength=len(catalog))
    selected = sorted((i for i, n in enumerate(histogram) if n), key=lambda i: (-histogram[i], i))[
        : options.max_colors
    ]
    selected.sort()  # Original catalog order, stable equal-distance tie breaking.
    assignments = _nearest(pixels, catalog_rgb[selected])[inverse]
    labels = np.full(alpha.shape, -1, dtype=np.int32)
    labels[valid] = assignments
    fingerprint = {
        "engine": __version__,
        "algorithm": "rgb-distance-frequency-v1",
        "numpy": np.__version__,
        "pillow": Image.__version__,
    }
    record.update(
        {
            "options": asdict(options),
            "catalog_sha": digest([c.key for c in catalog]),
            "blank_mask_sha": sha256(blank_mask.tobytes()) if blank_mask is not None else None,
            "subject_mask_sha": sha256(subject_mask.tobytes())
            if subject_mask is not None
            else None,
            "engine": fingerprint,
        }
    )
    design = new_design(
        options.width,
        options.height,
        [catalog[i] for i in selected],
        labels.tolist(),
        origin="converted",
        provenance=(("conversion_run_sha", digest(record)),),
    )
    record["result_sha"] = design.content_sha
    return design, record


def compose_local_image(original, candidate, region, grid_size, *, original_icc=None):
    """Normalize both inputs, then take only an explicitly aligned rectangle from candidate."""
    base, base_record = normalize_image(original, original_icc)
    edited, edited_record = normalize_image(candidate)
    try:
        if base.size != edited.size:
            raise StitchPressError("局部图像尺寸/方向不一致，不能拉伸贴回；请作为整体蓝图候选")
        width, height = grid_size
        x0, y0, x1, y1 = region
        if any(type(v) is not int for v in (*grid_size, *region)) or not (
            0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height
        ):
            raise StitchPressError("局部合成针格坐标无效")
        from math import ceil

        box = (
            x0 * base.width // width,
            y0 * base.height // height,
            ceil(x1 * base.width / width),
            ceil(y1 * base.height / height),
        )
        if box[0] >= box[2] or box[1] >= box[3]:
            raise StitchPressError("局部区域不足一个源像素，不能确定对应关系")
        composed = base.copy()
        composed.paste(edited.crop(box), box[:2])
        outside = (
            (0, 0, base.width, box[1]),
            (0, box[3], base.width, base.height),
            (0, box[1], box[0], box[3]),
            (box[2], box[1], base.width, box[3]),
        )
        for area in outside:
            if composed.crop(area).tobytes() != base.crop(area).tobytes():
                raise StitchPressError("局部合成外区核验失败")
        output = io.BytesIO()
        composed.save(output, "PNG")
        composed.close()
        return output.getvalue(), {
            "original": base_record,
            "candidate": edited_record,
            "pixel_region": box,
            "grid_region": region,
            "alignment": "human-confirmed-same-canvas",
            "outside_pixels": "exact-normalized-original",
            "method": "local-composite-v1",
        }
    finally:
        base.close()
        edited.close()


def convert_region(
    data,
    catalog,
    options,
    base,
    region,
    *,
    protected=frozenset(),
    override_icc=None,
    capacity=66,
    subject_mask=None,
):
    """Quantize only the selected grid samples; append colors and preserve all outer semantics."""
    from .models import PatchCell, apply_patch
    from .proposals import design_changes

    if (options.width, options.height) != (base.width, base.height):
        raise StitchPressError("局部转换不允许修改W/H，请创建整体转换候选")
    x0, y0, x1, y1 = region
    if any(type(v) is not int for v in region) or not (
        0 <= x0 < x1 <= base.width and 0 <= y0 < y1 <= base.height
    ):
        raise StitchPressError("局部转换区域越界")
    normalized, normalization = normalize_image(data, override_icc)
    sampled = normalized.resize((base.width, base.height), Image.Resampling.BOX)
    local = sampled.crop(region)
    raw = io.BytesIO()
    local.save(raw, "PNG")
    normalized.close()
    sampled.close()
    local.close()
    local_options = ConversionOptions(
        x1 - x0,
        y1 - y0,
        options.max_colors,
        alpha_mode=options.alpha_mode,
        alpha_threshold=options.alpha_threshold,
        background=options.background,
        resampling=options.resampling,
    )
    if subject_mask is not None and (
        subject_mask.shape != (base.height, base.width) or subject_mask.dtype != np.bool_
    ):
        raise StitchPressError("主体蒙版须为当前针格布尔矩阵，不能隐式缩放")
    regional, run = convert_image(
        raw.getvalue(),
        catalog,
        local_options,
        subject_mask=subject_mask[y0:y1, x0:x1] if subject_mask is not None else None,
    )
    palette = list(base.palette)
    lookup = {c.key: i for i, c in enumerate(palette)}
    appended = []
    for color in regional.palette:
        if color.key in lookup:
            if palette[lookup[color.key]].rgb != color.rgb:
                raise StitchPressError("目录身份相同但RGB不同，不能改写区域外色板")
        else:
            lookup[color.key] = len(palette)
            palette.append(color)
            appended.append(color)
    cells = []
    for y in range(y0, y1):
        for x in range(x0, x1):
            index = regional.labels[y - y0][x - x0]
            after = lookup[regional.palette[index].key] if index >= 0 else -1
            if base.labels[y][x] != after:
                cells.append(PatchCell(x, y, base.labels[y][x], after))
    if not cells:
        raise StitchPressError("局部转换没有针格变化，无需生成新修订")
    allowed = frozenset((x, y) for y in range(y0, y1) for x in range(x0, x1))
    result = apply_patch(
        base,
        base_sha=base.content_sha,
        cells=tuple(cells),
        allowed=allowed,
        protected=protected,
        append_colors=tuple(appended),
        capacity=min(capacity, options.max_colors),
    )
    if any((change["x"], change["y"]) not in allowed for change in design_changes(base, result)):
        raise StitchPressError("局部转换改变了区域外语义")
    return result, {
        "kind": "local-conversion-v1",
        "base_sha": base.content_sha,
        "result_sha": result.content_sha,
        "region": region,
        "cells": [asdict(c) for c in cells],
        "source": normalization,
        "regional_run": run,
        "append_colors": [asdict(c) for c in appended],
        "options": asdict(options),
        "external_semantics": "unchanged",
    }
