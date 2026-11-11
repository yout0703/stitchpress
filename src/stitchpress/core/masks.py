"""Explicit grid-coordinate shapes; never resize masks or infer white as background."""

import numpy as np

from .errors import StitchPressError


def annotation_masks(record, width, height, source_sha=None):
    masks = {
        k: np.zeros((height, width), dtype=np.bool_) for k in ("subject", "blank", "protected")
    }
    if not record:
        return masks
    if not isinstance(record, dict):
        raise StitchPressError("蒙版记录须为对象")
    if record.get("schema_version") != 1 or record.get("grid") != [width, height]:
        raise StitchPressError("蒙版针格语境已改变，请检查或清除旧蒙版；不会自动缩放")
    if record.get("source_sha") != source_sha:
        raise StitchPressError("蒙版来源已改变，不能自动应用到另一张图片")
    regions = record.get("regions")
    if not isinstance(regions, list) or len(regions) > 2000:
        raise StitchPressError("蒙版区域记录无效或超过2000处")
    work = 0
    for item in regions:
        if not isinstance(item, dict):
            raise StitchPressError("蒙版区域须为对象")
        kind = item.get("kind")
        if kind not in masks:
            raise StitchPressError("未知蒙版类型")
        if item.get("shape", "rectangle") == "polygon":
            if (
                item.get("fill_rule", "cell-center-even-odd-rational-v1")
                != "cell-center-even-odd-rational-v1"
            ):
                raise StitchPressError("不支持的多边形填充规则")
            points = item.get("points")
            if not isinstance(points, list):
                raise StitchPressError("多边形顶点无效")
            work += len(points) * height
            if work > 5_000_000:
                raise StitchPressError("多边形扫描工作量超过本地上限，请简化顶点或拆分工程")
            masks[kind] |= polygon_mask(points, width, height)
            continue
        if item.get("shape", "rectangle") != "rectangle":
            raise StitchPressError("未知蒙版形状，不能忽略")
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


def polygon_mask(points, width, height):
    """Fill cell centers using exact rational intersections and the even-odd rule."""
    from fractions import Fraction
    from math import ceil

    if not isinstance(points, list) or not 3 <= len(points) <= 512:
        raise StitchPressError("多边形须3–512个针格顶点")
    for point in points:
        if (
            not isinstance(point, (tuple, list))
            or len(point) != 2
            or any(type(v) is not int for v in point)
            or not (0 <= point[0] <= width and 0 <= point[1] <= height)
        ):
            raise StitchPressError("多边形顶点须为针格边界内的整数坐标")
    edges = list(zip(points, points[1:] + points[:1], strict=True))
    if sum(a[0] * b[1] - b[0] * a[1] for a, b in edges) == 0:
        raise StitchPressError("多边形面积为0，请重新标注")
    result = np.zeros((height, width), dtype=np.bool_)
    for y in range(min(p[1] for p in points), max(p[1] for p in points)):
        cy = Fraction(2 * y + 1, 2)
        crossings = []
        for (x0, y0), (x1, y1) in edges:
            if (y0 <= cy < y1) or (y1 <= cy < y0):
                crossings.append(Fraction(x0) + (cy - y0) * Fraction(x1 - x0, y1 - y0))
        crossings.sort()
        for left, right in zip(crossings[::2], crossings[1::2], strict=True):
            x0, x1 = ceil(left - Fraction(1, 2)), ceil(right - Fraction(1, 2))
            result[y, max(0, x0) : min(width, x1)] = True
    return result
