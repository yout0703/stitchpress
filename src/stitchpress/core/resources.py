import io
import json
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageFont

from .errors import ResourceError, StitchPressError
from .identity import digest, sha256, symbol_key
from .models import Design, ThreadColor


def bundled_font() -> Path:
    return Path(__file__).parents[1] / "resources" / "fonts" / "NotoSans-Regular.ttf"


def load_font(size: int, path: Path | bytes | None = None):
    path = path or bundled_font()
    if isinstance(path, bytes):
        if len(path) > 32 * 1024 * 1024:
            raise ResourceError("字体超过32MiB资源上限")
        try:
            return ImageFont.truetype(io.BytesIO(path), size=size)
        except OSError as exc:
            raise ResourceError("字体无法解码，请导入有效TTF/OTF") from exc
    if not path.is_file():
        raise ResourceError("缺少版式所绑定的字体文件，请重新导入")
    return ImageFont.truetype(str(path), size=size)


def natural_color_key(color):
    import re

    parts = tuple(
        (0, int(p)) if p.isdigit() else (1, p.casefold())
        for p in re.split(r"(\d+)", color.code)
        if p
    )
    return color.brand.casefold(), parts, color.code, color.key


@dataclass(frozen=True)
class Symbol:
    key: str
    external_id: int | str
    filename: str
    tile: bytes
    pixel_sha: str

    def image(self) -> Image.Image:
        with Image.open(io.BytesIO(self.tile)) as im:
            im.load()
            return im.copy()


@dataclass(frozen=True)
class SymbolLibrary:
    revision: str
    symbols: tuple[Symbol, ...]
    icc: bytes | None
    usage: str

    def __post_init__(self):
        if not isinstance(self.revision, str) or not self.revision or len(self.symbols) > 512:
            raise ResourceError("符号库修订无效或超过512个资源上限")
        if self.icc:
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(self.icc))
                if profile.profile.xcolor_space.strip() != "CMYK":
                    raise ValueError("not CMYK")
            except Exception as exc:
                raise ResourceError("符号库共享ICC损坏或不是CMYK配置") from exc
        if not self.symbols or len({s.key for s in self.symbols}) != len(self.symbols):
            raise ResourceError("符号库为空或ID重复")
        if len({s.pixel_sha for s in self.symbols}) != len(self.symbols):
            raise ResourceError("符号库含像素完全重复的图块")

    @property
    def by_key(self):
        return {s.key: s for s in self.symbols}


def parse_symbol(data: bytes, external_id: int | str, filename: str) -> Symbol:
    if len(data) > 8 * 1024 * 1024:
        raise ResourceError("符号原件超过8MiB试验限制")
    with Image.open(io.BytesIO(data)) as source:
        if getattr(source, "n_frames", 1) != 1 or source.width * source.height > 1_000_000:
            raise ResourceError("符号必须为单页，且不能超过100万像素")
        source.load()
        if source.mode != "CMYK":
            raise ResourceError("当前印花小样仅接收已规范化的CMYK符号；不能直接改模式冒充ICC转换")
        pixels = source.tobytes()
        if not any(pixels):
            raise ResourceError("符号图块为空白")
        if source.info.get("icc_profile"):
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(source.info["icc_profile"]))
                if profile.profile.xcolor_space.strip() != "CMYK":
                    raise ValueError("CMYK tile with non-CMYK ICC")
            except Exception as exc:
                raise ResourceError("符号ICC损坏") from exc
        return Symbol(
            symbol_key(external_id),
            external_id,
            filename,
            data,
            sha256(source.mode.encode() + str(source.size).encode() + pixels),
        )


def normalize_symbol_image(data: bytes, target_icc: bytes | None) -> tuple[bytes, dict]:
    """Normalize explicitly imported glyphs; retain source separately at the app boundary."""
    if len(data) > 8 * 1024 * 1024:
        raise ResourceError("符号原件超过8MiB限制")
    with Image.open(io.BytesIO(data)) as source:
        if source.format not in {"TIFF", "PNG", "BMP"}:
            raise ResourceError("符号导入仅支持TIF/PNG/BMP")
        if getattr(source, "n_frames", 1) != 1 or source.width * source.height > 1_000_000:
            raise ResourceError("符号须为单页且不超过100万像素")
        source.load()
        source_icc = source.info.get("icc_profile")
        record = {
            "source_sha": sha256(data),
            "source_format": source.format,
            "source_mode": source.mode,
            "size": source.size,
            "source_icc_sha": sha256(source_icc) if source_icc else None,
            "target_icc_sha": sha256(target_icc) if target_icc else None,
            "alpha": "white-background",
            "revision": "symbol-normalize-v1",
        }
        if source.mode == "CMYK" and source_icc == target_icc:
            # Preserve original ink values for already normalized factory tiles.
            return data, {**record, "method": "preserve-cmyk"}
        if target_icc is None:
            if source_icc or source.mode == "CMYK":
                raise ResourceError("演示库没有输出ICC，不能丢弃已配置色彩语境；请先导入生产库")
            rgba = source.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            converted = background.convert("RGB").convert("CMYK")
            record["method"] = "demo-only-unprofiled-rgb-to-cmyk"
        else:
            try:
                destination = ImageCms.ImageCmsProfile(io.BytesIO(target_icc))
                if destination.profile.xcolor_space.strip() != "CMYK":
                    raise ValueError("target not CMYK")
                if source.mode == "CMYK":
                    if not source_icc:
                        raise ResourceError("CMYK原件缺少ICC，不能猜测输入配置")
                    rgb = source
                    input_profile = ImageCms.ImageCmsProfile(io.BytesIO(source_icc))
                else:
                    rgba = source.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    background.alpha_composite(rgba)
                    rgb = background.convert("RGB")
                    input_profile = (
                        ImageCms.ImageCmsProfile(io.BytesIO(source_icc))
                        if source_icc
                        else ImageCms.createProfile("sRGB")
                    )
                converted = ImageCms.profileToProfile(
                    rgb, input_profile, destination, outputMode="CMYK", renderingIntent=1
                )
                record["method"] = "icc-relative-colorimetric"
                record["unprofiled_rgb_assumption"] = "sRGB" if not source_icc else None
            except ResourceError:
                raise
            except Exception as exc:
                raise ResourceError("符号ICC转换失败，未采用；请检查输入/输出配置") from exc
        buffer = io.BytesIO()
        converted.save(buffer, "TIFF", compression="tiff_lzw", icc_profile=target_icc)
        converted.close()
        result = buffer.getvalue()
        parse_symbol(result, "validation", "normalized.tif")
        return result, {**record, "normalized_sha": sha256(result)}


def demo_library(count: int = 66) -> SymbolLibrary:
    """Self-made test glyphs, explicitly unsuitable for customer production acceptance."""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz@#%&"
    symbols = []
    for i, char in enumerate(chars[:count]):
        im = Image.new("CMYK", (20, 20), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((2, -2), char, font=load_font(18), fill=(255,) * 4)
        output = io.BytesIO()
        im.save(output, "TIFF", compression="tiff_lzw")
        symbols.append(parse_symbol(output.getvalue(), i, f"demo-{i}.tif"))
    return SymbolLibrary(
        digest([s.pixel_sha for s in symbols]),
        tuple(symbols),
        None,
        "self-authored-demo-not-factory-approved",
    )


def load_catalog(data: bytes, brand: str = "DMC") -> tuple[ThreadColor, ...]:
    values = json.loads(data)
    if not isinstance(values, list) or not 1 <= len(values) <= 4096:
        raise ResourceError("线色目录必须为1–4096条原始记录")
    revision = sha256(data)
    colors = tuple(
        ThreadColor(brand, c["code"], c["name"], tuple(c["rgb"]), revision) for c in values
    )
    if len({c.key for c in colors}) != len(colors):
        raise ResourceError("目录包含重复线号身份")
    return colors


def assign_symbols(
    design: Design, library: SymbolLibrary, existing: dict[str, str] | None = None
) -> dict[str, str]:
    colors = [c for c, n in zip(design.palette, design.counts, strict=True) if n]
    if len(colors) > len(library.symbols):
        raise StitchPressError(f"有效色数{len(colors)}超过符号容量{len(library.symbols)}")
    keys = {c.key for c in colors}
    result = {k: v for k, v in (existing or {}).items() if k in keys}
    if any(v not in library.by_key for v in result.values()) or len(set(result.values())) != len(
        result
    ):
        raise StitchPressError("既有/手动绑定引用未知或重复符号")
    available = [s.key for s in library.symbols if s.key not in result.values()]
    # Source palette order is stable. Assignment is persisted, not rerun at export.
    for color in sorted(colors, key=natural_color_key):
        if color.key not in result:
            result[color.key] = available.pop(0)
    return result


def revise_library(
    library: SymbolLibrary,
    operation: str,
    *,
    key=None,
    external_id=None,
    data=None,
    name=None,
    used=frozenset(),
):
    symbols = list(library.symbols)
    if operation == "add":
        new = parse_symbol(data, external_id, name)
        if new.key in library.by_key:
            raise ResourceError("此精确类型的符号ID已存在")
        symbols.append(new)
    elif operation in {"replace", "rename", "remove"}:
        if key not in library.by_key:
            raise ResourceError("所选符号不存在")
        index = next(i for i, symbol in enumerate(symbols) if symbol.key == key)
        if operation == "remove":
            if key in used:
                raise ResourceError("不能移除当前使用中的符号；请先事务性调整绑定")
            symbols.pop(index)
        elif operation == "rename":
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ResourceError("显示名称须为1–200字符")
            symbols[index] = replace(symbols[index], filename=name.strip())
        else:
            new = parse_symbol(data, symbols[index].external_id, name)
            symbols[index] = new
    else:
        raise ResourceError("未知符号库编辑操作")
    if data is not None:
        with Image.open(io.BytesIO(data)) as image:
            if image.info.get("icc_profile") != library.icc:
                raise ResourceError("图块须已规范化为当前库的CMYK ICC；不能暗中改色彩语境")
    revision = digest(
        {
            "parent": library.revision,
            "symbols": [(s.key, s.filename, s.pixel_sha) for s in symbols],
            "icc": sha256(library.icc) if library.icc else None,
        }
    )
    return SymbolLibrary(revision, tuple(symbols), library.icc, library.usage)


def library_assets(library: SymbolLibrary) -> tuple[dict, dict[str, bytes]]:
    assets = {}
    entries = []
    for sym in library.symbols:
        path = f"resources/symbols/{sha256(sym.tile)}.tif"
        assets[path] = sym.tile
        entries.append({"external_id": sym.external_id, "filename": sym.filename, "asset": path})
    icc_path = None
    if library.icc:
        icc_path = f"resources/icc/{sha256(library.icc)}.icc"
        assets[icc_path] = library.icc
    return {
        "revision": library.revision,
        "usage": library.usage,
        "icc_asset": icc_path,
        "entries": entries,
    }, assets


def library_from_assets(record: dict, assets: dict[str, bytes]) -> SymbolLibrary:
    return SymbolLibrary(
        record["revision"],
        tuple(
            parse_symbol(assets[e["asset"]], e["external_id"], e["filename"])
            for e in record["entries"]
        ),
        assets[record["icc_asset"]] if record["icc_asset"] else None,
        record["usage"],
    )
