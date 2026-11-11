"""Portable SKU and reading-page settings; no UI or machine defaults."""

from dataclasses import asdict, dataclass, replace
from decimal import Decimal

from .errors import StitchPressError
from .identity import digest
from .layout import Fabric, decimal

PAPERS = {"A4": ("21", "29.7"), "A3": ("29.7", "42"), "A2": ("42", "59.4")}


def margin_value(value: str, unit: str, pitch: str, *, to_cm: bool) -> str:
    """Use axis-specific actual pitch, never nominal CT, for stitch-count units."""
    factors = {"cm": Decimal(1), "mm": Decimal("0.1"), "cells": decimal(pitch)}
    if unit not in factors or factors[unit] <= 0 or decimal(value) < 0:
        raise StitchPressError("留白单位或数值无效")
    return format(decimal(value) * factors[unit] if to_cm else decimal(value) / factors[unit], "f")


@dataclass(frozen=True)
class ShowcaseSettings:
    style: str = "x"
    cell: int = 18

    def __post_init__(self):
        if self.style not in {"x", "dot"} or type(self.cell) is not int or not 6 <= self.cell <= 36:
            raise StitchPressError("模拟样式须为X或点图，像素密度须6–36整数；不改变生产格距")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ReferenceSettings:
    paper: str = "A4"
    custom_width_cm: str = "21"
    custom_height_cm: str = "29.7"
    landscape: bool = False
    scale: str = "1"
    overlap: int = 0
    style: str = "symbols"
    minor_grid: bool = True

    def __post_init__(self):
        if self.paper not in {*PAPERS, "custom"}:
            raise StitchPressError("纸张仅支持A4/A3/A2或自定义")
        if any(
            not isinstance(v, str)
            for v in (self.scale, self.custom_width_cm, self.custom_height_cm)
        ):
            raise StitchPressError("纸张尺寸与倍率须保存为十进制文本")
        if not 1 <= decimal(self.scale) <= 4:
            raise StitchPressError("阅读倍率须为1–4，不能缩小冒充原倍率")
        if any(
            not Decimal("10") <= decimal(v) <= Decimal("120")
            for v in (self.custom_width_cm, self.custom_height_cm)
        ):
            raise StitchPressError("自定义纸张边长须为10–120cm")
        if type(self.overlap) is not int or not 0 <= self.overlap <= 30:
            raise StitchPressError("重叠格数须为0–30整数")
        if self.style not in {"symbols", "pale", "color"}:
            raise StitchPressError("未知查图样式")
        if type(self.landscape) is not bool or type(self.minor_grid) is not bool:
            raise StitchPressError("方向和网格选项须为布尔值")

    @property
    def paper_cm(self):
        size = PAPERS.get(self.paper, (self.custom_width_cm, self.custom_height_cm))
        return tuple(reversed(size)) if self.landscape else size

    @property
    def factory_compatible(self):
        return self == ReferenceSettings()

    def to_dict(self):
        return asdict(self)


def resize_fabric(fabric: Fabric, grid_width: int, *, target_width_cm: str) -> Fabric:
    """Preserve the calibrated X/Y aspect; never change stitch counts to resize cloth."""
    if type(grid_width) is not int or grid_width < 1:
        raise StitchPressError("针格宽度无效")
    width = decimal(target_width_cm)
    if width <= 0:
        raise StitchPressError("目标画心宽度必须大于0")
    ratio = width / (decimal(fabric.sx) * grid_width)
    return replace(
        fabric,
        sx=str(width / grid_width),
        sy=str(decimal(fabric.sy) * ratio),
        source="explicit-target-width",
    )


@dataclass(frozen=True)
class Preset:
    name: str
    fabric: dict
    reference: dict
    schema_version: int = 1

    def __post_init__(self):
        if self.schema_version != 1 or not isinstance(self.name, str) or not self.name.strip():
            raise StitchPressError("预设名称或格式版本无效")
        Fabric(**self.fabric)
        ReferenceSettings(**self.reference)

    @property
    def revision(self):
        return digest(asdict(self))

    def to_dict(self):
        return asdict(self)
