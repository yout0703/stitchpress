import io
from dataclasses import asdict, dataclass

import numpy as np
from PIL import Image, ImageCms, ImageOps

from stitchpress import __version__

from .errors import StitchPressError
from .identity import digest, sha256
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


def normalize_image(data: bytes, override_icc: bytes | None = None) -> tuple[Image.Image, dict]:
    with Image.open(io.BytesIO(data)) as source:
        if getattr(source, "n_frames", 1) != 1:
            raise StitchPressError("当前蓝图仅支持单帧图像")
        if source.width * source.height > 40_000_000:
            raise StitchPressError("蓝图超过4000万像素试验上限")
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
        rgba = np.array(rgb.convert("RGBA"))
        rgba[:, :, 3] = np.asarray(alpha)
        rgba[rgba[:, :, 3] == 0, :3] = 0
        result = Image.fromarray(rgba)
        return result, {
            "source_sha": sha256(data),
            "source_icc_sha": sha256(icc) if icc else None,
            "assumption": assumption,
            "orientation_applied": orientation,
            "normalized_pixel_sha": sha256(result.tobytes()),
            "normalized_size": result.size,
            "working_space": "sRGB",
            "intent": "relative-colorimetric",
        }


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
):
    if not catalog:
        raise StitchPressError("未选择线色目录")
    normalized, record = normalize_image(data)
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
