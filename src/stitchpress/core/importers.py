import re
from xml.etree import ElementTree as XML

from defusedxml import ElementTree as SafeXML

from .errors import StitchPressError
from .identity import sha256
from .models import MAX_CELLS, Design, ThreadColor, new_design


def import_oxs(data: bytes) -> Design:
    if len(data) > 16 * 1024 * 1024:
        raise StitchPressError("OXS超过16MiB试验限制")
    try:
        root = SafeXML.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if root.tag != "chart":
            raise StitchPressError("仅支持OXS 1.0 chart根元素")
        allowed = {
            "format",
            "commentboxes",
            "properties",
            "palette",
            "fullstitches",
            "backstitches",
            "partstitches",
            "ornaments_inc_knots_and_beads",
        }
        for node in root:
            if node.tag not in allowed:
                raise StitchPressError(f"未知OXS记录：{node.tag}")
            if node.tag == "format":
                if len(node) or any(not re.fullmatch(r"comments\d+", k) for k in node.attrib):
                    raise StitchPressError("未知OXS格式说明，无法确认是否包含针法")
                continue
            if node.tag not in {"properties", "palette", "fullstitches"} and len(node):
                raise StitchPressError(f"不支持非全针记录：{node.tag}，数量{len(node)}")
        for tag in ("properties", "palette", "fullstitches"):
            if len(root.findall(tag)) != 1:
                raise StitchPressError(f"OXS须恰有一个{tag}")
        props = root.find("properties")
        if props.get("oxsversion") != "1.0":
            raise StitchPressError("仅支持明确声明的OXS 1.0")
        w, h = int(props.attrib["chartwidth"]), int(props.attrib["chartheight"])
        if min(w, h) < 1 or w * h > MAX_CELLS:
            raise StitchPressError("OXS针格尺寸无效或超出试验上限")
        palette, index_map = [], {}
        for item in root.find("palette"):
            if item.tag != "palette_item" or len(item):
                raise StitchPressError("未知OXS色板记录")
            idx = int(item.attrib["index"])
            if idx in index_map:
                raise StitchPressError("OXS色板索引重复")
            if item.get("blendcolor", "nil") != "nil":
                raise StitchPressError("不支持混线材料")
            number = item.attrib["number"]
            if idx == 0 and number.lower() == "cloth":
                index_map[idx] = -1
                continue
            if idx < 0:
                raise StitchPressError("OXS色板索引不得为负")
            match = re.fullmatch(r"([0-9a-fA-F]{6})", item.attrib["color"].lstrip("#"))
            if not match:
                raise StitchPressError("OXS颜色须为6位RGB")
            brand, code = number.split(" ", 1) if " " in number else ("Unresolved", number)
            palette.append(
                ThreadColor(
                    brand,
                    code,
                    item.get("name", code),
                    tuple(bytes.fromhex(match[1])),
                    "embedded-oxs-" + sha256(data),
                )
            )
            index_map[idx] = len(palette) - 1
        labels = [[-1] * w for _ in range(h)]
        seen = set()
        for stitch in root.find("fullstitches"):
            if (
                stitch.tag != "stitch"
                or set(stitch.attrib) != {"x", "y", "palindex"}
                or len(stitch)
            ):
                raise StitchPressError("未知OXS针法字段/记录")
            x, y, idx = (int(stitch.attrib[k]) for k in ("x", "y", "palindex"))
            if not 0 <= x < w or not 0 <= y < h or (x, y) in seen:
                raise StitchPressError("OXS坐标越界/重复，须明确0基坐标")
            if idx not in index_map or index_map[idx] == -1:
                raise StitchPressError("OXS针格引用未知或布色索引")
            labels[y][x] = index_map[idx]
            seen.add((x, y))
        return new_design(
            w,
            h,
            palette,
            labels,
            provenance=(("source_sha", sha256(data)), ("parser", "oxs-1-zero-based-v1")),
        )
    except StitchPressError:
        raise
    except Exception as exc:
        raise StitchPressError("OXS损坏、字段缺失或含不安全XML") from exc


def export_oxs(design: Design) -> bytes:
    root = XML.Element("chart")
    XML.SubElement(
        root,
        "properties",
        oxsversion="1.0",
        software="StitchPress",
        chartwidth=str(design.width),
        chartheight=str(design.height),
        palettecount=str(len(design.palette)),
    )
    palette = XML.SubElement(root, "palette")
    XML.SubElement(
        palette,
        "palette_item",
        index="0",
        number="cloth",
        name="cloth",
        color="FFFFFF",
        blendcolor="nil",
    )
    for i, color in enumerate(design.palette, 1):
        XML.SubElement(
            palette,
            "palette_item",
            index=str(i),
            number=f"{color.brand} {color.code}",
            name=color.name,
            color=bytes(color.rgb).hex().upper(),
            blendcolor="nil",
        )
    stitches = XML.SubElement(root, "fullstitches")
    for y, row in enumerate(design.labels):
        for x, i in enumerate(row):
            if i >= 0:
                XML.SubElement(stitches, "stitch", x=str(x), y=str(y), palindex=str(i + 1))
    XML.SubElement(root, "backstitches")
    return XML.tostring(root, encoding="utf-8", xml_declaration=True)
