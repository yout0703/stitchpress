import io
import json
import re
from xml.etree import ElementTree as XML

from defusedxml import ElementTree as SafeXML

from .errors import StitchPressError
from .identity import sha256
from .limits import MAX_CHART_BYTES
from .models import MAX_CELLS, Design, ThreadColor, new_design


def _oxs_headers(data):
    """Retain metadata only; discard millions of stitch XML objects as they arrive."""
    stack, root, stitches = [], None, 0
    for event, node in SafeXML.iterparse(
        io.BytesIO(data),
        events=("start", "end"),
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    ):
        if event == "start":
            stack.append(node)
            if len(stack) == 1:
                root = node
                if root.tag != "chart":
                    raise StitchPressError("仅支持OXS 1.0 chart根元素")
            if len(stack) == 2 and node.tag not in {
                "format",
                "commentboxes",
                "properties",
                "palette",
                "fullstitches",
                "backstitches",
                "partstitches",
                "ornaments_inc_knots_and_beads",
            }:
                raise StitchPressError(f"未知OXS记录：{node.tag}")
            if len(stack) == 3 and stack[1].tag in {
                "backstitches",
                "partstitches",
                "ornaments_inc_knots_and_beads",
            }:
                raise StitchPressError(f"不支持非全针记录：{stack[1].tag}")
        else:
            if len(stack) == 3 and stack[1].tag == "fullstitches":
                stitches += 1
                if stitches > MAX_CELLS:
                    raise StitchPressError(f"OXS针数超过{MAX_CELLS:,}格上限")
                stack[1].clear()
            if len(stack) == 3 and stack[1].tag == "palette" and len(stack[1]) > 513:
                raise StitchPressError("OXS色板超过512色上限")
            stack.pop()
    return root


def import_oxs(data: bytes) -> Design:
    if len(data) > MAX_CHART_BYTES:
        raise StitchPressError(f"OXS文件{len(data) / 1024**2:.1f}MiB，超过256MiB导入上限")
    try:
        root = _oxs_headers(data)
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
            raise StitchPressError(
                f"OXS针格{w:,}×{h:,}（{w * h:,}格），须为正且不超过{MAX_CELLS:,}格"
            )
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
        seen = bytearray(w * h)
        stack = []
        for event, stitch in SafeXML.iterparse(
            io.BytesIO(data),
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        ):
            if event == "start":
                stack.append(stitch)
                continue
            parent = stack[-2] if len(stack) > 1 else None
            stack.pop()
            if parent is None or parent.tag != "fullstitches":
                continue
            if (
                stitch.tag != "stitch"
                or set(stitch.attrib) != {"x", "y", "palindex"}
                or len(stitch)
            ):
                raise StitchPressError("未知OXS针法字段/记录")
            x, y, idx = (int(stitch.attrib[k]) for k in ("x", "y", "palindex"))
            if not 0 <= x < w or not 0 <= y < h or seen[y * w + x]:
                raise StitchPressError("OXS坐标越界/重复，须明确0基坐标")
            if idx not in index_map or index_map[idx] == -1:
                raise StitchPressError("OXS针格引用未知或布色索引")
            labels[y][x] = index_map[idx]
            seen[y * w + x] = 1
            parent.clear()
        return new_design(
            w,
            h,
            palette,
            labels,
            provenance=(
                ("source_sha", sha256(data)),
                ("parser", "oxs-1-zero-based-v1"),
                (
                    "import_report",
                    json.dumps(dict(title=props.get("charttitle", "")), ensure_ascii=False),
                ),
            ),
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
    buffer = io.BytesIO()
    buffer.write(
        XML.tostring(root, encoding="utf-8", xml_declaration=True).removesuffix(b"</chart>")
    )
    buffer.write(b"<fullstitches>")
    for y, row in enumerate(design.labels):
        buffer.write(
            "".join(
                f'<stitch x="{x}" y="{y}" palindex="{i + 1}" />'
                for x, i in enumerate(row)
                if i >= 0
            ).encode("utf-8")
        )
        if buffer.tell() > MAX_CHART_BYTES:
            raise StitchPressError("OXS导出超过256MiB上限；未生成不完整文件")
    buffer.write(b"</fullstitches><backstitches /></chart>")
    return buffer.getvalue()


def import_chart(data: bytes, format: str) -> Design:
    """Explicit codec dispatch, shared by import and unchanged-original checks."""
    if format == "oxs":
        return import_oxs(data)
    if format == "xsd":
        from .xsd import import_xsd

        return import_xsd(data)
    raise StitchPressError("图纸格式不支持，请选择全针 XSD 或 OXS")
