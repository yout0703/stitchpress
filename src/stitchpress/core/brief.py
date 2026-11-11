"""Human-editable design requirements; model analysis is never an approval."""

from dataclasses import asdict, dataclass

from .errors import StitchPressError
from .identity import digest

INPUT_KINDS = {
    "unspecified": "尚未判断",
    "text": "文字原创",
    "flat_image": "干净平面蓝图",
    "textured_image": "带针脚或材质的效果图",
    "photo": "拍摄照片",
    "engineering": "已有针格工程",
}
ROUTES = {
    "clarify": "待补充/确认",
    "direct_blueprint": "复用干净蓝图",
    "rebuild": "重建平面蓝图",
    "existing_design": "复用已有针格",
}


@dataclass(frozen=True)
class DesignBrief:
    input_kind: str = "unspecified"
    route: str = "clarify"
    topic: str = ""
    key_objects: tuple[str, ...] = ()
    composition: str = ""
    must_keep: str = ""
    simplification: str = ""
    background: str = "unconfirmed"
    width: int = 160
    height: int = 160
    max_colors: int = 32
    catalog_revision: str | None = None
    source_sha: str | None = None
    confirmed: bool = False
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise StitchPressError("设计要求版本无效")
        if self.input_kind not in INPUT_KINDS or self.route not in ROUTES:
            raise StitchPressError("输入类型或制版路径无效")
        if (
            self.background not in {"unconfirmed", "full", "selective"}
            or type(self.confirmed) is not bool
        ):
            raise StitchPressError("背景或确认状态无效")
        if any(type(v) is not int or not 1 <= v <= 1000 for v in (self.width, self.height)):
            raise StitchPressError("针格宽高须为1–1000整数")
        if type(self.max_colors) is not int or not 1 <= self.max_colors <= 512:
            raise StitchPressError("色数上限须为1–512整数")
        if any(
            not isinstance(s, str) or len(s) > 8000
            for s in (self.topic, self.composition, self.must_keep, self.simplification)
        ):
            raise StitchPressError("设计要求文本过长或格式无效")
        if (
            not isinstance(self.key_objects, (list, tuple))
            or len(self.key_objects) > 100
            or any(
                not isinstance(s, str) or not s.strip() or len(s) > 500 for s in self.key_objects
            )
        ):
            raise StitchPressError("关键对象须为不超过100项的文本清单")
        for value in (self.catalog_revision, self.source_sha):
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise StitchPressError("输入和目录修订须为SHA256或未指定")
        if (
            self.confirmed
            and self.route == "direct_blueprint"
            and (self.input_kind != "flat_image" or not self.source_sha)
        ):
            raise StitchPressError("直接采用须人工确认为干净平面蓝图并绑定输入")
        if self.confirmed and self.route == "existing_design" and self.input_kind != "engineering":
            raise StitchPressError("已有工程路径须明确选择工程输入")

    @property
    def revision(self):
        return digest(self.to_dict())

    def to_dict(self):
        return asdict(self)

    def validate_context(self, *, source_sha, catalog_revision, capacity):
        if self.source_sha != source_sha or self.catalog_revision != catalog_revision:
            raise StitchPressError("设计要求绑定的图片或线色目录已改变，请重新核对规格")
        if self.max_colors > capacity:
            raise StitchPressError("设计色数上限超过当前符号库容量")
        if not self.confirmed:
            raise StitchPressError("请先人工确认设计要求；模型分析不会代替确认")
        if self.route != "direct_blueprint":
            raise StitchPressError("当前路径尚未采用干净蓝图；请先重建/确认蓝图再转换")
