"""Pattern Maker v4 full-cross reader, directly implemented; no reference EXE code.

Binary layout cross-check: StitchForge's existing independent codec and
https://github.com/embroidery-space/embroiderly/blob/main/crates/embroiderly-parsers/src/pmaker/xsd.rs
No XSD writer or universal Pattern Maker compatibility is promised.
"""

import json
import struct

from .errors import StitchPressError
from .identity import sha256
from .models import MAX_CELLS, ThreadColor, new_design


class Reader:
    def __init__(self, data: bytes, encoding: str):
        self.data, self.pos, self.encoding = data, 0, encoding

    def take(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise StitchPressError(f"XSD截断或长度无效，位置{self.pos}")
        result = self.data[self.pos : self.pos + count]
        self.pos += count
        return result

    def number(self, fmt="H"):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def decode(self, data):
        value = data.split(b"\0", 1)[0]
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return value.decode(self.encoding)
            except UnicodeDecodeError as exc:
                raise StitchPressError(
                    "XSD文字编码不匹配，请重新选择编码；不能替换丢失原线号"
                ) from exc

    def text(self, count):
        return self.decode(self.take(count + 1))


def _grid(reader, count):
    seeds = struct.unpack("<4I", reader.take(16))
    key = (
        (seeds[0] & 255) << 24
        | ((seeds[1] >> 8) & 255) << 16
        | ((seeds[2] >> 16) & 255) << 8
        | (seeds[3] & 255)
    )
    rotations = [(word >> bit) % 32 for word in seeds for bit in range(4)]
    counter, step = seeds[:2]
    word_index, cells = 0, []
    while len(cells) < count:
        raw = reader.take(reader.number("I") * 4)
        repeat = None
        for (word,) in struct.iter_unpack("<I", raw):
            value = word ^ key ^ counter
            shift = rotations[word_index % 16]
            key = ((key << shift) | (key >> ((32 - shift) % 32))) & 0xFFFFFFFF
            counter = (counter + step) & 0xFFFFFFFF
            word_index += 1
            if repeat is None and value & 0x40000000:
                repeat = (value & 0x3FFFFFFF) >> 16
                if repeat == 0:
                    raise StitchPressError("XSD压缩段重复次数为零")
                continue
            size = 1 if repeat is None else repeat
            if len(cells) + size > count:
                raise StitchPressError("XSD压缩段超出声明针格")
            cells.extend([value] * size)
            repeat = None
        if repeat is not None:
            raise StitchPressError("XSD压缩段不完整")
    return cells


def import_xsd(data: bytes, encoding="gb18030"):
    if len(data) > 16 * 1024 * 1024 or encoding not in {"gb18030", "cp1251", "cp1252"}:
        raise StitchPressError("XSD文件过大或文字编码不支持")
    r = Reader(data, encoding)
    if r.number() != 0x0510:
        raise StitchPressError("仅支持Pattern Maker v4二进制XSD (0x0510)")
    r.take(739)
    w, h = r.number(), r.number()
    if min(w, h) < 1 or w * h > MAX_CELLS:
        raise StitchPressError("XSD针格超出试验版范围")
    if r.number("I") or r.number():
        raise StitchPressError("XSD包含小针/半针/回针/结粒等非全针记录")
    ct_x, ct_y = r.number(), r.number()
    if ct_x != ct_y or not 6 <= ct_x <= 40:
        raise StitchPressError("XSD导入目前只支持经纬相同的6–40 CT Aida")
    r.take(6)
    palette_size = r.number()
    if palette_size > 240:
        raise StitchPressError("XSD色板超过格式240项限制")
    palette = []
    for _ in range(palette_size):
        record = r.take(123)
        if struct.unpack_from("<H", record, 59)[0] or struct.unpack_from("<I", record, 113)[0] == 1:
            raise StitchPressError("XSD包含混线或珠绣材料")
        palette.append(
            {
                "brand": {0: "DMC", 1: "Anchor", 2: "Madeira"}.get(
                    record[2], f"PatternMakerBrand:{record[2]}"
                ),
                "code": r.decode(record[3:14]),
                "name": r.decode(record[14:55]),
                "rgb": tuple(record[55:58]),
            }
        )
    r.take(palette_size * 2)
    for color in palette:
        for stitch_type in range(9):
            note = r.take(r.number())
            if stitch_type == 0 and note.startswith(b"StitchForge:"):
                try:
                    identity = json.loads(note[len(b"StitchForge:") :])
                    for name in ("brand", "code", "name"):
                        if name in identity and isinstance(identity[name], str):
                            color[name] = identity[name]
                except (ValueError, TypeError) as exc:
                    raise StitchPressError("XSD线材身份备注损坏，不能猜测原身份") from exc
    strands = []
    for _ in palette:
        strands.append(r.number())
        r.take(14)
    r.take(240 * 117 + palette_size * 12 + 380 + 56)
    cloth_name = r.text(40)
    cloth_rgb = tuple(r.take(3))
    r.take(65)
    title, author, company = r.text(40), r.text(40), r.text(40)
    copyright_text, notes = r.text(200), r.text(2048)
    r.take(6)
    fabric_kind = r.text(40)
    if fabric_kind.strip().lower() not in {"", "aida", "аида", "绣布"}:
        raise StitchPressError("XSD绣布组织未支持，请明确实际格距")
    r.take(206)
    default_strands = r.number()
    r.take(50 + 32 + 16412 + 512)
    words = _grid(r, w * h)
    r.take(2)
    if r.number():
        raise StitchPressError("XSD包含特殊针法模型")
    # Extra records are not silently discarded. This deliberately rejects some native variants.
    if r.pos != len(data):
        raise StitchPressError("XSD含未识别尾部记录，当前无法确认其针法语义")
    labels = []
    for offset, word in enumerate(words):
        kind, index = word >> 24, (word >> 16) & 255
        if kind == 15:
            labels.append(-1)
        elif kind == 0 and index < palette_size:
            labels.append(index)
        else:
            raise StitchPressError(f"XSD第{offset + 1}格含未知针法或非法色板索引")
    report = {
        "parser": "pmaker-v4-full-v1",
        "ct": ct_x,
        "title": title,
        "author": author,
        "company": company,
        "copyright": copyright_text,
        "notes": notes,
        "cloth_name": cloth_name,
        "cloth_rgb": cloth_rgb,
        "strands": [s or default_strands for s in strands],
        "encoding": encoding,
    }
    if any(not 1 <= s <= 6 for s in report["strands"]):
        raise StitchPressError("XSD全针股数须为1–6")
    design = new_design(
        w,
        h,
        [ThreadColor(**c, catalog_revision="embedded-xsd-" + sha256(data)) for c in palette],
        [labels[y * w : (y + 1) * w] for y in range(h)],
        provenance=(
            ("source_sha", sha256(data)),
            ("import_report", json.dumps(report, ensure_ascii=False)),
        ),
    )
    return design
