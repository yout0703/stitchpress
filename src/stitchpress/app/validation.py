"""Validate portable entity relationships without mutating or resolving external paths."""

import math

from stitchpress.core.brief import DesignBrief
from stitchpress.core.configuration import ReferenceSettings, ShowcaseSettings
from stitchpress.core.conversion import ConversionOptions
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import sha256
from stitchpress.core.layout import Fabric
from stitchpress.core.masks import annotation_masks
from stitchpress.core.models import Design, ReviewRecord
from stitchpress.core.resources import assign_symbols, library_from_assets, load_catalog


def validate_entities(entities: dict, assets: dict[str, bytes]):
    if not isinstance(entities, dict) or not all(isinstance(k, str) and k for k in entities):
        raise StitchPressError("作品实体须为具名对象")

    def json_value(value, depth=0):
        if depth > 40:
            raise StitchPressError("作品实体嵌套过深")
        if isinstance(value, dict):
            if not all(isinstance(k, str) for k in value):
                raise StitchPressError("作品字段名称必须为文本")
            forbidden = {"api_key", "password", "token", "secret", "authorization"}
            if any(k.casefold() in forbidden for k in value):
                raise StitchPressError("作品不能保存密钥字段，须使用系统安全存储")
            for item in value.values():
                json_value(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                json_value(item, depth + 1)
        elif isinstance(value, float) and not math.isfinite(value):
            raise StitchPressError("作品不能包含非有限数值")
        elif value is not None and not isinstance(value, (str, bool, int, float)):
            raise StitchPressError("作品实体包含不可携带的数据类型")

    json_value(entities)
    for key, record in entities.items():
        if key.startswith("blueprint:"):
            from stitchpress.core.blueprints import check_blueprint

            check_blueprint(record, assets)
        if key.startswith("candidate:") and isinstance(record, dict) and record.get("design"):
            candidate = Design.from_dict(record["design"])
            if record.get("result_sha", candidate.content_sha) != candidate.content_sha:
                raise StitchPressError("针格候选SHA与实际矩阵不一致")
    if any(
        v is not None and not isinstance(v, dict) for k, v in entities.items() if k != "mapping"
    ):
        raise StitchPressError("作品实体记录必须为对象或已删除标记")
    metadata = entities.get("metadata")
    if not isinstance(metadata, dict) or any(
        not isinstance(metadata.get(k), str) for k in ("title", "code")
    ):
        raise StitchPressError("作品名称/客户货号必须为文本，保留前导零")
    if any(len(metadata[k]) > 2000 for k in ("title", "code")):
        raise StitchPressError("作品名称/货号超过2000字符")
    design = Design.from_dict(entities["design"]) if entities.get("design") else None
    if entities.get("fabric"):
        Fabric(**entities["fabric"])
    if entities.get("reference"):
        ReferenceSettings(**entities["reference"])
    if entities.get("showcase"):
        ShowcaseSettings(**entities["showcase"])
    if entities.get("post"):
        from stitchpress.core.post import PostSettings

        PostSettings(**entities["post"])
    if entities.get("commerce"):
        from stitchpress.core.commerce import CommerceSettings, SceneSettings

        settings = CommerceSettings(**entities["commerce"])
        if settings.font_asset and settings.font_asset not in assets:
            raise StitchPressError("商品模板缺少内嵌字体")
        for scene in settings.scenes:
            asset = SceneSettings(**scene).background_asset
            if asset and asset not in assets:
                raise StitchPressError("商品模板缺少内嵌背景")
    if entities.get("conversion_options"):
        ConversionOptions(**entities["conversion_options"])
    if entities.get("brief"):
        DesignBrief(**entities["brief"])
    if entities.get("material_rules"):
        from stitchpress.core.materials import MaterialRule

        rules = entities["material_rules"].get("rules")
        if not isinstance(rules, list) or len(rules) > 12:
            raise StitchPressError("配线规则须为不超过12套的清单")
        for record in rules:
            MaterialRule(**record)
    if entities.get("text_layers"):
        from stitchpress.core.text import parse_layers

        for layer in parse_layers(entities["text_layers"]).values():
            if layer.font_asset and layer.font_asset not in assets:
                raise StitchPressError("文字区域缺少内嵌字体")
    if entities.get("ai_policy"):
        from .task_history import AIPolicy

        AIPolicy(**entities["ai_policy"])
    if entities.get("ai_usage"):
        from .task_history import validate_usage

        validate_usage(entities["ai_usage"])
    if annotations := entities.get("annotations"):
        grid = annotations.get("grid")
        if (
            not isinstance(grid, list)
            or len(grid) != 2
            or any(type(n) is not int or n < 1 for n in grid)
            or grid[0] * grid[1] > 1_000_000
        ):
            raise StitchPressError("蒙版针格范围无效")
        annotation_masks(annotations, *grid, annotations.get("source_sha"))
    library = library_from_assets(entities["library"], assets) if entities.get("library") else None
    mapping = entities.get("mapping")
    if mapping is not None:
        if not isinstance(mapping, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
        ):
            raise StitchPressError("符号映射须为文本身份对应关系")
        if design and library and assign_symbols(design, library, mapping) != mapping:
            raise StitchPressError("已保存映射未完整覆盖当前母版")
    for name in ("source", "catalog", "source_profile", "output_font", "input_profile"):
        if record := entities.get(name):
            asset = record.get("asset")
            if not isinstance(asset, str) or asset not in assets:
                raise StitchPressError(f"作品缺少内嵌资源：{name}")
            if record.get("sha256") and sha256(assets[asset]) != record["sha256"]:
                raise StitchPressError(f"作品资源实体SHA不符：{name}")
            if name == "catalog":
                load_catalog(assets[asset], record.get("brand", "DMC"))
    if record := entities.get("freeze"):
        if design is None:
            raise StitchPressError("审核记录缺少对应母版")
        value = {**record, "detail_regions": tuple(tuple(r) for r in record["detail_regions"])}
        ReviewRecord(**value).validate(design)
