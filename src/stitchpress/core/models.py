from dataclasses import asdict, dataclass, replace
from functools import cached_property
from typing import Any, Literal
from uuid import uuid4

from .errors import StitchPressError
from .identity import digest

MAX_CELLS = 1_000_000  # Provisional local alpha guard, not a released performance promise.


@dataclass(frozen=True)
class ThreadColor:
    brand: str
    code: str
    name: str
    rgb: tuple[int, int, int]
    catalog_revision: str

    def __post_init__(self):
        if not all(
            isinstance(v, str) and v for v in (self.brand, self.code, self.catalog_revision)
        ):
            raise StitchPressError("线材品牌、原代码和目录修订必须为非空文本")
        if not isinstance(self.name, str):
            raise StitchPressError("线色名称必须为文本")
        if len(self.rgb) != 3 or any(type(c) is not int or not 0 <= c <= 255 for c in self.rgb):
            raise StitchPressError("RGB必须为三个0–255整数")

    @cached_property
    def key(self) -> str:
        return digest([self.brand, self.code, self.catalog_revision])

    @classmethod
    def from_dict(cls, data: dict) -> "ThreadColor":
        return cls(**{**data, "rgb": tuple(data["rgb"])})


@dataclass(frozen=True)
class Design:
    width: int
    height: int
    palette: tuple[ThreadColor, ...]
    labels: tuple[tuple[int, ...], ...]
    revision_id: str
    origin: Literal["imported", "converted", "patched"] = "imported"
    parent_revision_id: str | None = None
    provenance: tuple[tuple[str, str], ...] = ()
    stitch_type: str = "full_cross"

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (self.width, self.height)):
            raise StitchPressError("针格宽高必须为正整数")
        if self.width * self.height > MAX_CELLS:
            raise StitchPressError(f"试验版暂限{MAX_CELLS}个总格，不能静默缩图")
        if (
            not isinstance(self.revision_id, str)
            or not self.revision_id
            or self.stitch_type != "full_cross"
        ):
            raise StitchPressError("当前只支持明确声明的全针母版")
        if self.origin not in {"imported", "converted", "patched"}:
            raise StitchPressError("未知母版来源")
        if self.origin == "patched" and not self.parent_revision_id:
            raise StitchPressError("补丁母版必须保留父修订")
        if len(self.labels) != self.height or any(len(row) != self.width for row in self.labels):
            raise StitchPressError("针格矩阵与声明的宽高不一致")
        if any(
            type(v) is not int or v < -1 or v >= len(self.palette)
            for row in self.labels
            for v in row
        ):
            raise StitchPressError("非法色板索引，只有-1表示无针")
        if len({c.key for c in self.palette}) != len(self.palette):
            raise StitchPressError("母版含重复线材身份，必须明确修订合并")
        if len(self.palette) > 512:
            raise StitchPressError("色板超过512色数据上限")
        if any(len(p) != 2 or not all(isinstance(v, str) for v in p) for p in self.provenance):
            raise StitchPressError("母版来源须为具名文本记录")

    @cached_property
    def counts(self) -> tuple[int, ...]:
        result = [0] * len(self.palette)
        for row in self.labels:
            for v in row:
                if v >= 0:
                    result[v] += 1
        return tuple(result)

    @cached_property
    def content_sha(self) -> str:
        return digest(
            {
                "width": self.width,
                "height": self.height,
                "palette": [asdict(c) for c in self.palette],
                "labels": self.labels,
                "stitch_type": self.stitch_type,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "Design":
        if data.get("schema_version") != 1:
            raise StitchPressError("不支持的母版格式版本；原件未改动")
        fields = {k: v for k, v in data.items() if k != "schema_version"}
        fields["palette"] = tuple(ThreadColor.from_dict(c) for c in fields["palette"])
        fields["labels"] = tuple(tuple(row) for row in fields["labels"])
        fields["provenance"] = tuple(tuple(p) for p in fields.get("provenance", ()))
        return cls(**fields)


def new_design(width: int, height: int, palette, labels, **kwargs) -> Design:
    return Design(
        width,
        height,
        tuple(palette),
        tuple(tuple(row) for row in labels),
        revision_id=uuid4().hex,
        **kwargs,
    )


def validate_catalog_membership(design: Design, catalog: tuple[ThreadColor, ...]):
    if not catalog:
        raise StitchPressError("正式印花需要已导入的线色目录，不能按RGB猜测线号")
    known = {(c.brand, c.code) for c in catalog}
    unresolved = [
        f"{c.brand} {c.code}"
        for c, n in zip(design.palette, design.counts, strict=True)
        if n and (c.brand, c.code) not in known
    ]
    if unresolved:
        raise StitchPressError("线号尚未在当前目录解析，请先修正：" + ", ".join(unresolved[:10]))


@dataclass(frozen=True)
class ReviewRecord:
    target_sha: str
    reviewer: str
    overall: bool
    detail_regions: tuple[tuple[int, int, int, int], ...]
    negative_space: bool
    source_match: bool
    notes: str
    source_sha: str | None = None
    brief_sha: str | None = None
    annotations_sha: str | None = None
    input_icc_sha: str | None = None
    reviewed_at: str | None = None

    def validate(self, design: Design) -> None:
        import re

        for key in ("source_sha", "brief_sha", "annotations_sha", "input_icc_sha"):
            value = getattr(self, key)
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value)
            ):
                raise StitchPressError("审核来源指纹格式无效")
        if not isinstance(self.reviewer, str) or not isinstance(self.notes, str):
            raise StitchPressError("审核人和说明须为文本")
        if any(type(v) is not bool for v in (self.overall, self.negative_space, self.source_match)):
            raise StitchPressError("审核勾选须为明确布尔值")
        if self.target_sha != design.content_sha or not self.reviewer.strip():
            raise StitchPressError("审核必须对应当前母版和具名审核人")
        if not all((self.overall, self.negative_space, self.source_match)):
            raise StitchPressError("冻结前须完成整体、负空间和来源对应审查")
        if len(set(self.detail_regions)) < 2:
            raise StitchPressError("冻结前至少审查两处不同局部")
        for x0, y0, x1, y1 in self.detail_regions:
            if any(type(v) is not int for v in (x0, y0, x1, y1)):
                raise StitchPressError("局部审核坐标须为整数")
            if not 0 <= x0 < x1 <= design.width or not 0 <= y0 < y1 <= design.height:
                raise StitchPressError("局部审核坐标越界")

    def validate_context(self, context=None):
        for key in ("source_sha", "brief_sha", "annotations_sha", "input_icc_sha"):
            if getattr(self, key) != (context or {}).get(key):
                raise StitchPressError(
                    "审核对应的源图/设计要求/蒙版/输入ICC已改变或旧审核未绑定来源，请重新核对冻结"
                )


@dataclass(frozen=True)
class PatchCell:
    x: int
    y: int
    before: int
    after: int


def apply_patch(
    design: Design,
    *,
    base_sha: str,
    cells: tuple[PatchCell, ...],
    allowed: frozenset[tuple[int, int]],
    protected: frozenset[tuple[int, int]] = frozenset(),
    append_colors: tuple[ThreadColor, ...] = (),
    capacity: int = 66,
) -> Design:
    if base_sha != design.content_sha:
        raise StitchPressError("补丁基准已过期；保留当前母版与候选")
    labels = [list(row) for row in design.labels]
    seen = set()
    palette = design.palette + append_colors
    for cell in cells:
        xy = (cell.x, cell.y)
        if any(type(v) is not int for v in (cell.x, cell.y, cell.before, cell.after)):
            raise StitchPressError("补丁坐标和值必须为整数")
        if (
            xy in seen
            or xy not in allowed
            or xy in protected
            or not (0 <= cell.x < design.width and 0 <= cell.y < design.height)
        ):
            raise StitchPressError("补丁越界、触及保护区或重复坐标")
        if labels[cell.y][cell.x] != cell.before:
            raise StitchPressError(f"补丁原值不匹配：{xy}")
        if not -1 <= cell.after < len(palette):
            raise StitchPressError("补丁目标色板索引非法")
        labels[cell.y][cell.x] = cell.after
        seen.add(xy)
    result = replace(
        design,
        palette=palette,
        labels=tuple(tuple(r) for r in labels),
        revision_id=uuid4().hex,
        parent_revision_id=design.revision_id,
        origin="patched",
        provenance=(("base_sha", base_sha), ("patch_sha", digest([asdict(c) for c in cells]))),
    )
    if sum(n > 0 for n in result.counts) > capacity:
        raise StitchPressError("补丁后有效色数超过符号库容量")
    return result
