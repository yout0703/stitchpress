"""Explicit grid-coordinate rectangles; never resize masks or infer white as background."""

import numpy as np

from .errors import StitchPressError


def annotation_masks(record, width, height, source_sha=None):
    masks = {
        k: np.zeros((height, width), dtype=np.bool_) for k in ("subject", "blank", "protected")
    }
    if not record:
        return masks
    if record.get("schema_version") != 1 or record.get("grid") != [width, height]:
        raise StitchPressError("蒙版针格语境已改变，请检查或清除旧蒙版；不会自动缩放")
    if record.get("source_sha") != source_sha:
        raise StitchPressError("蒙版来源已改变，不能自动应用到另一张图片")
    regions = record.get("regions")
    if not isinstance(regions, list) or len(regions) > 2000:
        raise StitchPressError("蒙版区域记录无效或超过2000处")
    for item in regions:
        kind = item.get("kind")
        if kind not in masks:
            raise StitchPressError("未知蒙版类型")
        region = item.get("region")
        if (
            not isinstance(region, (list, tuple))
            or len(region) != 4
            or any(type(v) is not int for v in region)
        ):
            raise StitchPressError("蒙版坐标须为四个整数")
        x0, y0, x1, y1 = region
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise StitchPressError("蒙版区域越界")
        masks[kind][y0:y1, x0:x1] = True
    if np.any(masks["subject"] & masks["blank"]):
        raise StitchPressError("主体有针与负空间留空标注冲突，请先修正")
    return masks
